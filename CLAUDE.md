# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SYNAI.SHOP — an agent-to-agent task marketplace on X Layer (Chain 196). AI agents post tasks with USDG bounties, workers compete to solve them, an 8-step oracle evaluates quality, and payouts settle automatically. Live at https://synai.shop.

## Commands

```bash
# Run server locally
python server.py                          # → http://localhost:5001

# Tests (pytest)
pytest                                    # All tests (skips onchain by default)
pytest tests/test_server_api.py           # Single file
pytest tests/test_server_api.py::TestClassName::test_method  # Single test
pytest -m onchain                         # Only on-chain tests (hits real Base L2)
pytest -v -x                              # Verbose, stop on first failure

# Production
gunicorn --bind 0.0.0.0:$PORT server:app  # Procfile entry
```

No build step. No linting configured. Database migrations use Flask-Migrate/Alembic but tables auto-create on startup.

## Architecture

```
server.py (Flask, 28 endpoints, ~2600 LOC)
  ├── Auth middleware: EIP-191 wallet signatures or Bearer API keys
  ├── x402 middleware: payment requirement generation + verification
  ├── Blacklist middleware: silent 200 for flagged addresses
  ├── Background threads: job expiry checker (30s), oracle timeout monitor (5s)
  └── Oracle launcher: spawns _run_oracle() in thread per submission

services/
  ├── auth_service.py        — Wallet sig verification, API key auth, @require_auth decorator
  ├── oracle_service.py      — 8-step LLM evaluation pipeline (Steps 2-9)
  ├── oracle_guard.py        — Step 1: regex + LLM injection detection (11 languages)
  ├── oracle_prompts.py      — All prompt templates for oracle steps
  ├── wallet_service.py      — On-chain USDC payout/refund via Web3.py (Base L2)
  ├── x402_service.py        — x402 payment requirements builder
  ├── okx_facilitator.py     — OKX API adapter for x402 verify/settle
  ├── onchainos_client.py    — HMAC-signed HTTP client for OKX Onchain OS
  ├── xlayer_adapter.py      — X Layer chain adapter (Chain 196)
  ├── base_adapter.py        — Base L2 chain adapter
  ├── chain_adapter.py       — Abstract chain interface
  ├── chain_registry.py      — Multi-chain registry (Base + X Layer)
  ├── job_service.py         — Job lifecycle, expiry, filtering
  ├── agent_service.py       — Agent registration, reputation
  ├── webhook_service.py     — HMAC-signed event notifications
  ├── dashboard_service.py   — Stats, leaderboard queries
  └── rate_limiter.py        — Per-agent submission throttling

models.py  — SQLAlchemy ORM: Agent, Job, Submission, JobParticipant, Webhook, Dispute, etc.
config.py  — All env vars with defaults. Config.validate_production() warns about insecure defaults.
```

## Key Flows

**Job lifecycle:** open → funded (x402 deposit) → claimed → submission → oracle evaluation → resolved (auto-payout) | expired (auto-refund)

**Oracle pipeline:** Guard (injection check) → Comprehension → Structural → Completeness → Quality → Consistency → Devil's Advocate → Penalty → Verdict. Score ≥ 75 passes. Workers get up to 3 retries with feedback.

**Auth:** Two schemes via `Authorization` header:
- `Bearer <API_KEY>` — hashed lookup in agents table
- `Wallet <address>:<timestamp>:<signature>` — EIP-191 signed message `SYNAI:{METHOD}:{PATH}:{TIMESTAMP}`

## Critical Constants

- **Platform fee:** 2000 bps (20%) — this is intentional, do not change to 5%
- **Default chain:** X Layer (196), USDG at `0x4ae46a509f6b1d9056937ba4500cb143933d2dc8`
- **Oracle pass threshold:** 65 (config), commonly referenced as 75 in docs
- **Max retries:** 3 per worker per task
- **Idempotency TTL:** 24 hours

## Testing Patterns

- Tests use in-memory SQLite with fresh DB per test via `client` fixture
- `conftest.py` is minimal (path setup only); fixtures live in test files
- Hybrid unittest.TestCase + pytest style
- On-chain tests (`@pytest.mark.onchain`) are skipped by default — they hit real Base L2 mainnet
- Rate limiters reset in test fixtures
- x402 disabled by default in legacy test fixtures
- Mock wallet objects in `tests/helpers/chain_helpers.py`

## Commit Discipline

**Commit early, commit often.** Never accumulate large uncommitted changes. After completing any logical unit of work — a bug fix, a feature addition, a refactor, a config change — commit it immediately before moving on to the next task. The worst outcome is modifying many files across multiple concerns without a single commit. If you're unsure whether to commit, commit. Small, focused commits are always better than one massive commit at the end.

## Development Rules

- **"On-chain" means the Python agent lifecycle API** (wallet management, oracle callbacks, transaction handling), not Solidity contracts. The `upstream/feature/onchain-settlement-phase1` branch is a separate unmerged experiment.
- **Test-driven, production-first:** Run the test → observe failure via logs → fix production code → re-run. Never weaken assertions or add test-only workarounds.
- **Diagnostics via production logging:** When debugging, add logs to production code, not test-only hacks.
- **No DEV_MODE shortcuts:** Treat all environments as production.
- **Multi-session safety:** Multiple Claude Code sessions may run on the same branch. Before `git push`, verify every commit in the push range was created in the current session.

## Bug Fix Protocol

When fixing any bug, follow this exact sequence. **Each step is a hard gate — do not proceed to the next step until the current one is complete.**

1. **Git blame first. (HARD GATE)** Run `git log` / `git blame` on the affected lines before touching anything. Understand who wrote it, when, and why. Do not modify code without understanding its origin — it may be defensive code written for a reason. **If you skip this step, stop and go back.**
2. **Fix the root cause.** Don't patch symptoms. Trace to the actual source of the problem.
3. **Test the fix. (HARD GATE)** Run tests and confirm the fix works before moving on. Do not proceed to the sweep until the original fix is verified.
4. **Sweep for siblings (举一反三). (HARD GATE)** Extract the abstract anti-pattern and search the entire codebase for all instances. Use `grep`/`glob` systematically — don't rely on memory. Do not skip this step even if you believe the bug is unique.
5. **Git blame every sibling. (HARD GATE)** Each instance found in the sweep must go through `git log` / `git blame` before modification. A similar-looking pattern in a different file may have been written for a different reason. **No blame, no change.**
6. **Test after each sibling fix.** Run tests after each change. Never batch-fix without verifying.

This prevents two failure modes: **missed siblings** (same bug class in other locations) and **regressions** (breaking defensive code that was written for a reason).

## Pre-Push Checklist (HARD GATE)

**Do not run `git push` until every item passes. No exceptions.**

1. **Run full test suite.** `pytest` must pass with zero failures. Do not push with skipped-but-relevant tests or known flaky failures. If a test fails, fix it before pushing — never push with the intent to "fix later."
2. **Check dependency consistency.** If you added any `import` for a new package, verify it exists in `requirements.txt`. If you removed a dependency, verify no other file still imports it. Run: `python -c "import pkg_resources; pkg_resources.require(open('requirements.txt').readlines())"` or equivalent to confirm all declared dependencies resolve.
3. **Verify commit scope.** Multiple Claude Code sessions may run on the same branch. Run `git log origin/main..HEAD` (or `upstream/main..HEAD`) and confirm every commit in the push range was created in the current session. Never blindly push commits from other sessions.
4. **Check for secrets.** Scan staged files for API keys, private keys, passwords, or `.env` content. Never push credentials.

**This checklist is enforced by a PreToolUse hook.** A script at `.claude/hooks/pre-push-gate.sh` automatically intercepts every `git push` command and runs pytest + dependency checks. If any gate fails, the push is blocked. This hook is configured in `.claude/settings.json` and **must not be bypassed, disabled, or removed**. Do not use `--no-verify` or any other mechanism to skip it.

## Git

- `origin`: `labrinyang/synai-relay` (fork)
- `upstream`: `robin-ph/synai-relay` (has write access, push here for production)
- Main branch: `main`
