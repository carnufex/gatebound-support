"""Runs every test attached to the agents in agents.json and prints a table.

Exit code 1 on any failed test so it can gate a push. Needs ELEVENLABS_API_KEY.

    python scripts/run_agent_tests.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.elevenlabs.io/v1/convai"
HERE = Path(__file__).resolve().parent.parent


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        print("ELEVENLABS_API_KEY is not set", file=sys.stderr)
        sys.exit(2)
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key, "content-type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code} {e.read().decode()[:300]}") from e


def main() -> int:
    registry = json.loads((HERE / "agents.json").read_text(encoding="utf-8"))
    failures = 0
    rows: list[tuple[str, str, str, str]] = []
    for agent in registry["agents"]:
        agent_id = agent.get("id")
        if not agent_id:
            rows.append((agent["config"], "(agent not pushed yet)", "-", ""))
            continue
        cfg = api(f"/agents/{agent_id}")
        attached = (cfg.get("platform_settings", {}).get("testing", {}) or {}).get("attached_tests", []) or []
        if not attached:
            rows.append((cfg["name"], "(no tests attached)", "-", ""))
            continue
        inv = api(f"/agents/{agent_id}/run-tests", "POST", {"tests": [{"test_id": t["test_id"]} for t in attached]})
        started = time.time()
        runs: list[dict] = []
        while time.time() - started < 600:
            time.sleep(5)
            status = api(f"/test-invocations/{inv['id']}")
            runs = status.get("test_runs", []) or []
            if runs and all(t.get("status") in ("passed", "failed", "error") for t in runs):
                break
        for t in runs:
            ok = t.get("status") == "passed"
            failures += 0 if ok else 1
            rationale = ""
            if not ok:
                cr = t.get("condition_result") or {}
                rat = cr.get("rationale") or {}
                rationale = (rat.get("summary") or (rat.get("messages") or [""])[0] or "")[:90]
            rows.append((cfg["name"], t.get("test_name") or t.get("test_id", "?"), t.get("status", "?"), rationale))
            if not ok and "--verbose" in sys.argv:
                print(f"--- {t.get('test_name')}: agent responses ---")
                for r in t.get("agent_responses", []) or []:
                    role = r.get("role", "?")
                    msg = (r.get("message") or "")[:600]
                    calls = [c.get("tool_name") for c in (r.get("tool_calls") or [])]
                    results = [(c.get("tool_name"), (c.get("result_value") or "")[:300]) for c in (r.get("tool_results") or [])]
                    print(f"[{role}] {msg}")
                    if calls:
                        print(f"   tool_calls: {calls}")
                    if results:
                        print(f"   tool_results: {results}")
                print("---")

    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for agent_name, test_name, status, reason in rows:
        print(f"{agent_name:<{widths[0]}}  {test_name:<{widths[1]}}  {status:<{widths[2]}}  {reason}")
    print(f"\n{len(rows)} test(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
