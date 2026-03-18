"""
SYNAI Relay Protocol — V2 Server
Flask application implementing the V2 multi-worker oracle architecture.

Job statuses:  open -> funded -> resolved | expired | cancelled
Submission statuses: pending -> judging -> passed | failed
"""

from flask import Flask, request, jsonify, g, render_template, send_from_directory, make_response
from models import db, Owner, Agent, Job, Submission, Webhook, IdempotencyKey, Dispute, JobParticipant, utc_iso, SubmissionAccess
from config import Config
from sqlalchemy.exc import IntegrityError
from services.auth_service import generate_api_key, require_auth, require_operator, require_buyer, verify_api_key, authenticate_request, get_or_create_agent_by_wallet
from services.rate_limiter import rate_limit, get_submit_limiter
import re

import atexit
import json
import logging
import os
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

# x402 imports (conditional — graceful when SDK not installed)
try:
    from x402 import x402ResourceServerSync, PaymentRequired, PaymentRequirements
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.http.facilitator_client import HTTPFacilitatorClientSync
    from x402.http import (encode_payment_required_header,
                           decode_payment_signature_header,
                           encode_payment_response_header,
                           PAYMENT_SIGNATURE_HEADER, X_PAYMENT_HEADER)
    from x402.facilitator import SettleResponse, VerifyResponse
    _X402_SDK_AVAILABLE = True
except ImportError:
    _X402_SDK_AVAILABLE = False

from services.x402_service import parse_chain_id, build_requirements

# ---------------------------------------------------------------------------
# Structured logging setup (G14, M7 fix: proper JSON escaping)
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Produce valid JSON log lines even when message contains quotes/newlines."""
    def format(self, record):
        import json as _json
        from flask import has_request_context
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if has_request_context():
            from flask import g as _g
            rid = getattr(_g, 'request_id', None)
            if rid:
                entry["request_id"] = rid
        return _json.dumps(entry)


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger('relay')

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Gzip compression — reduces /jobs 268KB→89KB, dashboard 98KB→~20KB
try:
    from flask_compress import Compress
    Compress(app)
    logger.info("Response compression enabled (flask-compress)")
except ImportError:
    # Fallback: manual gzip via after_request
    import gzip as _gzip
    from io import BytesIO as _BytesIO

    @app.after_request
    def _compress_response(response):
        if (response.status_code < 200 or response.status_code >= 300
                or response.direct_passthrough
                or 'Content-Encoding' in response.headers
                or 'gzip' not in request.headers.get('Accept-Encoding', '')
                or len(response.get_data()) < 500):
            return response
        buf = _BytesIO()
        with _gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6) as f:
            f.write(response.get_data())
        response.set_data(buf.getvalue())
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = len(response.get_data())
        response.headers['Vary'] = 'Accept-Encoding'
        return response
    logger.info("Response compression enabled (manual gzip fallback)")

# Enable WAL mode for SQLite concurrent access (test process + running server)
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine

@sa_event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    import sqlite3
    if isinstance(dbapi_conn, sqlite3.Connection):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        mode = cursor.fetchone()
        cursor.close()
        logger.info("SQLite PRAGMA journal_mode=WAL → %s", mode)

# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------

logger.info("Starting SYNAI Relay Protocol V2")
# C7: Warn about SQLite limitations
if 'sqlite' in Config.SQLALCHEMY_DATABASE_URI:
    logger.warning("SQLite detected — row-level locking (with_for_update) is NOT supported. "
                    "Use PostgreSQL for production deployments.")

# P0-2: Startup guard — reject SQLite in production
Config.validate_production()

# P1-8: Startup check — Guard Layer B configuration
guard_url = os.environ.get('ORACLE_LLM_BASE_URL', '')
guard_key = os.environ.get('ORACLE_LLM_API_KEY', '')
if not guard_url or not guard_key:
    logger.warning("Guard Layer B not configured. "
                   "Set ORACLE_LLM_BASE_URL and ORACLE_LLM_API_KEY for oracle evaluation.")

with app.app_context():
    try:
        db.create_all()
        # Ensure indexes and columns exist on pre-existing databases
        # (create_all only builds new tables, not new columns)
        with db.engine.connect() as conn:
            conn.execute(db.text(
                "CREATE INDEX IF NOT EXISTS ix_submissions_task_status ON submissions (task_id, status)"
            ))
            # Add payout_error column if missing (added 2026-02-15)
            try:
                conn.execute(db.text("SELECT payout_error FROM jobs LIMIT 0"))
            except Exception:
                conn.rollback()
                conn.execute(db.text(
                    "ALTER TABLE jobs ADD COLUMN payout_error TEXT"
                ))
            # Add chain_id column if missing (added for X Layer multi-chain)
            try:
                conn.execute(db.text("SELECT chain_id FROM jobs LIMIT 0"))
            except Exception:
                conn.rollback()
                conn.execute(db.text(
                    "ALTER TABLE jobs ADD COLUMN chain_id INTEGER"
                ))
                logger.info("Migrated: added chain_id column to jobs table")
            conn.commit()
        logger.info("Database tables created / verified")
    except Exception as e:
        logger.critical("Database init failed: %s", e)
        raise

    # L11: Recover stuck judging submissions from previous crash
    try:
        stuck = Submission.query.filter_by(status='judging').count()
        if stuck > 0:
            Submission.query.filter_by(status='judging').update(
                {'status': 'failed', 'oracle_score': 0, 'oracle_reason': 'Server restarted during evaluation'},
                synchronize_session='fetch'
            )
            db.session.commit()
            logger.info("Recovered %d stuck judging submissions", stuck)
    except Exception as e:
        logger.error("Crash recovery check failed: %s", e)

    # P2-2: Provide app reference for webhook failure tracking in background threads
    from services.webhook_service import set_app_ref
    set_app_ref(app)

# ---------------------------------------------------------------------------
# x402 facilitator setup
# ---------------------------------------------------------------------------

_x402_servers: dict = {}  # chain_id -> x402ResourceServerSync
_chain_registry = None  # Set in _init_x402()


def _init_x402():
    """Initialize x402 facilitators and chain registry at startup."""
    global _x402_servers, _chain_registry
    from services.chain_registry import ChainRegistry
    from services.base_adapter import BaseAdapter

    _chain_registry = ChainRegistry(default_chain_id=Config.DEFAULT_CHAIN_ID)

    # Base L2 adapter (always available)
    try:
        from services.wallet_service import get_wallet_service
        ws = get_wallet_service()
        base_adapter = BaseAdapter(ws)
        _chain_registry.register(base_adapter)
    except Exception as e:
        logger.warning("Failed to init Base adapter: %s", e)

    if not (_X402_SDK_AVAILABLE and app.config.get('X402_ENABLED', False)):
        logger.info("x402 disabled or SDK not available")
        return

    # Coinbase facilitator (Base) — requires CDP API keys for mainnet
    # TODO: re-enable once CDP API keys are configured
    # try:
    #     coinbase_fac = HTTPFacilitatorClientSync(
    #         {"url": Config.X402_COINBASE_FACILITATOR_URL})
    #     coinbase_server = x402ResourceServerSync(coinbase_fac)
    #     coinbase_server.register("eip155:8453", ExactEvmServerScheme())
    #     coinbase_server.initialize()
    #     _x402_servers[8453] = coinbase_server
    #     logger.info("x402: Coinbase facilitator registered for Base (8453)")
    # except Exception as e:
    #     logger.warning("x402: Failed to init Coinbase facilitator: %s", e)

    # OKX facilitator (X Layer) — only if OnchainOS credentials configured
    if Config.ONCHAINOS_API_KEY:
        try:
            from services.onchainos_client import OnchainOSClient
            from services.okx_facilitator import OKXFacilitatorClient
            from services.xlayer_adapter import XLayerAdapter

            # S1: single OnchainOSClient shared by facilitator and adapter
            onchainos = OnchainOSClient(
                Config.ONCHAINOS_API_KEY,
                Config.ONCHAINOS_SECRET_KEY,
                Config.ONCHAINOS_PASSPHRASE,
                Config.ONCHAINOS_PROJECT_ID,
            )

            okx_fac = OKXFacilitatorClient(onchainos_client=onchainos)
            okx_server = x402ResourceServerSync(okx_fac)
            okx_server.register("eip155:196", ExactEvmServerScheme())
            okx_server.initialize()
            _x402_servers[196] = okx_server
            logger.info("x402: OKX facilitator registered for X Layer (196)")

            if not Config.OPERATIONS_WALLET_KEY:
                logger.warning("x402: OPERATIONS_WALLET_KEY not set — XLayerAdapter will be read-only (no payout/refund)")
            _chain_registry.register(XLayerAdapter(
                onchainos,
                ops_private_key=Config.OPERATIONS_WALLET_KEY,
                rpc_url=Config.XLAYER_RPC_URL,
                usdc_addr=Config.XLAYER_USDC_CONTRACT,
            ))
        except Exception as e:
            logger.warning("x402: Failed to init OKX facilitator: %s", e)


def _get_x402_server(network: str):
    """Route to correct facilitator based on CAIP-2 network string."""
    chain_id = parse_chain_id(network)
    server = _x402_servers.get(chain_id)
    if not server:
        raise ValueError(f"No x402 facilitator for chain {chain_id}")
    return server


def _x402_enabled_adapters() -> list:
    """Return chain adapters that have an active x402 facilitator."""
    return [
        a for a in (_chain_registry.adapters() if _chain_registry else [])
        if a.chain_id() in _x402_servers
    ]


def _validate_job_fields(data: dict):
    """Validate job request fields. Returns (parsed_fields_dict, None) on success,
    or (None, error_response_tuple) on failure. Call this BEFORE x402 settlement."""
    title = data.get('title')
    description = data.get('description')

    if not title:
        return None, (jsonify({"error": "title is required"}), 400)
    if len(title) > 500:
        return None, (jsonify({"error": "title must be <= 500 characters"}), 400)
    if not description:
        return None, (jsonify({"error": "description is required"}), 400)
    if len(description) > 50000:
        return None, (jsonify({"error": "description must be <= 50000 characters"}), 400)

    raw_price = data.get('price')
    if raw_price is None:
        return None, (jsonify({"error": "price is required"}), 400)
    try:
        price = Decimal(str(raw_price))
        if not price.is_finite() or price < Decimal(str(Config.MIN_TASK_AMOUNT)):
            return None, (jsonify({"error": f"price must be >= {Config.MIN_TASK_AMOUNT}"}), 400)
    except (InvalidOperation, ValueError, TypeError):
        return None, (jsonify({"error": "Invalid price value"}), 400)
    if price > Decimal('1000000'):
        return None, (jsonify({"error": f"price must be <= 1,000,000 {Config.XLAYER_TOKEN_SYMBOL}"}), 400)

    rubric = data.get('rubric')
    if rubric and len(rubric) > 10000:
        return None, (jsonify({"error": "rubric must be <= 10000 characters"}), 400)

    return {"title": title, "description": description, "price": price, "rubric": rubric}, None


# Initialize on first request (lazy — avoids import-time side effects in tests)
_x402_initialized = False
_x402_init_lock = threading.Lock()


@app.before_request
def _ensure_x402_init():
    global _x402_initialized
    if not _x402_initialized:
        with _x402_init_lock:
            if not _x402_initialized:
                _init_x402()
                _x402_initialized = True


# Silent blacklist — covers both Wallet auth and Bearer API key auth.
# Blacklisted addresses receive the same message regardless of auth method.
_BLACKLIST_MSG = (
    "The platform is currently under testing. Your request frequency has been flagged as abnormal. "
    "Normal access will be restored once testing is complete."
)

@app.before_request
def _check_blacklist():
    from config import Config
    if not Config.BLACKLIST_ADDRESSES:
        return
    auth = request.headers.get('Authorization', '')
    address = None

    if auth.startswith('Wallet '):
        parts = auth[7:].split(':', 2)
        if parts:
            address = parts[0].lower()

    elif auth.startswith('Bearer '):
        import hashlib
        from models import Agent as _Agent
        key_hash = hashlib.sha256(auth[7:].encode()).hexdigest()
        agent = _Agent.query.filter_by(api_key_hash=key_hash).first()
        if agent and agent.wallet_address:
            address = agent.wallet_address.lower()

    if address and address in Config.BLACKLIST_ADDRESSES:
        return make_response(_BLACKLIST_MSG, 200)


# G14: Correlation ID — attach unique request ID to every request
@app.before_request
def _attach_request_id():
    import uuid as _uuid
    rid = request.headers.get('X-Request-ID') or str(_uuid.uuid4())
    g.request_id = rid

@app.after_request
def _add_request_id_header(response):
    rid = getattr(g, 'request_id', None)
    if rid:
        response.headers['X-Request-ID'] = rid
    return response

@app.after_request
def _add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

@app.after_request
def _invalidate_dashboard_cache(response):
    """Invalidate dashboard caches after successful mutating requests."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and response.status_code < 400:
        from services.dashboard_service import DashboardService
        DashboardService.invalidate_caches()
        logger.info("dashboard cache invalidated: %s %s → %d",
                     request.method, request.path, response.status_code)
    return response

@app.errorhandler(Exception)
def handle_unexpected_error(e):
    logger.exception("Unhandled exception: %s", e)
    try:
        db.session.rollback()
    except Exception:
        pass
    return jsonify({"error": "Internal server error"}), 500

# Thread pool for oracle evaluations with timeout support (G07)
class _ScheduledExecutor:
    """G18: Wrapper around ThreadPoolExecutor with error recovery and dead-letter logging."""
    def __init__(self, max_workers=4):
        self._max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='oracle')
        self._dead_letters = []  # Recent failures for monitoring
        self._lock = threading.Lock()

    def ensure_pool(self):
        """Recreate pool if needed (for test re-use after teardown)."""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix='oracle',
            )

    def submit(self, fn, *args, **kwargs):
        self.ensure_pool()
        return self._pool.submit(fn, *args, **kwargs)

    def record_failure(self, submission_id: str, error: str):
        with self._lock:
            self._dead_letters.append({
                "submission_id": submission_id,
                "error": error,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            # Keep only last 100 failures
            if len(self._dead_letters) > 100:
                self._dead_letters = self._dead_letters[-100:]

    def shutdown(self, wait=True):
        """Shutdown the executor."""
        if self._pool:
            self._pool.shutdown(wait=wait)
            self._pool = None

    @property
    def dead_letters(self):
        with self._lock:
            return list(self._dead_letters)

_oracle_executor = _ScheduledExecutor(max_workers=4)

# Graceful shutdown signal for background threads
_shutdown_event = threading.Event()

# Wire shutdown event to webhook service so delivery threads can check it
from services.webhook_service import set_shutdown_event
set_shutdown_event(_shutdown_event)

import time as _time_mod  # needed for monotonic()
_pending_oracles = {}  # submission_id -> (future, start_time, timeout_seconds)
_pending_lock = threading.Lock()


# ---------------------------------------------------------------------------
# G12: Auto-refund helper (used by expiry checker, sweep, cancel)
# ---------------------------------------------------------------------------

def _auto_refund(job, label="auto"):
    """Attempt on-chain refund with pending marker, per-job commit, 3 retries.

    Uses ChainRegistry to resolve the appropriate adapter for the job's chain.

    Safety guarantees:
    - Sets refund_tx_hash='pending' + commit BEFORE sending (cross-path mutex)
    - Commits immediately after each successful send (no batched commits)
    - adapter.refund() returns RefundResult with .tx_hash and .error
    - Resets pending marker on final failure

    Returns tx_hash on success, or None on failure.
    """
    import time as _time

    if not (job.deposit_tx_hash and job.depositor_address):
        return None
    if job.refund_tx_hash:  # already refunded or in-progress
        return None

    refund_amount = job.deposit_amount if job.deposit_amount is not None else job.price
    if not refund_amount or refund_amount <= 0:
        return None

    adapter = None
    if _chain_registry:
        try:
            adapter = _chain_registry.get_or_default(job.chain_id)
        except (ValueError, RuntimeError):
            adapter = None
    if not adapter or not adapter.is_connected():
        return None

    # Atomic pending marker — conditional UPDATE prevents concurrent refund
    # from any other path (expiry, sweep, cancel, manual /refund endpoint).
    # Only succeeds if refund_tx_hash is still NULL in the database.
    updated = Job.query.filter_by(
        task_id=job.task_id,
    ).filter(
        Job.refund_tx_hash.is_(None),
    ).update({'refund_tx_hash': 'pending'}, synchronize_session='fetch')
    db.session.commit()
    if not updated:
        logger.info("%s refund skipped for job %s: another path claimed it", label, job.task_id)
        return None

    for attempt in range(1, 4):
        try:
            result = adapter.refund(job.depositor_address, refund_amount)
            if result.error:
                raise RuntimeError(result.error)
            tx = result.tx_hash
            job.refund_tx_hash = tx
            db.session.commit()
            logger.info(
                "%s refund for job %s: tx=%s amount=%s",
                label, job.task_id, tx, refund_amount,
            )
            return tx
        except Exception as e:
            if attempt < 3:
                logger.warning(
                    "%s refund attempt %d/3 failed for job %s: %s",
                    label, attempt, job.task_id, e,
                )
                _time.sleep(2 ** attempt)
            else:
                logger.error(
                    "%s refund failed after 3 attempts for job %s: %s",
                    label, job.task_id, e,
                )
                # Reset pending marker so sweep can retry later
                job.refund_tx_hash = None
                db.session.commit()
    return None


# ---------------------------------------------------------------------------
# G12: Proactive expiry checker (background loop)
# ---------------------------------------------------------------------------

def _expiry_checker_loop():
    """Background thread: check for expired funded jobs every 60 seconds."""
    import time
    consecutive_errors = 0
    while not _shutdown_event.is_set():
        try:
            # m6 fix: Exponential backoff on consecutive errors (60s, 120s, 240s, max 600s)
            sleep_time = min(60 * (2 ** consecutive_errors), 600)
            if _shutdown_event.wait(timeout=sleep_time):
                break  # Shutdown requested during sleep
            with app.app_context():
                try:
                    from services.job_service import JobService
                    now = datetime.datetime.now(datetime.timezone.utc)
                    expired_jobs = Job.query.filter(
                        Job.status == 'funded',
                        Job.expiry.isnot(None),
                        Job.expiry < now,
                    ).all()
                    for job in expired_jobs:
                        if JobService.check_expiry(job):
                            logger.info("Proactively expired job %s", job.task_id)
                            from services.webhook_service import fire_event
                            fire_event('job.expired', job.task_id, {"status": "expired"})

                            # G12-refund: Auto-refund expired funded jobs
                            _auto_refund(job, label="expiry")
                    if expired_jobs:
                        db.session.commit()  # persist status changes

                    # G12-refund-sweep: Catch expired/cancelled jobs that missed auto-refund
                    unrefunded = Job.query.filter(
                        Job.status.in_(('expired', 'cancelled')),
                        Job.deposit_tx_hash.isnot(None),
                        Job.depositor_address.isnot(None),
                        Job.refund_tx_hash.is_(None),
                    ).all()
                    if unrefunded:
                        for job in unrefunded:
                            _auto_refund(job, label="sweep")

                    # Clean up expired idempotency keys (24h TTL)
                    from models import IdempotencyKey
                    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
                    deleted = IdempotencyKey.query.filter(
                        IdempotencyKey.created_at < cutoff
                    ).delete(synchronize_session=False)
                    if deleted:
                        db.session.commit()
                        logger.info("Cleaned up %d expired idempotency keys", deleted)
                finally:
                    db.session.remove()
            consecutive_errors = 0  # Reset on success
        except Exception as e:
            consecutive_errors += 1
            logger.error("Expiry checker error (consecutive=%d): %s", consecutive_errors, e)

_expiry_thread = None  # started by _start_background_threads()


# ---------------------------------------------------------------------------
# G07: Timeout monitor — single background thread replaces per-submission wrappers
# ---------------------------------------------------------------------------

def _mark_submission_timed_out(submission_id):
    """Mark a submission as timed out. Called by timeout monitor."""
    timeout = Config.ORACLE_TIMEOUT_SECONDS
    with app.app_context():
        try:
            sub = db.session.query(Submission).filter_by(
                id=submission_id
            ).with_for_update().first()
            if sub and sub.status == 'judging':
                sub.status = 'failed'
                sub.oracle_score = 0
                sub.oracle_reason = f"Evaluation timed out after {timeout}s"
                sub.oracle_steps = [{"step": 0, "name": "timeout", "output": {"error": "timeout"}}]
                db.session.commit()
                logger.warning("Oracle timeout for submission %s after %ds", submission_id, timeout)
        except Exception as e:
            logger.error("Error marking submission %s as timed out: %s", submission_id, e)
        finally:
            db.session.remove()
    _oracle_executor.record_failure(submission_id, f"timeout after {timeout}s")


def _timeout_monitor_loop():
    """Background thread: check for timed-out oracle evaluations every 5 seconds."""
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=5):
            break
        with _pending_lock:
            now = _time_mod.monotonic()
            expired = [
                (sid, fut) for sid, (fut, start, tout) in _pending_oracles.items()
                if now - start > tout
            ]
        for sid, fut in expired:
            fut.cancel()  # best-effort cancel
            _mark_submission_timed_out(sid)
            with _pending_lock:
                _pending_oracles.pop(sid, None)
        # Clean up completed futures
        with _pending_lock:
            done = [sid for sid, (fut, _, _) in _pending_oracles.items() if fut.done()]
            for sid in done:
                _pending_oracles.pop(sid)


_timeout_monitor = None  # started by _start_background_threads()


def _start_background_threads():
    """Start (or restart) daemon threads. Safe to call multiple times.

    Called once at module load and again by test fixtures after teardown.
    """
    global _timeout_monitor, _expiry_thread
    # Timeout monitor
    if _timeout_monitor is None or not _timeout_monitor.is_alive():
        _timeout_monitor = threading.Thread(
            target=_timeout_monitor_loop, daemon=True, name='oracle-timeout-monitor')
        _timeout_monitor.start()
    # Expiry checker
    if _expiry_thread is None or not _expiry_thread.is_alive():
        _expiry_thread = threading.Thread(
            target=_expiry_checker_loop, daemon=True, name='expiry-checker')
        _expiry_thread.start()


_start_background_threads()


def _atexit_shutdown():
    """Graceful cleanup: signal daemon threads to stop and drain pools."""
    _shutdown_event.set()
    _oracle_executor.shutdown(wait=False)
    try:
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
    except Exception:
        pass

atexit.register(_atexit_shutdown)

# ---------------------------------------------------------------------------
# Oracle background thread
# ---------------------------------------------------------------------------


def _run_oracle(app, submission_id):
    """Background thread: guard check + 6-step oracle evaluation."""
    if _shutdown_event.is_set():
        return
    with app.app_context():
        try:
            # C1 fix: Re-read with lock to prevent race with timeout handler
            sub = db.session.query(Submission).filter_by(id=submission_id).with_for_update().first()
            if not sub or sub.status != 'judging':
                return
            job = db.session.get(Job, sub.task_id)
            if not job:
                return

            # Step 1: Guard
            from services.oracle_guard import OracleGuard
            guard = OracleGuard()

            # P1-2 fix (M-O03): Guard scans rubric and description for injection
            if job.rubric:
                rubric_guard = guard.check_rubric(job.rubric)
                if rubric_guard['blocked']:
                    sub.status = 'failed'
                    sub.oracle_score = 0
                    sub.oracle_reason = f"Blocked by guard (rubric injection): {rubric_guard['reason']}"
                    sub.oracle_steps = [{"step": 1, "name": "guard_rubric", "output": rubric_guard}]
                    db.session.commit()
                    return

            if job.description:
                desc_guard = guard.check_rubric(job.description)
                if desc_guard['blocked']:
                    sub.status = 'failed'
                    sub.oracle_score = 0
                    sub.oracle_reason = f"Blocked by guard (description injection): {desc_guard['reason']}"
                    sub.oracle_steps = [{"step": 1, "name": "guard_description", "output": desc_guard}]
                    db.session.commit()
                    return

            text_for_guard = (
                json.dumps(sub.content, ensure_ascii=False)
                if isinstance(sub.content, dict)
                else str(sub.content)
            )
            guard_result = guard.check(text_for_guard)

            if guard_result['blocked']:
                sub.status = 'failed'
                sub.oracle_score = 0
                sub.oracle_reason = f"Blocked by guard: {guard_result['reason']}"
                sub.oracle_steps = [{"step": 1, "name": "guard", "output": guard_result}]
                db.session.commit()
                return

            # Steps 2-6: Oracle evaluation
            from services.oracle_service import OracleService
            oracle = OracleService()
            result = oracle.evaluate(job.title, job.description, job.rubric, sub.content)

            # Don't write results during shutdown
            if _shutdown_event.is_set():
                return

            # C1 fix: Re-check status under lock before writing results
            # (timeout handler may have set status='failed' while we were evaluating)
            sub = db.session.query(Submission).filter_by(id=submission_id).with_for_update().first()
            if not sub or sub.status != 'judging':
                return  # Timeout handler already marked this submission

            sub.oracle_score = result['score']
            sub.oracle_reason = result['reason']
            sub.oracle_steps = (
                [{"step": 1, "name": "guard", "output": guard_result}] + result['steps']
            )

            if result['verdict'] == 'RESOLVED':
                # C4 fix: Atomic resolve FIRST, then mark submission
                updated = Job.query.filter_by(
                    task_id=sub.task_id, status='funded'
                ).update({
                    'status': 'resolved',
                    'winner_id': sub.worker_id,
                    'result_data': sub.content,
                })

                if updated:
                    sub.status = 'passed'
                    # H7: Discard other in-flight submissions
                    Submission.query.filter(
                        Submission.task_id == sub.task_id,
                        Submission.id != sub.id,
                        Submission.status.in_(['pending', 'judging']),
                    ).update({
                        'status': 'failed',
                        'oracle_score': 0,
                        'oracle_reason': 'Another submission was accepted for this task',
                    }, synchronize_session='fetch')

                    # This submission won — attempt payout (G06: track status)
                    # P0-3 fix (C-06): Lock Job row before payout to prevent race with cancel
                    job_obj = db.session.query(Job).filter_by(
                        task_id=sub.task_id
                    ).with_for_update().first()

                    if not job_obj or job_obj.status != 'resolved':
                        # State changed concurrently (e.g., cancelled), abort payout
                        sub.status = 'failed'
                        sub.oracle_score = 0
                        sub.oracle_reason = "Job state changed during payout preparation"
                        db.session.commit()
                        return

                    worker = db.session.get(Agent, sub.worker_id)
                    if worker and worker.wallet_address:
                        adapter = None
                        if _chain_registry:
                            try:
                                adapter = _chain_registry.get_or_default(job_obj.chain_id)
                            except (ValueError, RuntimeError):
                                adapter = None
                        if adapter and adapter.is_connected():
                            job_obj.payout_status = 'pending'
                            db.session.flush()
                            # G06-retry: Retry payout up to 3 times with exponential backoff
                            # to handle transient RPC rate-limits / timeouts
                            import time as _time
                            max_attempts = 3
                            last_error = None
                            for attempt in range(1, max_attempts + 1):
                                try:
                                    # G19: Use per-job fee_bps
                                    fee_bps = job_obj.fee_bps if job_obj.fee_bps is not None else Config.PLATFORM_FEE_BPS
                                    payout_result = adapter.payout(worker.wallet_address, job_obj.price, fee_bps)
                                    job_obj.payout_tx_hash = payout_result.payout_tx
                                    job_obj.fee_tx_hash = payout_result.fee_tx or None

                                    # P0-1 fix (C-03): Check pending status
                                    if payout_result.pending:
                                        job_obj.payout_status = 'pending_confirmation'
                                        logger.warning(
                                            "Payout pending confirmation for job %s: %s",
                                            sub.task_id, payout_result.error or 'receipt timeout'
                                        )
                                    # P0-1 fix (C-02): Check fee_error
                                    elif payout_result.fee_error:
                                        job_obj.payout_status = 'partial'
                                        logger.error(
                                            "Partial settlement for job %s: worker paid, fee failed: %s",
                                            sub.task_id, payout_result.fee_error
                                        )
                                    else:
                                        job_obj.payout_status = 'success'

                                    job_obj.payout_error = None  # Clear on success

                                    # Only count worker earnings when not pending
                                    if not payout_result.pending:
                                        worker_share = Decimal(10000 - fee_bps) / Decimal(10000)
                                        worker.total_earned = (
                                            (worker.total_earned or 0) + job_obj.price * worker_share
                                        )
                                    last_error = None
                                    break  # success — exit retry loop
                                except Exception as e:
                                    last_error = e
                                    if attempt < max_attempts:
                                        backoff = 2 ** attempt  # 2s, 4s
                                        logger.warning(
                                            "Payout attempt %d/%d failed for job %s: %s — retrying in %ds",
                                            attempt, max_attempts, sub.task_id, e, backoff,
                                        )
                                        _time.sleep(backoff)
                                    else:
                                        logger.error(
                                            "Payout failed after %d attempts for submission %s: %s",
                                            max_attempts, sub.id, e,
                                        )
                            if last_error:
                                job_obj.payout_status = 'failed'
                                job_obj.payout_error = str(last_error)
                        else:
                            job_obj.payout_status = 'skipped'
                    else:
                        if job_obj:
                            job_obj.payout_status = 'skipped'

                    # G04: Fire webhook for resolve
                    from services.webhook_service import fire_event
                    fire_event('job.resolved', sub.task_id, {
                        "status": "resolved",
                        "winner_id": sub.worker_id,
                        "score": result['score'],
                    })

                    # Update reputation
                    from services.agent_service import AgentService
                    AgentService.update_reputation(sub.worker_id)
                else:
                    # C4: Job was no longer funded (cancelled/expired during evaluation)
                    sub.status = 'failed'
                    sub.oracle_score = 0
                    sub.oracle_reason = "Job was no longer in funded state"
            else:
                sub.status = 'failed'
                # Increment failure count
                job_obj = db.session.get(Job, sub.task_id)
                if job_obj:
                    job_obj.failure_count = (job_obj.failure_count or 0) + 1

                # Update reputation on failure too
                from services.agent_service import AgentService
                AgentService.update_reputation(sub.worker_id)

            db.session.commit()

            # G04: Fire webhook for submission result
            from services.webhook_service import fire_event
            fire_event('submission.completed', sub.task_id, {
                "submission_id": sub.id,
                "worker_id": sub.worker_id,
                "status": sub.status,
                "score": sub.oracle_score,
            })
        except Exception as e:
            sub.status = 'failed'
            sub.oracle_score = 0
            # M8: Don't leak internal error details to client
            sub.oracle_reason = "Internal processing error"
            sub.oracle_steps = [{"step": 0, "name": "error", "output": {"error": "internal"}}]
            logger.exception("Oracle exception for submission %s", sub.id)
            _oracle_executor.record_failure(submission_id, str(e))
            db.session.commit()
        finally:
            db.session.remove()


def _launch_oracle_with_timeout(submission_id):
    """Submit oracle evaluation to thread pool with timeout tracking (G07)."""
    # F08: Prevent unbounded queue
    _oracle_executor.ensure_pool()
    pending = _oracle_executor._pool._work_queue.qsize() if _oracle_executor._pool else 0
    if pending >= Config.ORACLE_QUEUE_MAX:
        logger.warning("Oracle queue saturated (%d pending), rejecting submission %s", pending, submission_id)
        with app.app_context():
            try:
                sub = db.session.query(Submission).filter_by(id=submission_id).first()
                if sub and sub.status == 'judging':
                    sub.status = 'failed'
                    sub.oracle_score = 0
                    sub.oracle_reason = "Oracle evaluation queue full — please retry later"
                    db.session.commit()
            finally:
                db.session.remove()
        _oracle_executor.record_failure(submission_id, "queue saturated")
        return

    future = _oracle_executor.submit(_run_oracle, app, submission_id)
    with _pending_lock:
        _pending_oracles[submission_id] = (future, _time_mod.monotonic(), Config.ORACLE_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# G17: Idempotency key helper
# ---------------------------------------------------------------------------


def check_idempotency():
    """Check for Idempotency-Key header. Returns cached response if found, else None.
    M5: Respects 24h TTL. M10: Requires authenticated agent (no 'anon' fallback)."""
    idem_key = request.headers.get('Idempotency-Key')
    if not idem_key:
        return None
    agent_id = getattr(g, 'current_agent_id', None)
    if not agent_id:
        return None  # M10: Skip idempotency for unauthenticated requests
    full_key = f"{agent_id}:{idem_key}"
    cached = db.session.get(IdempotencyKey, full_key)
    if cached:
        if cached.is_expired:
            # M5: Expired — remove and allow re-use
            db.session.delete(cached)
            db.session.commit()
            return None
        return jsonify(cached.response_body), cached.response_code
    return None


def save_idempotency(response_tuple):
    """Save response for the current idempotency key."""
    idem_key = request.headers.get('Idempotency-Key')
    if not idem_key:
        return
    agent_id = getattr(g, 'current_agent_id', None)
    if not agent_id:
        return
    full_key = f"{agent_id}:{idem_key}"
    body, code = response_tuple
    try:
        entry = IdempotencyKey(
            key=full_key,
            agent_id=agent_id,
            response_code=code,
            response_body=body.get_json(),
        )
        db.session.add(entry)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


# ---------------------------------------------------------------------------
# Helper: optional auth viewer extraction (G16)
# ---------------------------------------------------------------------------


def _get_viewer_id() -> str:
    """Extract agent_id from auth header if present, without requiring auth.

    Supports both Bearer API key and Wallet signature.
    """
    agent = authenticate_request()
    return agent.agent_id if agent else None


# ---------------------------------------------------------------------------
# Helper: submission serialiser
# ---------------------------------------------------------------------------


def _sanitize_oracle_steps(steps):
    """Return only step name + pass/fail, hide full LLM outputs."""
    if not steps:
        return steps
    sanitized = []
    for s in steps:
        output = s.get("output")
        if isinstance(output, dict):
            verdict = output.get("verdict")
            if verdict:
                # Explicit verdict exists — use it
                passed = verdict not in ("CLEAR_FAIL", "REJECTED", "BLOCKED")
            else:
                # No verdict field (e.g., guard step uses "blocked" key)
                blocked = output.get("blocked")
                if blocked is not None:
                    passed = not blocked
                else:
                    passed = None
        else:
            passed = None
        sanitized.append({
            "step": s.get("step"),
            "name": s.get("name"),
            "passed": passed,
        })
    return sanitized


def _check_submission_access(sub, job, viewer_id):
    """Determine if viewer can see submission content.
    Returns:
        True  -- show content
        False -- hide content (task not funded)
        None  -- payment required (x402)
    """
    if viewer_id and viewer_id == sub.worker_id:
        return True
    if job.status in ('resolved', 'expired', 'cancelled'):
        return True
    if viewer_id and SubmissionAccess.query.filter_by(
            submission_id=sub.id, viewer_agent_id=viewer_id).first():
        return True
    if job.status == 'funded':
        return None
    return False


def _submission_to_dict(sub: Submission, viewer_id: str = None,
                        public_content: bool = False,
                        show_content: bool = None,
                        job: Job = None) -> dict:
    """Serialize submission. Uses _check_submission_access for visibility.

    Pass `job` to avoid per-row DB lookup when called in a loop.
    """
    if show_content is None:
        if public_content:
            show_content = True
        else:
            if job is None:
                job = db.session.get(Job, sub.task_id)
            access = _check_submission_access(sub, job, viewer_id)
            show_content = (access is True)

    result = {
        "submission_id": sub.id,
        "task_id": sub.task_id,
        "worker_id": sub.worker_id,
        "status": sub.status,
        "oracle_score": sub.oracle_score,
        "oracle_reason": sub.oracle_reason,
        "oracle_steps": _sanitize_oracle_steps(sub.oracle_steps),
        "attempt": sub.attempt,
        "created_at": utc_iso(sub.created_at),
    }
    if show_content:
        result["content"] = sub.content
    else:
        result["content"] = "[redacted]"
    return result


# ===================================================================
# 1. GET /health
# ===================================================================


@app.route('/health', methods=['GET'])
def health():
    result = {"status": "healthy", "service": "synai-relay-v2"}
    return jsonify(result), 200


# ===================================================================
# 2. GET /platform/deposit-info
# ===================================================================


@app.route('/platform/deposit-info', methods=['GET'])
def deposit_info():
    from services.wallet_service import get_wallet_service
    wallet = get_wallet_service()

    chain_id = Config.DEFAULT_CHAIN_ID
    chain_name = "xlayer" if chain_id == 196 else "base"
    usdc = Config.XLAYER_USDC_CONTRACT if chain_id == 196 else app.config.get('USDC_CONTRACT', '')

    resp = {
        "operations_wallet": wallet.get_ops_address(),
        "usdc_contract": usdc,
        "chain": chain_name,
        "chain_id": chain_id,
        "min_amount": app.config.get('MIN_TASK_AMOUNT', 0.1),
        "chain_connected": wallet.is_connected(),
        "gas_estimate": None,
    }

    # Provide real-time gas estimation for Buyer/Worker
    if wallet.is_connected():
        from decimal import Decimal
        gas_info = wallet.estimate_gas(wallet.get_ops_address(), Decimal('1.0'))
        if 'error' not in gas_info:
            resp["gas_estimate"] = {
                "gas_limit": gas_info["gas_limit"],
                "gas_price_gwei": gas_info["gas_price_gwei"],
                "estimated_cost_eth": gas_info["estimated_cost_eth"],
                "note": f"Real-time estimate for a {Config.XLAYER_TOKEN_SYMBOL if chain_id == 196 else 'USDC'} transfer on {chain_name}. "
                        "Fetch latest before sending your deposit transaction.",
            }

    return jsonify(resp), 200


# ===================================================================
# 2b. GET /platform/chains — list supported chains
# ===================================================================


@app.route('/platform/chains', methods=['GET'])
def platform_chains():
    if _chain_registry:
        return jsonify({
            "chains": _chain_registry.supported_chains(),
            "default_chain_id": Config.DEFAULT_CHAIN_ID,
        }), 200
    return jsonify({"chains": [], "default_chain_id": Config.DEFAULT_CHAIN_ID}), 200


# ===================================================================
# 3. POST /agents — register agent
# ===================================================================


@app.route('/agents', methods=['POST'])
@rate_limit()  # C2: Rate limit registration to prevent DoS
def register_agent():
    from services.agent_service import AgentService

    data = request.get_json(silent=True) or {}
    agent_id = data.get('agent_id')
    name = data.get('name')
    wallet_address = data.get('wallet_address')

    if not agent_id:
        return jsonify({"error": "agent_id is required"}), 400

    # C2: Validate agent_id format
    if not re.match(r'^[a-zA-Z0-9_-]{3,100}$', agent_id):
        return jsonify({"error": "agent_id must be 3-100 alphanumeric/hyphen/underscore characters"}), 400

    if name and (not isinstance(name, str) or len(name) > 100):
        return jsonify({"error": "name must be a string of 100 characters or less"}), 400

    # M6: Validate wallet before calling service
    if wallet_address and not re.match(r'^0x[0-9a-fA-F]{40}$', wallet_address):
        return jsonify({"error": "Invalid wallet address format"}), 400

    result = AgentService.register(agent_id, name, wallet_address)

    # AgentService.register returns a dict with "error" key if already exists
    if "error" in result:
        return jsonify(result), 409

    # G01: Generate API key and store hash
    raw_key, key_hash = generate_api_key()
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if agent:
        agent.api_key_hash = key_hash
        db.session.commit()

    response = {"status": "registered", "api_key": raw_key, **result}
    logger.info("Agent registered: %s (name=%s) — total agents now: %d",
                agent_id, name,
                db.session.query(Agent).count())

    # G20: Wallet warning
    if not wallet_address:
        response["warnings"] = ["wallet_address not set — payouts will be skipped"]

    return jsonify(response), 201


# ===================================================================
# 4. GET /agents/<agent_id> — get profile
# ===================================================================


@app.route('/agents/<agent_id>', methods=['GET'])
def get_agent(agent_id):
    from services.agent_service import AgentService

    profile = AgentService.get_profile(agent_id)
    if not profile:
        return jsonify({"error": "Agent not found"}), 404
    return jsonify(profile), 200


# ===================================================================
# 4b. PATCH /agents/<agent_id> — update profile (G02)
# ===================================================================


@app.route('/agents/<agent_id>', methods=['PATCH'])
@require_auth
def update_agent(agent_id):
    # Must be the agent themselves
    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot update another agent's profile"}), 403

    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    data = request.get_json(silent=True) or {}

    if 'name' in data:
        name = data['name']
        if not isinstance(name, str) or len(name) < 1 or len(name) > 100:
            return jsonify({"error": "name must be 1-100 characters"}), 400
        agent.name = name

    if 'wallet_address' in data:
        wallet = data['wallet_address']
        if wallet and not re.match(r'^0x[0-9a-fA-F]{40}$', wallet):
            return jsonify({"error": "Invalid wallet address format"}), 400
        agent.wallet_address = wallet

    db.session.commit()

    from services.agent_service import AgentService
    return jsonify(AgentService.get_profile(agent_id)), 200


# ===================================================================
# 4c. POST /agents/<agent_id>/rotate-key — rotate API key (P2-3)
# ===================================================================


@app.route('/agents/<agent_id>/rotate-key', methods=['POST'])
@require_auth
def rotate_api_key(agent_id):
    """P2-3: Rotate API key for an agent."""
    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot rotate another agent's API key"}), 403

    from services.agent_service import AgentService
    result = AgentService.rotate_api_key(agent_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


# ===================================================================
# 5 & 6. /jobs — POST create | GET list
# ===================================================================


@app.route('/jobs', methods=['GET'])
def list_jobs_endpoint():
    return _list_jobs()


@app.route('/jobs', methods=['POST'])
@rate_limit()
def create_job_endpoint():
    if not (app.config.get('X402_ENABLED', False) and _X402_SDK_AVAILABLE):
        # No x402 — require API key or wallet auth
        agent = authenticate_request()
        if not agent:
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        g.current_agent_id = agent.agent_id
        return _create_job()

    data = request.get_json(silent=True) or {}

    # CRITICAL: Validate ALL fields BEFORE x402 settlement to avoid orphaned payments
    fields, err = _validate_job_fields(data)
    if err:
        return err
    price = fields["price"]

    # Check for x402 payment header
    payment_header = (request.headers.get(PAYMENT_SIGNATURE_HEADER)
                      or request.headers.get(X_PAYMENT_HEADER))

    if not payment_header:
        # No payment: return 402 with requirements (only chains with active facilitators)
        requirements = build_requirements(
            price,
            Config.OPERATIONS_WALLET_ADDRESS or '',
            _x402_enabled_adapters(),
        )
        payment_required = PaymentRequired(accepts=requirements)
        resp = jsonify({
            "error": "Payment required",
            "description": f"Task escrow: {price} {Config.XLAYER_TOKEN_SYMBOL}",
        })
        resp.status_code = 402
        resp.headers['PAYMENT-REQUIRED'] = encode_payment_required_header(
            payment_required)
        return resp

    # Has payment header: validate -> verify -> settle -> create funded job
    try:
        payload = decode_payment_signature_header(payment_header)
    except Exception as e:
        return jsonify({"error": f"Invalid payment header: {e}"}), 400

    # Server-side validation: amount and payTo must match expectations
    expected_atomic = str(int(price * Decimal(10 ** 6)))
    expected_pay_to = Config.OPERATIONS_WALLET_ADDRESS or ''
    if payload.accepted.amount != expected_atomic:
        return jsonify({
            "error": "Payment amount mismatch",
            "expected": expected_atomic,
            "actual": payload.accepted.amount,
        }), 402
    if payload.accepted.pay_to.lower() != expected_pay_to.lower():
        return jsonify({
            "error": "Payment recipient mismatch",
        }), 402

    network = payload.accepted.network
    try:
        server = _get_x402_server(network)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    verify_result = server.verify_payment(payload, payload.accepted)
    if not verify_result.is_valid:
        return jsonify({
            "error": "Payment verification failed",
            "reason": verify_result.invalid_reason,
        }), 402

    settle_result = server.settle_payment(payload, payload.accepted)
    if not settle_result.success:
        return jsonify({
            "error": "x402 settlement failed",
            "reason": settle_result.error_reason,
        }), 402

    if not settle_result.payer:
        logger.error("x402 settlement succeeded but payer address missing — cannot track depositor")
        return jsonify({"error": "Settlement missing payer address"}), 500

    # Pure x402: payer wallet = buyer identity (no API key needed)
    buyer_agent = get_or_create_agent_by_wallet(settle_result.payer)
    g.current_agent_id = buyer_agent.agent_id

    # Settlement succeeded — create job as funded
    # CRITICAL: if _create_job() fails after this point, funds are already on-chain.
    # Log enough info for manual reconciliation.
    chain_id = parse_chain_id(settle_result.network)
    sol_price = price * Decimal(Config.SOLUTION_VIEW_FEE_PERCENT) / Decimal(100)

    try:
        resp = _create_job(
            override_status='funded',
            deposit_tx_hash=settle_result.transaction,
            depositor_address=settle_result.payer,
            chain_id=chain_id,
            deposit_amount=price,
            solution_price=sol_price,
        )
    except Exception as e:
        logger.critical(
            "x402 ORPHAN PAYMENT: settlement succeeded but job creation failed! "
            "tx=%s payer=%s amount=%s chain=%s error=%s",
            settle_result.transaction, settle_result.payer, price, chain_id, e)
        db.session.rollback()
        return jsonify({
            "error": "Internal error after payment — contact support",
            "tx_hash": settle_result.transaction,
        }), 500

    # x402 spec: include settlement receipt in response
    if isinstance(resp, tuple):
        response_obj = resp[0]
    else:
        response_obj = resp
    response_obj.headers['PAYMENT-RESPONSE'] = encode_payment_response_header(settle_result)
    return resp


def _create_job(override_status=None, deposit_tx_hash=None,
                depositor_address=None, chain_id=None,
                deposit_amount=None, solution_price=None):
    data = request.get_json(silent=True) or {}

    # buyer_id is the authenticated agent
    buyer_id = g.current_agent_id

    # A4: Delegate shared field validation to _validate_job_fields()
    fields, err = _validate_job_fields(data)
    if err:
        return err
    title = fields["title"]
    description = fields["description"]
    price = fields["price"]
    rubric = fields["rubric"]

    # Optional fields
    artifact_type = data.get('artifact_type', 'GENERAL')
    if not isinstance(artifact_type, str) or len(artifact_type) > 20:
        return jsonify({"error": "artifact_type must be a string of 20 characters or less"}), 400

    expiry = None
    raw_expiry = data.get('expiry')
    if raw_expiry is not None:
        try:
            expiry = datetime.datetime.fromtimestamp(int(raw_expiry), tz=datetime.timezone.utc)
        except (ValueError, TypeError, OSError):
            return jsonify({"error": "Invalid expiry timestamp"}), 400
        if expiry <= datetime.datetime.now(datetime.timezone.utc):
            return jsonify({"error": "expiry must be in the future"}), 400

    max_submissions = data.get('max_submissions', 20)
    if not isinstance(max_submissions, int) or max_submissions < 1:
        max_submissions = 20

    max_retries = data.get('max_retries', 3)
    if not isinstance(max_retries, int) or max_retries < 1:
        max_retries = 3

    for key, val in [('max_submissions', max_submissions), ('max_retries', max_retries)]:
        caps = {'max_submissions': 100, 'max_retries': 10}
        if val > caps.get(key, 100):
            return jsonify({"error": f"{key} must be <= {caps[key]}"}), 400

    # G19: Per-job fee_bps from request body (with validation)
    raw_fee_bps = data.get('fee_bps')
    if raw_fee_bps is not None:
        try:
            custom_fee_bps = int(raw_fee_bps)
            if custom_fee_bps < 0 or custom_fee_bps > 10000:
                return jsonify({"error": "fee_bps must be 0-10000"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid fee_bps value"}), 400
    else:
        custom_fee_bps = Config.PLATFORM_FEE_BPS

    status = override_status or 'open'

    job = Job(
        title=title,
        description=description,
        rubric=rubric,
        price=price,
        buyer_id=buyer_id,
        status=status,
        artifact_type=artifact_type,
        expiry=expiry,
        max_submissions=max_submissions,
        max_retries=max_retries,
        fee_bps=custom_fee_bps,
        chain_id=chain_id,
        deposit_tx_hash=deposit_tx_hash,
        depositor_address=depositor_address,
        deposit_amount=deposit_amount,
    )

    if solution_price is not None:
        job.solution_price = solution_price

    db.session.add(job)
    db.session.flush()  # materialise task_id before commit expires attrs
    task_id = job.task_id
    price_float = float(job.price)
    db.session.commit()
    logger.info("Job created: task_id=%s buyer=%s price=%.2f status=%s",
                task_id, buyer_id, price_float, status)

    resp_data = {
        "status": status,
        "task_id": task_id,
        "price": price_float,
    }

    if deposit_tx_hash:
        resp_data["x402_settlement"] = {
            "tx_hash": deposit_tx_hash,
            "chain_id": chain_id,
            "depositor": depositor_address,
            "amount": float(deposit_amount) if deposit_amount else None,
        }

    return jsonify(resp_data), 201


def _list_jobs():
    """G03: Enhanced job listing with filtering, sorting, pagination."""
    from services.job_service import JobService

    status = request.args.get('status')
    buyer_id = request.args.get('buyer_id')
    worker_id = request.args.get('worker_id')
    artifact_type = request.args.get('artifact_type')
    min_price = request.args.get('min_price')
    max_price = request.args.get('max_price')
    _ALLOWED_SORT_FIELDS = {'created_at', 'price', 'expiry'}
    sort_by = request.args.get('sort_by', 'created_at')
    if sort_by not in _ALLOWED_SORT_FIELDS:
        sort_by = 'created_at'
    sort_order = request.args.get('sort_order', 'desc')

    try:
        limit = min(max(1, int(request.args.get('limit', 50))), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0

    jobs, total = JobService.list_jobs(
        status=status, buyer_id=buyer_id, worker_id=worker_id,
        artifact_type=artifact_type, min_price=min_price, max_price=max_price,
        sort_by=sort_by, sort_order=sort_order,
        limit=limit, offset=offset,
    )

    return jsonify({
        "jobs": JobService.to_dict_batch(jobs),
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200


# ===================================================================
# 7. GET /jobs/<task_id> — get job details
# ===================================================================


@app.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(JobService.to_dict(job)), 200


# ===================================================================
# 8. POST /jobs/<task_id>/fund — fund a job
# ===================================================================


@app.route('/jobs/<task_id>/fund', methods=['POST'])
@require_auth
def fund_job(task_id):
    # G17: Idempotency check
    cached = check_idempotency()
    if cached:
        return cached

    from services.job_service import JobService
    from services.wallet_service import get_wallet_service

    # F02: Row lock to prevent double-fund race condition
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    JobService.check_expiry(job)

    if job.status != 'open':
        return jsonify({"error": f"Job not in open state (current: {job.status})"}), 400

    # Auth: must be the buyer
    err = require_buyer(job)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    tx_hash = data.get('tx_hash')
    if not tx_hash:
        return jsonify({"error": "tx_hash is required"}), 400
    if not re.match(r'^0x[0-9a-fA-F]{64}$', tx_hash):
        return jsonify({"error": "tx_hash must be a 66-character hex string (0x + 64 hex chars)"}), 400

    wallet = get_wallet_service()
    overpayment = None
    if wallet.is_connected():
        # On-chain verification
        verify = wallet.verify_deposit(tx_hash, job.price)
        if not verify.get('valid'):
            return jsonify({"error": "Deposit verification failed"}), 400
        # P1-4 fix (M-F01): Verify depositor matches buyer's registered wallet
        buyer = Agent.query.filter_by(agent_id=g.current_agent_id).first()
        depositor = verify.get('depositor', '').lower()
        if buyer and buyer.wallet_address:
            if depositor and depositor != buyer.wallet_address.lower():
                return jsonify({
                    "error": "Deposit must come from your registered wallet address",
                    "expected": buyer.wallet_address,
                    "actual": verify.get('depositor'),
                }), 400

        job.depositor_address = verify.get('depositor')
        job.deposit_amount = verify.get('amount')
        overpayment = verify.get('overpayment')  # G22
    else:
        return jsonify({"error": "Chain not connected"}), 503

    job.status = 'funded'
    job.deposit_tx_hash = tx_hash
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "This transaction has already been used to fund a job"}), 409

    resp_data = {
        "status": "funded",
        "task_id": task_id,
        "tx_hash": tx_hash,
    }
    # G22: Report overpayment in response
    if overpayment:
        resp_data["warnings"] = [
            f"Overpayment of {overpayment} {Config.XLAYER_TOKEN_SYMBOL} detected. "
            f"The full deposited amount will be refunded if the job is cancelled or expires. "
            f"Only the job price ({float(job.price)} {Config.XLAYER_TOKEN_SYMBOL}) will be used for settlement."
        ]
    result = jsonify(resp_data), 200
    save_idempotency(result)
    return result


# ===================================================================
# 9. POST /jobs/<task_id>/claim — worker claims task
# ===================================================================


@app.route('/jobs/<task_id>/claim', methods=['POST'])
@require_auth
def claim_job(task_id):
    # H6: Atomic claim with DB-level locking
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'funded':
        return jsonify({"error": f"Job not claimable (current status: {job.status})"}), 400

    worker_id = g.current_agent_id

    # Self-dealing prevention
    if worker_id == job.buyer_id:
        return jsonify({"error": "Buyer cannot claim their own task"}), 403

    # Worker must be registered
    worker = Agent.query.filter_by(agent_id=worker_id).first()
    if not worker:
        return jsonify({"error": "Worker not registered. POST /agents first."}), 400

    # Check worker not already a participant
    existing = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id, unclaimed_at=None).first()
    if existing:
        return jsonify({"error": "Worker already claimed this task"}), 409

    # F04: Check for previously unclaimed record — reactivate instead of creating new
    previously_unclaimed = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id).filter(
        JobParticipant.unclaimed_at.isnot(None)
    ).first()
    if previously_unclaimed:
        previously_unclaimed.unclaimed_at = None
        previously_unclaimed.claimed_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.commit()
        result = {"status": "claimed", "task_id": task_id, "worker_id": worker_id}
        if not worker.wallet_address:
            result["warnings"] = ["wallet_address not set — payouts will be skipped"]
        return jsonify(result), 200

    # Add to participants (new claim)
    jp = JobParticipant(task_id=task_id, worker_id=worker_id)
    db.session.add(jp)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Worker already claimed this task"}), 409

    result = {
        "status": "claimed",
        "task_id": task_id,
        "worker_id": worker_id,
    }

    # G20: Wallet warning at claim time
    if not worker.wallet_address:
        result["warnings"] = ["wallet_address not set — payouts will be skipped"]

    return jsonify(result), 200


# ===================================================================
# 9b. POST /jobs/<task_id>/unclaim — worker withdraws (G05)
# ===================================================================


@app.route('/jobs/<task_id>/unclaim', methods=['POST'])
@require_auth
def unclaim_job(task_id):
    # M3 fix: Use row lock to match claim_job pattern
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    worker_id = g.current_agent_id

    if job.status not in ('funded',):
        return jsonify({"error": f"Cannot unclaim from job in {job.status} state"}), 400

    jp = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id, unclaimed_at=None).first()
    if not jp:
        return jsonify({"error": "Worker has not claimed this task"}), 400

    # Block unclaim if worker has active judging submissions
    active_judging = Submission.query.filter(
        Submission.task_id == task_id,
        Submission.worker_id == worker_id,
        Submission.status == 'judging',
    ).count()
    if active_judging > 0:
        return jsonify({"error": "Cannot unclaim: submissions are being judged"}), 400

    # Cancel any pending submissions from this worker
    Submission.query.filter(
        Submission.task_id == task_id,
        Submission.worker_id == worker_id,
        Submission.status == 'pending',
    ).update({'status': 'failed'}, synchronize_session='fetch')

    jp.unclaimed_at = datetime.datetime.now(datetime.timezone.utc)
    db.session.commit()

    return jsonify({
        "status": "unclaimed",
        "task_id": task_id,
        "worker_id": worker_id,
    }), 200


# ===================================================================
# 10. POST /jobs/<task_id>/submit — worker submits result
# ===================================================================


@app.route('/jobs/<task_id>/submit', methods=['POST'])
@require_auth
@rate_limit(get_submit_limiter())
def submit_result(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'funded':
        return jsonify({
            "error": f"Job not accepting submissions (current status: {job.status})"
        }), 400

    data = request.get_json(silent=True) or {}
    worker_id = g.current_agent_id
    content = data.get('content')

    if content is None:
        return jsonify({"error": "content is required"}), 400

    # C3: Enforce 50KB content size limit
    content_str = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else str(content)
    if len(content_str.encode('utf-8')) > Config.SUBMISSION_MAX_SIZE_BYTES:
        return jsonify({"error": "Submission content exceeds 50KB limit"}), 400

    # Worker must be a participant
    jp = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id, unclaimed_at=None).first()
    if not jp:
        return jsonify({"error": "Worker has not claimed this task"}), 403

    # H10: Check submission limits within a locked read
    job_for_submit = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job_for_submit or job_for_submit.status != 'funded':
        return jsonify({"error": "Job is no longer accepting submissions"}), 409
    total_submissions = Submission.query.filter_by(task_id=task_id).count()
    if total_submissions >= (job_for_submit.max_submissions or 20):
        return jsonify({"error": "Task has reached maximum submissions"}), 400

    # Check max_retries per worker
    # max_retries means "N retries after the initial attempt" → total = max_retries + 1
    worker_submissions = Submission.query.filter_by(
        task_id=task_id, worker_id=worker_id
    ).count()
    if worker_submissions > (job.max_retries or 3):
        return jsonify({"error": "Maximum retries reached for this worker"}), 400

    # Create submission with status 'judging' before starting thread
    attempt_number = worker_submissions + 1
    sub = Submission(
        task_id=task_id,
        worker_id=worker_id,
        content=content,
        status='judging',
        attempt=attempt_number,
    )
    db.session.add(sub)
    db.session.commit()

    # Launch oracle with timeout (G07)
    _launch_oracle_with_timeout(sub.id)

    return jsonify({
        "status": "judging",
        "submission_id": sub.id,
        "attempt": sub.attempt,
    }), 202


# ===================================================================
# 11. GET /jobs/<task_id>/submissions — list submissions for a task
# ===================================================================


@app.route('/jobs/<task_id>/submissions', methods=['GET'])
def list_submissions(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    viewer_id = _get_viewer_id()

    try:
        limit = min(max(1, int(request.args.get('limit', 50))), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0

    public = request.args.get('public', '').lower() == 'true'

    query = Submission.query.filter_by(task_id=task_id).order_by(
        Submission.created_at.asc()
    )
    total = query.count()
    subs = query.offset(offset).limit(limit).all()

    return jsonify({
        "submissions": [_submission_to_dict(s, viewer_id=viewer_id, public_content=public, job=job) for s in subs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200


# ===================================================================
# 12. GET /submissions/<submission_id> — get submission details
# ===================================================================


@app.route('/submissions/<submission_id>', methods=['GET'])
def get_submission(submission_id):
    sub = db.session.get(Submission, submission_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404

    viewer_id = _get_viewer_id()
    job = db.session.get(Job, sub.task_id)
    access = _check_submission_access(sub, job, viewer_id)
    settle_receipt = None

    if access is None and app.config.get('X402_ENABLED', False) and _X402_SDK_AVAILABLE:
        payment_header = (request.headers.get(PAYMENT_SIGNATURE_HEADER)
                          or request.headers.get(X_PAYMENT_HEADER))

        # Compute effective solution price — if <= 0, grant free access
        sol_price = job.solution_price or Decimal(0)
        if sol_price <= 0:
            sol_price = job.price * Decimal(Config.SOLUTION_VIEW_FEE_PERCENT) / Decimal(100)
        if sol_price <= 0:
            # No price set — grant free access, skip x402
            access = True

        # Pre-fetch submission author (used by both payment and 402 branches)
        author = db.session.get(Agent, sub.worker_id) if access is None else None

        if access is None and payment_header:
            if not viewer_id:
                return jsonify({"error": "Authentication required for x402 payment"}), 401

            try:
                payload = decode_payment_signature_header(payment_header)
            except Exception as e:
                return jsonify({"error": f"Invalid payment header: {e}"}), 400

            expected_atomic = str(int(sol_price * Decimal(10 ** 6)))
            if payload.accepted.amount != expected_atomic:
                return jsonify({
                    "error": "Payment amount mismatch",
                    "expected": expected_atomic,
                    "actual": payload.accepted.amount,
                }), 402

            # Validate recipient — payment must go to the submission author
            expected_pay_to = author.wallet_address if author else ''
            if not expected_pay_to or payload.accepted.pay_to.lower() != expected_pay_to.lower():
                return jsonify({"error": "Payment recipient mismatch"}), 402

            try:
                server = _get_x402_server(payload.accepted.network)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            verify = server.verify_payment(payload, payload.accepted)
            if not verify.is_valid:
                return jsonify({
                    "error": "Payment verification failed",
                    "reason": verify.invalid_reason,
                }), 402

            settle = server.settle_payment(payload, payload.accepted)
            if settle.success:
                try:
                    sa = SubmissionAccess(
                        submission_id=sub.id,
                        viewer_agent_id=viewer_id,
                        tx_hash=settle.transaction,
                        amount=sol_price,
                        chain_id=parse_chain_id(settle.network),
                    )
                    db.session.add(sa)
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                except Exception as e:
                    logger.critical(
                        "x402 ORPHAN PAYMENT (submission access): settlement succeeded "
                        "but DB write failed! tx=%s viewer=%s sub=%s amount=%s error=%s",
                        settle.transaction, viewer_id, sub.id, sol_price, e)
                    db.session.rollback()
                    return jsonify({
                        "error": "Internal error after payment — contact support",
                        "tx_hash": settle.transaction,
                    }), 500
                access = True
                settle_receipt = encode_payment_response_header(settle)
            else:
                return jsonify({
                    "error": "Payment settlement failed",
                    "reason": settle.error_reason,
                }), 402

        elif access is None:
            # No payment header — return 402 with requirements
            if not author or not author.wallet_address:
                return jsonify({
                    "error": "Solution author has no wallet configured; viewing unavailable",
                }), 409

            # Solution view payments go 100% to worker — platform earns from
            # the 20% job escrow fee instead. This incentivizes quality submissions.
            requirements = build_requirements(
                sol_price,
                author.wallet_address,
                _x402_enabled_adapters(),
            )
            payment_required = PaymentRequired(accepts=requirements)
            resp_data = _submission_to_dict(sub, viewer_id, show_content=False)
            resp = jsonify(resp_data)
            resp.status_code = 402
            resp.headers['PAYMENT-REQUIRED'] = encode_payment_required_header(
                payment_required)
            return resp

    resp_data = _submission_to_dict(sub, viewer_id, show_content=(access is True))
    resp = jsonify(resp_data)
    if settle_receipt:
        resp.headers['PAYMENT-RESPONSE'] = settle_receipt
    return resp


# ===================================================================
# 12b. GET /submissions — cross-job submission query (G16)
# ===================================================================


@app.route('/submissions', methods=['GET'])
def list_all_submissions():
    """G16: Cross-job submission query with worker_id filter."""
    worker_id = request.args.get('worker_id')
    if not worker_id:
        return jsonify({"error": "worker_id query parameter is required"}), 400

    viewer_id = _get_viewer_id()

    try:
        limit = min(max(1, int(request.args.get('limit', 50))), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0

    query = Submission.query.filter_by(worker_id=worker_id).order_by(
        Submission.created_at.desc()
    )
    total = query.count()
    subs = query.offset(offset).limit(limit).all()

    # Batch-fetch jobs to avoid N+1 in _submission_to_dict
    task_ids = list({s.task_id for s in subs})
    jobs_by_id = {j.task_id: j for j in Job.query.filter(Job.task_id.in_(task_ids)).all()} if task_ids else {}

    return jsonify({
        "submissions": [_submission_to_dict(s, viewer_id=viewer_id, job=jobs_by_id.get(s.task_id)) for s in subs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200


# ===================================================================
# 13. POST /jobs/<task_id>/cancel — cancel unfunded task
# ===================================================================


@app.route('/jobs/<task_id>/cancel', methods=['POST'])
@require_auth
def cancel_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Auth: must be the buyer
    err = require_buyer(job)
    if err:
        return err

    if job.status not in ('open', 'funded'):
        return jsonify({"error": f"Cannot cancel job in {job.status} state"}), 400

    # C2: Lock the job row to prevent cancel/resolve race
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()

    # Re-check status under lock (may have changed concurrently)
    if job.status not in ('open', 'funded'):
        return jsonify({"error": f"Cannot cancel job in {job.status} state"}), 400

    if job.status == 'funded':
        # Block cancel if any submission is actively being judged
        active_judging = Submission.query.filter(
            Submission.task_id == task_id,
            Submission.status == 'judging',
        ).count()
        if active_judging > 0:
            return jsonify({"error": "Cannot cancel: submissions are being judged"}), 409

        # Cancel pending submissions (judging already confirmed == 0 above)
        Submission.query.filter(
            Submission.task_id == task_id,
            Submission.status == 'pending',
        ).update({'status': 'failed'}, synchronize_session='fetch')

    job.status = 'cancelled'

    # P2-7 fix (m-S01): Auto-refund for cancelled funded jobs
    # Must commit status='cancelled' first so _auto_refund's commit doesn't lose it
    db.session.commit()

    auto_refund_tx = None
    cooldown_blocked, _ = _check_refund_cooldown(job.depositor_address)
    if not cooldown_blocked:
        auto_refund_tx = _auto_refund(job, label="cancel")
    else:
        logger.warning("Auto-refund skipped for job %s: depositor cooldown active", task_id)

    # G04: Fire webhook
    from services.webhook_service import fire_event
    fire_event('job.cancelled', task_id, {"status": "cancelled"})

    result = {"status": "cancelled", "task_id": task_id}
    if auto_refund_tx:
        result["refund_tx_hash"] = auto_refund_tx
        result["refund_status"] = "success"
    elif job.deposit_tx_hash and not job.refund_tx_hash:
        result["refund_status"] = "pending_manual"
        result["message"] = "Automatic refund failed. Use POST /jobs/{task_id}/refund to retry."
    return jsonify(result), 200


# ===================================================================
# 14. POST /jobs/<task_id>/refund — refund funded expired/cancelled task
# ===================================================================


def _check_refund_cooldown(depositor_address):
    """Check if a refund was issued to this address within the last hour.
    Returns (is_blocked, seconds_remaining).
    """
    if not depositor_address:
        return False, 0
    one_hour_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    recent_refund = Job.query.filter(
        Job.depositor_address == depositor_address,
        Job.refund_tx_hash.isnot(None),
        Job.refund_tx_hash != 'pending',
        Job.updated_at >= one_hour_ago,
    ).first()
    if recent_refund:
        updated = recent_refund.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=datetime.timezone.utc)
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
        remaining = max(0, Config.REFUND_COOLDOWN_SECONDS - elapsed)
        return True, int(remaining)
    return False, 0


@app.route('/jobs/<task_id>/refund', methods=['POST'])
@require_auth
def refund_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Auth: must be the buyer
    err = require_buyer(job)
    if err:
        return err

    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400

    # C1: Atomic idempotency check — lock the row to prevent concurrent double-refund
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()

    # Re-check under lock
    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400
    if job.refund_tx_hash:
        return jsonify({"error": "Job already refunded"}), 409

    # Refund cooldown: 1 hour per depositor address
    if job.depositor_address:
        blocked, remaining = _check_refund_cooldown(job.depositor_address)
        if blocked:
            return jsonify({
                "error": "Refund cooldown active for this depositor address",
                "retry_after_seconds": remaining,
            }), 429

    # Mark refund as in-progress before sending (prevents concurrent attempts)
    job.refund_tx_hash = 'pending'
    db.session.flush()

    # Attempt on-chain refund via chain adapter
    adapter = None
    if _chain_registry:
        try:
            adapter = _chain_registry.get_or_default(job.chain_id)
        except (ValueError, RuntimeError):
            adapter = None
    # P1-5 fix (M-F02): Refund actual deposit amount (may include overpayment)
    refund_amount = job.deposit_amount if job.deposit_amount is not None else job.price
    refund_tx = None
    if adapter and adapter.is_connected() and job.depositor_address and job.deposit_tx_hash:
        try:
            refund_result = adapter.refund(job.depositor_address, refund_amount)
            refund_tx = refund_result.tx_hash
            job.refund_tx_hash = refund_tx
        except Exception as e:
            # Rollback the 'pending' marker on failure
            job.refund_tx_hash = None
            db.session.commit()
            logger.error("Refund failed for task %s: %s", task_id, e)
            return jsonify({"error": "Refund processing failed"}), 500

    if not refund_tx:
        # Off-chain mode: mark as refunded without tx
        job.refund_tx_hash = 'off-chain'
    db.session.commit()

    result = {
        "status": "refunded",
        "task_id": task_id,
        "amount": float(refund_amount),
    }
    if refund_tx:
        result["refund_tx_hash"] = refund_tx

    # G04: Fire webhook
    from services.webhook_service import fire_event
    fire_event('job.refunded', task_id, result)

    return jsonify(result), 200


# ===================================================================
# 15. Webhook CRUD — POST/GET/DELETE (G04)
# ===================================================================


@app.route('/agents/<agent_id>/webhooks', methods=['POST'])
@require_auth
def create_webhook(agent_id):
    from services.webhook_service import create_webhook as _create_wh

    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot manage webhooks for another agent"}), 403

    data = request.get_json(silent=True) or {}
    url = data.get('url')
    events = data.get('events', [])

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not url.startswith('https://'):
        return jsonify({"error": "url must use HTTPS"}), 400

    # C1: SSRF protection — validate URL resolves to a public IP
    from services.webhook_service import is_safe_webhook_url
    if not is_safe_webhook_url(url):
        return jsonify({"error": "Webhook URL must resolve to a public IP address"}), 400

    if not isinstance(events, list) or not events:
        return jsonify({"error": "events must be a non-empty list"}), 400

    try:
        result = _create_wh(agent_id, url, events)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result), 201


@app.route('/agents/<agent_id>/webhooks', methods=['GET'])
@require_auth
def list_webhooks(agent_id):
    from services.webhook_service import list_webhooks as _list_wh

    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot view webhooks for another agent"}), 403

    return jsonify(_list_wh(agent_id)), 200


@app.route('/agents/<agent_id>/webhooks/<webhook_id>', methods=['DELETE'])
@require_auth
def delete_webhook(agent_id, webhook_id):
    from services.webhook_service import delete_webhook as _delete_wh

    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot manage webhooks for another agent"}), 403

    if _delete_wh(webhook_id, agent_id):
        return '', 204
    return jsonify({"error": "Webhook not found"}), 404


# ===================================================================
# 16. PATCH /jobs/<task_id> — update job (G11)
# ===================================================================


@app.route('/jobs/<task_id>', methods=['PATCH'])
@require_auth
def update_job(task_id):
    from services.job_service import JobService

    # M4 fix: Lock row to prevent concurrent state changes
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    # Check expiry under lock
    JobService.check_expiry(job)

    err = require_buyer(job)
    if err:
        return err

    data = request.get_json(silent=True) or {}

    if job.status == 'open':
        # Mutable when open: title, description, rubric, expiry, max_submissions, max_retries
        for field in ('title', 'description', 'rubric'):
            if field in data:
                val = data[field]
                if not isinstance(val, str):
                    return jsonify({"error": f"{field} must be a string"}), 400
                limits = {'title': 500, 'description': 50000, 'rubric': 10000}
                if len(val) > limits.get(field, 50000):
                    return jsonify({"error": f"{field} exceeds maximum length"}), 400
                setattr(job, field, val)
        if 'expiry' in data:
            try:
                new_expiry = datetime.datetime.fromtimestamp(int(data['expiry']), tz=datetime.timezone.utc)
            except (ValueError, TypeError, OSError):
                return jsonify({"error": "Invalid expiry timestamp"}), 400
            if new_expiry <= datetime.datetime.now(datetime.timezone.utc):
                return jsonify({"error": "expiry must be in the future"}), 400
            job.expiry = new_expiry
        for int_field in ('max_submissions', 'max_retries'):
            if int_field in data:
                val = data[int_field]
                if isinstance(val, int) and val >= 1:
                    caps = {'max_submissions': 100, 'max_retries': 10}
                    if val > caps.get(int_field, 100):
                        return jsonify({"error": f"{int_field} must be <= {caps[int_field]}"}), 400
                    setattr(job, int_field, val)
    elif job.status == 'funded':
        # When funded: only extend expiry
        if 'expiry' in data:
            try:
                new_expiry = datetime.datetime.fromtimestamp(int(data['expiry']), tz=datetime.timezone.utc)
            except (ValueError, TypeError, OSError):
                return jsonify({"error": "Invalid expiry timestamp"}), 400
            # m7 fix: Must extend existing expiry; if no expiry was set, new one must be >= 24h out
            now = datetime.datetime.now(datetime.timezone.utc)
            if job.expiry and new_expiry <= job.expiry:
                return jsonify({"error": "Can only extend expiry on funded jobs"}), 400
            if not job.expiry and new_expiry < now + datetime.timedelta(hours=24):
                return jsonify({"error": "New expiry on funded job must be at least 24h from now"}), 400
            job.expiry = new_expiry
        else:
            return jsonify({"error": "Only expiry extension allowed on funded jobs"}), 400
    else:
        return jsonify({"error": f"Cannot update job in {job.status} state"}), 400

    db.session.commit()
    return jsonify(JobService.to_dict(job)), 200


# ===================================================================
# 17. GET /platform/solvency — solvency monitoring (G21)
# ===================================================================


@app.route('/platform/solvency', methods=['GET'])
@require_operator  # Operator-only: financial data requires signature verification
def platform_solvency():
    """G21: Solvency overview — outstanding liabilities vs wallet balance."""
    from sqlalchemy import func

    # Outstanding liabilities: funded jobs that haven't been resolved/cancelled
    liabilities = db.session.query(
        func.coalesce(func.sum(Job.price), 0)
    ).filter(Job.status == 'funded').scalar()

    total_payouts = db.session.query(
        func.coalesce(func.sum(Job.price), 0)
    ).filter(
        Job.status == 'resolved',
        Job.payout_status == 'success',
    ).scalar()

    total_refunds = db.session.query(
        func.count(Job.task_id)
    ).filter(
        Job.refund_tx_hash.isnot(None),
        Job.refund_tx_hash != 'pending',
    ).scalar()

    funded_count = Job.query.filter_by(status='funded').count()
    failed_payouts = Job.query.filter_by(payout_status='failed').count()

    return jsonify({
        "outstanding_liabilities": float(liabilities),
        "funded_jobs_count": funded_count,
        "total_payouts_value": float(total_payouts),
        "total_refund_count": total_refunds,
        "failed_payouts_count": failed_payouts,
    }), 200


# ===================================================================
# 17b. POST /admin/jobs/<task_id>/retry-payout (G06)
# ===================================================================


@app.route('/admin/jobs/<task_id>/retry-payout', methods=['POST'])
@require_auth  # F05: buyer/winner check inside function body
def retry_payout(task_id):
    """G06: Retry failed payout for a resolved job."""
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'resolved':
        return jsonify({"error": f"Job is not resolved (current: {job.status})"}), 400

    if job.payout_status != 'failed':
        return jsonify({"error": f"Payout is not in failed state (current: {job.payout_status})"}), 400

    if not job.winner_id:
        return jsonify({"error": "No winner to pay"}), 400

    # F05: Only buyer or winner can retry payout
    if g.current_agent_id not in (job.buyer_id, job.winner_id):
        return jsonify({"error": "Only buyer or winner can retry payout"}), 403

    worker = db.session.get(Agent, job.winner_id)
    if not worker or not worker.wallet_address:
        return jsonify({"error": "Winner has no wallet address"}), 400

    adapter = None
    if _chain_registry:
        try:
            adapter = _chain_registry.get_or_default(job.chain_id)
        except (ValueError, RuntimeError):
            adapter = None
    if not adapter or not adapter.is_connected():
        return jsonify({"error": "Chain not connected"}), 503

    # G06-retry: Retry with exponential backoff for transient RPC failures
    import time as _time
    max_attempts = 3
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            fee_bps = job.fee_bps if job.fee_bps is not None else Config.PLATFORM_FEE_BPS
            result = adapter.payout(worker.wallet_address, job.price, fee_bps)
            job.payout_tx_hash = result.payout_tx
            job.fee_tx_hash = result.fee_tx or None

            # P0-1 fix (C-03): Check pending status
            if result.pending:
                job.payout_status = 'pending_confirmation'
            # P0-1 fix (C-02): Check fee_error
            elif result.fee_error:
                job.payout_status = 'partial'
            else:
                job.payout_status = 'success'

            job.payout_error = None  # Clear previous error on success

            # Only count worker earnings when not pending
            if not result.pending:
                worker_share = Decimal(10000 - fee_bps) / Decimal(10000)
                worker.total_earned = (worker.total_earned or 0) + job.price * worker_share

            db.session.commit()
            return jsonify({
                "status": "payout_retried",
                "task_id": task_id,
                "payout_tx_hash": job.payout_tx_hash,
                "payout_status": job.payout_status,
                "attempts": attempt,
            }), 200
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                backoff = 2 ** attempt
                logger.warning(
                    "Payout retry attempt %d/%d failed for task %s: %s — retrying in %ds",
                    attempt, max_attempts, task_id, e, backoff,
                )
                _time.sleep(backoff)
            else:
                logger.error("Payout retry failed after %d attempts for task %s: %s", max_attempts, task_id, e)

    job.payout_error = str(last_error)
    db.session.commit()
    return jsonify({"error": f"Payout retry failed after {max_attempts} attempts"}), 500


# ===================================================================
# 18. POST /jobs/<task_id>/dispute — dispute stub (G24)
# ===================================================================


@app.route('/jobs/<task_id>/dispute', methods=['POST'])
@require_auth
def dispute_job(task_id):
    """G24: Dispute stub — records dispute request but doesn't resolve it."""
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'resolved':
        return jsonify({"error": f"Can only dispute resolved jobs (current: {job.status})"}), 400

    data = request.get_json(silent=True) or {}
    reason = data.get('reason', '')
    if not reason:
        return jsonify({"error": "reason is required"}), 400
    if len(reason) > 5000:
        return jsonify({"error": "reason must be <= 5000 characters"}), 400

    # Only buyer or winner can dispute
    agent_id = g.current_agent_id
    if agent_id not in (job.buyer_id, job.winner_id):
        return jsonify({"error": "Only buyer or winner can dispute"}), 403

    existing_dispute = Dispute.query.filter_by(task_id=task_id, filed_by=agent_id).first()
    if existing_dispute:
        return jsonify({"error": "You have already filed a dispute for this job"}), 409

    dispute = Dispute(
        task_id=task_id,
        filed_by=agent_id,
        reason=reason,
    )
    db.session.add(dispute)
    db.session.commit()

    return jsonify({
        "status": "dispute_filed",
        "dispute_id": dispute.id,
        "task_id": task_id,
        "filed_by": agent_id,
        "message": "Dispute recorded. Manual review required.",
    }), 202


# ===================================================================
# Dashboard — read-only HTML pages and stats API
# ===================================================================


@app.route('/')
def landing():
    return render_template('landing.html')


@app.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')


@app.route('/skill.md')
def skill_md():
    import pathlib
    skill_path = pathlib.Path(app.root_path, 'static', 'Skill.md')
    if not skill_path.exists():
        return jsonify({"error": "Skill.md not yet available"}), 404
    resp = make_response(skill_path.read_text(encoding='utf-8'))
    resp.headers['Content-Type'] = 'text/markdown; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


@app.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    from services.dashboard_service import DashboardService, etag_response
    stats = DashboardService.get_stats()
    logger.info("GET /dashboard/stats → agents=%d volume=%.2f",
                stats.get('total_agents', 0), stats.get('total_volume', 0))
    return etag_response(stats, cache_max_age=30)


@app.route('/dashboard/leaderboard', methods=['GET'])
def dashboard_leaderboard():
    from services.dashboard_service import DashboardService, etag_response
    sort_by = request.args.get('sort_by', 'total_earned')
    if sort_by not in ('total_earned', 'completion_rate'):
        sort_by = 'total_earned'
    try:
        limit = min(max(1, int(request.args.get('limit', 20))), 100)
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0
    data = DashboardService.get_leaderboard(sort_by=sort_by, limit=limit, offset=offset)
    return etag_response(data, cache_max_age=30)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(port=5005, debug=os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1'))
