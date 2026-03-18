#!/usr/bin/env python3
"""
Live Scenario Runner — sends real HTTP requests to a running SYNAI server.

Unlike pytest (which uses an isolated in-memory SQLite), this script hits the
actual running server so all data is visible on the Dashboard in real time.

Usage:
    # Start the server first:
    python server.py

    # Then run scenarios:
    python scripts/live_scenario.py                    # full lifecycle
    python scripts/live_scenario.py --scenario market  # busy marketplace
    python scripts/live_scenario.py --url http://localhost:5006

Scenarios:
    lifecycle  — Register agents, create job, fund, claim, submit, poll oracle
    market     — Create a busy marketplace with many agents and tasks
    stress     — Rapid concurrent claims and submissions
"""

import argparse
import os
import sys
import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

CYAN = "\033[96m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

_pass_count = 0
_fail_count = 0


def ok(label, detail=""):
    global _pass_count
    _pass_count += 1
    extra = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {GREEN}\u2713{RESET} {label}{extra}")


def fail(label, detail=""):
    global _fail_count
    _fail_count += 1
    extra = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {RED}\u2717{RESET} {label}{extra}")


def check(label, condition, detail=""):
    if condition:
        ok(label, detail)
    else:
        fail(label, detail)
    return condition


def section(title):
    print(f"\n{BOLD}{CYAN}--- {title} ---{RESET}")


def info(msg):
    print(f"  {DIM}{msg}{RESET}")


def summary():
    total = _pass_count + _fail_count
    print(f"\n{BOLD}{'=' * 55}{RESET}")
    if _fail_count == 0:
        print(f"  {GREEN}{BOLD}{_pass_count}/{total} checks passed{RESET}")
    else:
        print(f"  {RED}{BOLD}{_fail_count} failed{RESET} / {_pass_count} passed (total {total})")
    print()
    return _fail_count == 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class Agent:
    """Wrapper around a registered agent with its API key."""

    def __init__(self, agent_id, api_key, name=None):
        self.agent_id = agent_id
        self.api_key = api_key
        self.name = name or agent_id

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def get(self, path, **kwargs):
        return requests.get(f"{BASE_URL}{path}", headers=self.headers(), **kwargs)

    def post(self, path, **kwargs):
        return requests.post(f"{BASE_URL}{path}", headers=self.headers(), **kwargs)

    def patch(self, path, **kwargs):
        return requests.patch(f"{BASE_URL}{path}", headers=self.headers(), **kwargs)


def register_agent(agent_id, name=None, wallet=None):
    """Register an agent and return an Agent wrapper."""
    payload = {"agent_id": agent_id}
    if name:
        payload["name"] = name
    if wallet:
        payload["wallet_address"] = wallet
    r = requests.post(f"{BASE_URL}/agents", json=payload, timeout=10)
    if r.status_code == 201:
        return Agent(agent_id, r.json()["api_key"], name)
    elif r.status_code == 409:
        # Already exists — cannot recover API key; caller should use unique IDs
        return None
    else:
        fail(f"Register {agent_id}", f"HTTP {r.status_code}: {r.text[:100]}")
        return None


def wait_for_server():
    """Block until the server responds to /health."""
    for _ in range(10):
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=3)
            if r.status_code == 200:
                return True
        except requests.ConnectionError:
            pass
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# Dashboard snapshot
# ---------------------------------------------------------------------------

def print_dashboard_snapshot():
    """Fetch and display current dashboard stats."""
    try:
        stats = requests.get(f"{BASE_URL}/dashboard/stats", timeout=5).json()
        lb = requests.get(f"{BASE_URL}/dashboard/leaderboard?limit=5", timeout=5).json()
    except Exception as e:
        info(f"(dashboard unavailable: {e})")
        return

    print(f"\n  {BOLD}Dashboard Snapshot{RESET}")
    print(f"    Agents: {stats.get('total_agents', '?')}  "
          f"Active: {stats.get('total_active_agents', '?')}  "
          f"Volume: {stats.get('total_volume', '?')} USDG")
    tbs = stats.get('tasks_by_status', {})
    status_parts = [f"{k}={v}" for k, v in sorted(tbs.items())]
    if status_parts:
        print(f"    Tasks:  {', '.join(status_parts)}")

    agents_list = lb.get('agents', [])
    if agents_list:
        print(f"    Top agents:")
        for i, a in enumerate(agents_list[:5], 1):
            medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, " ")
            print(f"      {medal} {a['name'] or a['agent_id']}: "
                  f"${a['total_earned']:.2f}  "
                  f"({a.get('tasks_won', 0)} wins)")
    print()


# ---------------------------------------------------------------------------
# Scenario: lifecycle
# ---------------------------------------------------------------------------

def scenario_lifecycle():
    """Full job lifecycle: register, create, fund, claim, submit, poll."""
    uid = uuid.uuid4().hex[:6]

    section("Health Check")
    if not check("Server reachable", wait_for_server()):
        print(f"\n{RED}Cannot connect to {BASE_URL}. Is the server running?{RESET}")
        sys.exit(1)

    # ---- Register agents ----
    section("Register Agents")
    buyer = register_agent(
        f"buyer-{uid}", f"Buyer-{uid}",
        f"0x{'b' * 40}",
    )
    check("Buyer registered", buyer is not None)

    workers = []
    for i in range(3):
        w = register_agent(
            f"worker-{uid}-{i}", f"Worker-{uid}-{i}",
            f"0x{i+1:040x}",
        )
        check(f"Worker {i} registered", w is not None)
        if w:
            workers.append(w)

    if not buyer or len(workers) < 2:
        fail("Need at least buyer + 2 workers to continue")
        return

    print_dashboard_snapshot()

    # ---- Create job ----
    section("Create Job")
    r = buyer.post("/jobs", json={
        "title": f"Live Test: Build a CLI calculator ({uid})",
        "description": "Create a Python CLI calculator that supports +, -, *, /. "
                       "Include error handling for division by zero.",
        "price": 5.0,
        "artifact_type": "CODE",
    })
    check("Job created (201)", r.status_code == 201, f"got {r.status_code}")
    job = r.json()
    task_id = job.get("task_id", "")
    info(f"task_id: {task_id}")
    info(f"status:  {job.get('status')}")
    check("Status is 'open'", job.get("status") == "open")

    # ---- Fund job ----
    section("Fund Job")
    r = buyer.post(f"/jobs/{task_id}/fund", json={
        "tx_hash": f"0xfund_{uid}_{int(time.time())}",
    })
    check("Job funded (200)", r.status_code == 200, f"got {r.status_code}")
    check("Status is 'funded'", r.json().get("status") == "funded")

    print_dashboard_snapshot()

    # ---- Workers claim ----
    section("Workers Claim")
    for i, w in enumerate(workers):
        r = w.post(f"/jobs/{task_id}/claim")
        check(f"Worker {i} claimed", r.status_code == 200,
              f"got {r.status_code}: {r.text[:80]}")

    # Verify participant count
    r = requests.get(f"{BASE_URL}/jobs/{task_id}", timeout=5)
    participants = r.json().get("participants", [])
    check("Participant count matches", len(participants) == len(workers),
          f"expected {len(workers)}, got {len(participants)}")

    print_dashboard_snapshot()

    # ---- Worker 0 unclaims ----
    section("Worker 0 Unclaims")
    r = workers[0].post(f"/jobs/{task_id}/unclaim")
    check("Worker 0 unclaimed", r.status_code == 200,
          f"got {r.status_code}: {r.text[:80]}")

    # ---- Worker 1 submits ----
    section("Worker 1 Submits")
    r = workers[1].post(f"/jobs/{task_id}/submit", json={
        "content": {
            "code": (
                "import sys\n"
                "a, op, b = float(sys.argv[1]), sys.argv[2], float(sys.argv[3])\n"
                "ops = {'+': a+b, '-': a-b, '*': a*b, '/': a/b if b else 'ERR'}\n"
                "print(ops.get(op, 'Unknown op'))\n"
            ),
            "language": "python",
            "notes": "Handles +, -, *, / with division-by-zero guard.",
        },
    })
    check("Submission accepted (202)", r.status_code == 202,
          f"got {r.status_code}: {r.text[:100]}")
    sub = r.json()
    submission_id = sub.get("submission_id", "")
    info(f"submission_id: {submission_id}")
    check("Status is 'judging'", sub.get("status") == "judging")

    # ---- Poll oracle ----
    section("Poll Oracle Evaluation")
    start = time.time()
    timeout = 180
    final_status = "judging"
    while time.time() - start < timeout:
        r = requests.get(f"{BASE_URL}/submissions/{submission_id}", timeout=10)
        if r.status_code != 200:
            break
        final_status = r.json().get("status", "")
        if final_status != "judging":
            break
        elapsed = int(time.time() - start)
        print(f"    {DIM}... judging ({elapsed}s){RESET}", end="\r", flush=True)
        time.sleep(3)

    elapsed = round(time.time() - start, 1)
    print(f"    Oracle finished in {elapsed}s                       ")
    check("Reached terminal status", final_status in ("passed", "failed"),
          f"got '{final_status}'")

    # Submission details
    r = requests.get(f"{BASE_URL}/submissions/{submission_id}", timeout=10)
    sub_detail = r.json()
    info(f"score:  {sub_detail.get('oracle_score')}")
    info(f"reason: {(sub_detail.get('oracle_reason') or '')[:120]}")

    # ---- Final job status ----
    section("Final Job Status")
    r = requests.get(f"{BASE_URL}/jobs/{task_id}", timeout=5)
    job_final = r.json()
    info(f"status: {job_final.get('status')}")
    info(f"winner: {job_final.get('winner_id', 'none')}")

    if final_status == "passed":
        check("Job resolved", job_final.get("status") == "resolved")
        check("Winner set to submitter", job_final.get("winner_id") == workers[1].agent_id)
    else:
        check("Job still funded (oracle rejected)", job_final.get("status") == "funded")

    print_dashboard_snapshot()


# ---------------------------------------------------------------------------
# Scenario: market — create a busy marketplace
# ---------------------------------------------------------------------------

def scenario_market():
    """Populate the dashboard with many agents and varied tasks."""
    uid = uuid.uuid4().hex[:4]

    section("Health Check")
    if not check("Server reachable", wait_for_server()):
        print(f"\n{RED}Cannot connect to {BASE_URL}. Is the server running?{RESET}")
        sys.exit(1)

    # ---- Register 8 agents ----
    section("Register 8 Agents")
    names = [
        ("alpha-coder", "Alpha Coder"),
        ("beta-solver", "Beta Solver"),
        ("gamma-writer", "Gamma Writer"),
        ("delta-analyst", "Delta Analyst"),
        ("epsilon-auditor", "Epsilon Auditor"),
        ("zeta-tester", "Zeta Tester"),
        ("eta-researcher", "Eta Researcher"),
        ("theta-builder", "Theta Builder"),
    ]
    agents = {}
    for idx, (base_id, name) in enumerate(names):
        aid = f"{base_id}-{uid}"
        wallet = f"0x{idx+1:040x}"
        a = register_agent(aid, name, wallet)
        if a:
            agents[base_id] = a
            ok(f"Registered {name}")
        else:
            fail(f"Register {name}")

    if len(agents) < 4:
        fail("Need at least 4 agents")
        return

    buyer_key = list(agents.keys())[0]
    buyer = agents[buyer_key]

    # ---- Create varied jobs ----
    section("Create 10 Tasks")
    tasks_spec = [
        ("Smart contract audit for DeFi protocol", "Review Solidity code for reentrancy, overflow, access control vulnerabilities", 50.0, "GENERAL"),
        ("Build REST API with auth middleware", "Create Express.js REST API with JWT auth, rate limiting, CORS", 25.0, "CODE"),
        ("Design database schema for social app", "ERD + migration scripts for user profiles, posts, comments, likes", 15.0, "GENERAL"),
        ("Implement real-time chat system", "WebSocket-based chat with rooms, typing indicators, read receipts", 35.0, "CODE"),
        ("Write comprehensive test suite", "pytest suite with fixtures, mocks, 90%+ coverage for payment module", 12.0, "CODE"),
        ("Create data pipeline ETL", "Airflow DAG for daily ETL from 3 data sources into warehouse", 40.0, "CODE"),
        ("Mobile app UI design mockups", "Figma wireframes and high-fidelity mockups for onboarding flow", 20.0, "GENERAL"),
        ("Kubernetes deployment manifests", "k8s YAML for 3-tier app with HPA, PDB, resource limits", 18.0, "CODE"),
        ("ML model for text classification", "Fine-tune transformer for multi-label classification, F1 > 0.85", 60.0, "GENERAL"),
        ("GraphQL API with subscriptions", "Apollo Server with type-safe resolvers, DataLoader, subscriptions", 30.0, "CODE"),
    ]

    task_ids = []
    for title, desc, price, atype in tasks_spec:
        r = buyer.post("/jobs", json={
            "title": title,
            "description": desc,
            "price": price,
            "artifact_type": atype,
        })
        if r.status_code == 201:
            tid = r.json()["task_id"]
            task_ids.append(tid)
            ok(f"${price:.0f} — {title[:50]}")
        else:
            fail(f"Create: {title[:40]}", f"HTTP {r.status_code}")

    # ---- Fund 7 of 10 jobs ----
    section("Fund 7 Tasks")
    for i, tid in enumerate(task_ids[:7]):
        r = buyer.post(f"/jobs/{tid}/fund", json={
            "tx_hash": f"0xmarket_{uid}_{i}_{int(time.time())}",
        })
        if r.status_code == 200:
            ok(f"Funded task {i+1}")
        else:
            fail(f"Fund task {i+1}", f"HTTP {r.status_code}: {r.text[:60]}")

    # ---- Workers claim tasks (spread across agents) ----
    section("Workers Claim Tasks")
    worker_keys = [k for k in agents if k != buyer_key]
    claims = 0
    for i, tid in enumerate(task_ids[:7]):
        # 2-4 workers per task
        n_workers = min(len(worker_keys), 2 + (i % 3))
        for j in range(n_workers):
            wk = worker_keys[j % len(worker_keys)]
            r = agents[wk].post(f"/jobs/{tid}/claim")
            if r.status_code == 200:
                claims += 1
    ok(f"{claims} total claims placed")

    # ---- Submit to first 2 funded tasks ----
    section("Submit to 2 Tasks")
    submissions = []
    for i in range(min(2, len(task_ids))):
        tid = task_ids[i]
        wk = worker_keys[i % len(worker_keys)]
        r = agents[wk].post(f"/jobs/{tid}/submit", json={
            "content": {"result": f"Solution for task {i+1} from {wk}",
                        "code": "print('hello world')"},
        })
        if r.status_code == 202:
            sid = r.json().get("submission_id")
            submissions.append(sid)
            ok(f"Submitted to task {i+1} (sub: {sid[:8]}...)")
        else:
            fail(f"Submit task {i+1}", f"HTTP {r.status_code}: {r.text[:80]}")

    # ---- Poll submissions ----
    if submissions:
        section("Poll Oracle (up to 3 min)")
        start = time.time()
        pending = set(submissions)
        while pending and time.time() - start < 180:
            for sid in list(pending):
                r = requests.get(f"{BASE_URL}/submissions/{sid}", timeout=10)
                if r.status_code == 200:
                    st = r.json().get("status")
                    if st != "judging":
                        pending.discard(sid)
                        score = r.json().get("oracle_score", "?")
                        ok(f"Sub {sid[:8]}: {st} (score={score})")
            if pending:
                elapsed = int(time.time() - start)
                print(f"    {DIM}... {len(pending)} still judging ({elapsed}s){RESET}",
                      end="\r", flush=True)
                time.sleep(3)
        print("                                              ")
        if pending:
            fail(f"{len(pending)} submissions still judging after timeout")

    print_dashboard_snapshot()


# ---------------------------------------------------------------------------
# Scenario: stress — concurrent claims and submissions
# ---------------------------------------------------------------------------

def scenario_stress():
    """Stress test: concurrent operations to verify thread safety."""
    uid = uuid.uuid4().hex[:4]

    section("Health Check")
    if not check("Server reachable", wait_for_server()):
        print(f"\n{RED}Cannot connect to {BASE_URL}. Is the server running?{RESET}")
        sys.exit(1)

    # ---- Register ----
    section("Register Agents")
    buyer = register_agent(f"stress-buyer-{uid}", "Stress Buyer", f"0x{'aa' * 20}")
    check("Buyer ready", buyer is not None)

    workers = []
    for i in range(6):
        w = register_agent(f"stress-w{i}-{uid}", f"Stress Worker {i}", f"0x{i+1:040x}")
        if w:
            workers.append(w)
    check(f"Registered {len(workers)} workers", len(workers) >= 4)

    if not buyer or len(workers) < 4:
        return

    # ---- Create and fund a task ----
    section("Create + Fund Task")
    r = buyer.post("/jobs", json={
        "title": f"Stress test task ({uid})",
        "description": "Any response is acceptable.",
        "price": 2.0,
    })
    check("Job created", r.status_code == 201)
    task_id = r.json().get("task_id", "")

    r = buyer.post(f"/jobs/{task_id}/fund", json={
        "tx_hash": f"0xstress_{uid}_{int(time.time())}",
    })
    check("Job funded", r.status_code == 200)

    # ---- Concurrent claims ----
    section("Concurrent Claims (6 workers)")

    def _claim(w):
        return w.agent_id, w.post(f"/jobs/{task_id}/claim")

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_claim, w) for w in workers]
        results = [f.result() for f in as_completed(futures)]

    claimed = sum(1 for _, r in results if r.status_code == 200)
    rejected = sum(1 for _, r in results if r.status_code != 200)
    ok(f"{claimed} claims accepted, {rejected} rejected")

    # All should succeed (no duplicate — each worker is unique)
    check("All unique workers claimed successfully", claimed == len(workers))

    # ---- Verify via GET ----
    r = requests.get(f"{BASE_URL}/jobs/{task_id}", timeout=5)
    pcount = len(r.json().get("participants", []))
    check("Participant count correct", pcount == len(workers),
          f"expected {len(workers)}, got {pcount}")

    # ---- Concurrent submissions from first 3 workers ----
    section("Concurrent Submissions (3 workers)")

    def _submit(w, idx):
        return w.agent_id, w.post(f"/jobs/{task_id}/submit", json={
            "content": f"Stress response from worker {idx}",
        })

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_submit, workers[i], i) for i in range(3)]
        sub_results = [f.result() for f in as_completed(futures)]

    accepted = sum(1 for _, r in sub_results if r.status_code == 202)
    ok(f"{accepted}/3 submissions accepted (202)")

    # At most one should resolve the job (race to resolve)
    info("Oracle will evaluate submissions — check dashboard for results")

    print_dashboard_snapshot()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SCENARIOS = {
    "lifecycle": scenario_lifecycle,
    "market": scenario_market,
    "stress": scenario_stress,
}


def main():
    global BASE_URL

    parser = argparse.ArgumentParser(
        description="SYNAI Live Scenario Runner — exercises real HTTP endpoints",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python scripts/live_scenario.py\n"
               "  python scripts/live_scenario.py --scenario market --url http://localhost:5006\n",
    )
    parser.add_argument(
        "--scenario", "-s",
        choices=list(SCENARIOS.keys()),
        default="lifecycle",
        help="Scenario to run (default: lifecycle)",
    )
    parser.add_argument(
        "--url", "-u",
        default=None,
        help=f"Server URL (default: {BASE_URL} or $SYNAI_URL)",
    )
    args = parser.parse_args()

    if args.url:
        BASE_URL = args.url.rstrip("/")

    print(f"{BOLD}{CYAN}SYNAI Live Scenario Runner{RESET}")
    print(f"  Server:   {BASE_URL}")
    print(f"  Scenario: {args.scenario}")

    SCENARIOS[args.scenario]()

    success = summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
