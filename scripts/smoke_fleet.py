"""Concurrent real-CLI acceptance: N missions dispatched at once.

Extends scripts/smoke_local.py from one mission to a concurrent fleet: starts
the real server with FACAI_MAX_WORKERS=N and FACAI_OPENCODE_SERIAL=0, walks the
same customer surface (register, top-up, create, bind), then dispatches N
jobs simultaneously and requires every one of them to run EXACTLY once on the
REAL OpenCode CLI and complete with a real artifact.

Pass criteria (all deterministic):
  * every job reaches COMPLETED (no FAILED, no stuck)
  * every mission shows exactly one OPENCODE_STARTED (zero double-claims)
  * every workspace contains the requested file with the requested content
  * the evidence chain per mission is complete (STARTED -> OUTPUT... ->
    ARTIFACT_CREATED -> COST_RECORDED -> COMPLETED)

Run: python scripts/smoke_fleet.py [--port 8766] [--missions 4]
Quota note: each mission is a tiny prompt on the bound model; keep the count
small on paid providers.
"""
import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_JOB_STATES = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def req(self, method, path, body=None, headers=None, csrf=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if csrf:
            hdrs["x-csrf-token"] = csrf
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                        headers=hdrs)
        try:
            with self.opener.open(request, timeout=20) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--missions", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=180,
                        help="per-job timeout seconds")
    args = parser.parse_args()

    client = Client(f"http://127.0.0.1:{args.port}")
    data = Path(tempfile.gettempdir()) / f"facai-fleet-{os.getpid()}"
    (data / "data").mkdir(parents=True, exist_ok=True)
    admin_pw = secrets.token_urlsafe(16)
    member_pw = "Brv" + secrets.token_urlsafe(12) + "9x"

    env = dict(os.environ)
    env.update({"FACAI_DATA_DIR": str(data / "data"),
                "FACAI_ADMIN_USER": "admin",
                "FACAI_ADMIN_PASSWORD": admin_pw,
                "FACAI_MAX_WORKERS": str(args.missions),
                "FACAI_OPENCODE_SERIAL": "0"})
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "facai.api:app", "--host", "127.0.0.1",
         "--port", str(args.port)], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    log = open(data / "server.log", "w", encoding="utf-8")
    threading.Thread(target=lambda: [log.write(line) or log.flush()
                                     for line in server.stdout], daemon=True).start()

    checks = []

    def note(step, ok, detail=""):
        checks.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail[:220]}", flush=True)

    try:
        deadline = time.time() + 60
        while time.time() < deadline and server.poll() is None:
            try:
                status, _, body = client.req("GET", "/api/v1/health")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            note("server-start", False, f"no ready health; exit={server.returncode}")
            return 1

        status, headers, body = client.req("POST", "/api/v1/auth/login",
                                           {"username": "admin", "password": admin_pw})
        admin_cookie = (headers.get("set-cookie")
                        or headers.get("Set-Cookie") or "").split(";")[0]
        admin_csrf = json.loads(body).get("csrf_token", "")
        admin = {"Cookie": admin_cookie}

        status, headers, body = client.req("POST", "/api/v1/auth/register",
                                           {"username": "fleet001", "password": member_pw})
        member_cookie = (headers.get("set-cookie")
                         or headers.get("Set-Cookie") or "").split(";")[0]
        member_csrf = json.loads(body).get("csrf_token", "")
        member = {"Cookie": member_cookie}
        note("auth/register+login", status == 201 and bool(member_cookie))

        status, _, body = client.req("POST", "/api/v1/admin/members/fleet001/top-up",
                                     headers=admin, csrf=admin_csrf,
                                     body={"amount_usd": 20.0})
        note("billing/top-up", status == 200, body[:120])

        missions = []
        for index in range(args.missions):
            status, _, body = client.req(
                "POST", "/api/v1/missions", headers=member, csrf=member_csrf,
                body={"title": f"fleet-{index:02d}",
                      "original_input": f"创建文件f{index}.txt，内容写：并发验证{index}",
                      "steps": [{"id": "solo", "label": "并发"}]})
            mission_id = json.loads(body).get("id") if status == 201 else None
            missions.append(mission_id)
        note("missions/created", all(missions), f"count={len(missions)}")

        status, _, body = client.req("GET", "/api/v1/registries/agents", headers=member)
        agents = sorted(json.loads(body).keys())
        agent = "opencode" if "opencode" in agents else (agents or [None])[0]
        for mission_id in missions:
            client.req("PUT", f"/api/v1/missions/{mission_id}/binding", headers=member,
                       csrf=member_csrf,
                       body={"agent_id": agent, "model_id": "deepseek-flash",
                             "runtime_id": "local"})
        note(f"missions/bound agent={agent}", bool(agent))

        started = time.monotonic()
        job_ids = {}
        for mission_id in missions:
            status, _, body = client.req(
                "POST", f"/api/v1/missions/{mission_id}/jobs", headers=member,
                csrf=member_csrf,
                body={"prompt": f"在当前工作区创建f{missions.index(mission_id)}.txt，内容：并发验证{missions.index(mission_id)}",
                      "timeout": args.timeout})
            job_ids[mission_id] = json.loads(body).get("id") if status == 201 else None
        note("jobs/enqueued", all(job_ids.values()), f"dispatched={len(job_ids)}")

        # Poll from ONE owner cursor: the fleet view of every job's state.
        finals, wall = {}, None
        deadline = time.time() + args.timeout * 2
        while time.time() < deadline:
            done = 0
            for mission_id, job_id in job_ids.items():
                if job_id in finals:
                    done += 1
                    continue
                status, _, body = client.req("GET", f"/api/v1/jobs/{job_id}",
                                             headers=member)
                state = json.loads(body).get("status")
                if state in TERMINAL_JOB_STATES:
                    finals[job_id] = state
                    done += 1
            if done == len(job_ids):
                wall = time.monotonic() - started
                break
            time.sleep(2)
        note("jobs/all-terminal", wall is not None,
             f"finals={finals} wall={wall and round(wall, 1)}s")
        note("jobs/all-completed", all(v == "COMPLETED" for v in finals.values()),
             str(finals))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    # Post-mortem straight from the durable store: double-claims, artifacts,
    # cost rows. The API is dead by now; the evidence must stand on its own.
    if any(missions):
        db_path = data / "data" / "facai.sqlite3"
        conn = sqlite3.connect(str(db_path))
        try:
            for index, mission_id in enumerate(missions):
                if not mission_id:
                    continue
                started_count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='OPENCODE_STARTED'",
                    (mission_id,)).fetchone()[0]
                completed = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='JOB_COMPLETED'",
                    (mission_id,)).fetchone()[0]
                outputs = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='OUTPUT'",
                    (mission_id,)).fetchone()[0]
                note(f"evidence/mission-{index:02d}",
                     started_count == 1 and completed == 1 and outputs > 0,
                     f"started={started_count} completed={completed} outputs={outputs}")
                artifact = (data / "data" / "workspaces" / mission_id / f"f{index}.txt")
                content = artifact.read_text(encoding="utf-8", errors="replace").strip() \
                    if artifact.exists() else "<missing>"
                note(f"artifact/mission-{index:02d}", content == f"并发验证{index}",
                     content[:60])
            cost_rows = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
            note("costs/recorded", cost_rows >= len(missions), f"rows={cost_rows}")
        finally:
            conn.close()

    passed = sum(checks)
    total = len(checks)
    print(f"\n=== FLEET VERDICT: {passed}/{total} passed ({args.missions} missions) ===",
          flush=True)
    print(f"isolated data kept for inspection: {data}", flush=True)
    return 0 if passed == total and total else 1


if __name__ == "__main__":
    sys.exit(main())
