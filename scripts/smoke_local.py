"""Local product smoke acceptance: one real mission end to end.

Starts the real uvicorn server on loopback with an isolated data dir and a
random throwaway operator password, then walks the real customer surface:
health, session, web console, PWA page, member registration, funding-pool
top-up, mission creation, registry discovery, agent binding, job dispatch and
the job's terminal state through the in-app scheduler.

A pass means the product is usable locally: a registered member with a topped
up balance can dispatch a mission to a bound agent and watch it run. The
script never prints a credential, never writes inside the repository and never
touches a real data dir. Run: python scripts/smoke_local.py [--port 8765]
"""
import argparse
import json
import os
import secrets
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


class Smoke:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        # Loopback health checks must never ride an external proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.checks = []

    def req(self, method, path, body=None, headers=None, csrf=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if csrf:
            hdrs["x-csrf-token"] = csrf
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                        headers=hdrs)
        try:
            with self.opener.open(request, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")

    def note(self, step, ok, detail):
        self.checks.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail[:240]}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    smoke = Smoke(args.port)
    data = Path(tempfile.gettempdir()) / f"facai-smoke-{os.getpid()}"
    (data / "data").mkdir(parents=True, exist_ok=True)
    admin_pw = secrets.token_urlsafe(16)
    member_pw = "Brv" + secrets.token_urlsafe(12) + "9x"

    env = dict(os.environ)
    env.update({"FACAI_DATA_DIR": str(data / "data"),
                "FACAI_ADMIN_USER": "admin",
                "FACAI_ADMIN_PASSWORD": admin_pw})
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "facai.api:app", "--host", "127.0.0.1",
         "--port", str(args.port)], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    log = open(data / "server.log", "w", encoding="utf-8")
    threading.Thread(target=lambda: [log.write(line) or log.flush()
                                     for line in server.stdout], daemon=True).start()

    try:
        deadline = time.time() + 60
        while time.time() < deadline and server.poll() is None:
            try:
                status, _, body = smoke.req("GET", "/api/v1/health")
                if status == 200:
                    smoke.note("health", '"auth_required":true' in body, body)
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            smoke.note("server-start", False, f"no ready health; exit={server.returncode}")

        status, headers, body = smoke.req("POST", "/api/v1/auth/login",
                                          {"username": "admin", "password": admin_pw})
        admin_cookie = (headers.get("set-cookie")
                        or headers.get("Set-Cookie") or "").split(";")[0]
        admin_csrf = ""
        try:
            admin_csrf = json.loads(body).get("csrf_token", "")
        except ValueError:
            pass
        smoke.note("auth/login", status == 200 and bool(admin_cookie), f"status={status}")
        admin = {"Cookie": admin_cookie}
        html = {"Cookie": admin_cookie, "Accept": "text/html"}

        for path in ("/", "/static/mobile.html"):
            status, _, body = smoke.req("GET", path, headers=html)
            smoke.note(f"web-ui {path}", status == 200 and "<!doctype" in body.lower(),
                       f"status={status} bytes={len(body)}")

        status, headers, body = smoke.req("POST", "/api/v1/auth/register",
                                          {"username": "smoker001", "password": member_pw})
        member_cookie = (headers.get("set-cookie")
                         or headers.get("Set-Cookie") or "").split(";")[0]
        member_csrf = ""
        try:
            member_csrf = json.loads(body).get("csrf_token", "")
        except ValueError:
            pass
        smoke.note("auth/register", status == 201 and bool(member_cookie),
                   f"status={status}")
        member = {"Cookie": member_cookie}

        status, _, body = smoke.req("POST", "/api/v1/admin/members/smoker001/top-up",
                                    headers=admin, csrf=admin_csrf, body={"amount_usd": 5.0})
        smoke.note("billing/top-up", status == 200, f"status={status} {body[:120]}")

        status, _, body = smoke.req("POST", "/api/v1/missions", headers=member,
                                    csrf=member_csrf,
                                    body={"title": "smoke-冒烟-001",
                                          "original_input": "帮我创建一个hello.txt，内容写：冒烟测试",
                                          "steps": [{"id": "solo", "label": "冒烟步骤"}]})
        mission_id = None
        try:
            mission_id = json.loads(body).get("id")
        except ValueError:
            pass
        smoke.note("missions/create", status == 201 and bool(mission_id),
                   f"status={status} id={mission_id}")

        if mission_id:
            registry = {}
            for kind in ("agents", "models", "runtimes"):
                status, _, body = smoke.req("GET", f"/api/v1/registries/{kind}",
                                            headers=member)
                try:
                    registry[kind] = sorted(json.loads(body).keys())
                except ValueError:
                    registry[kind] = []
            agent = "opencode" if "opencode" in registry["agents"] else \
                (registry["agents"] or [None])[0]
            bindable = all(registry.get(k) for k in ("agents", "models", "runtimes"))
            if bindable and agent:
                status, _, body = smoke.req(
                    "PUT", f"/api/v1/missions/{mission_id}/binding", headers=member,
                    csrf=member_csrf,
                    body={"agent_id": agent, "model_id": registry["models"][0],
                          "runtime_id": registry["runtimes"][0]})
                smoke.note("mission/binding", status == 200, f"status={status} agent={agent}")

            status, _, body = smoke.req(
                "POST", f"/api/v1/missions/{mission_id}/jobs", headers=member,
                csrf=member_csrf,
                body={"prompt": "在当前工作区创建hello.txt，内容：冒烟测试", "timeout": 120})
            job_id = None
            try:
                job_id = json.loads(body).get("id")
            except ValueError:
                pass
            smoke.note("jobs/enqueue", status == 201 and bool(job_id),
                       f"status={status} {body[:160]}")
            if job_id:
                final = None
                deadline = time.time() + 120
                while time.time() < deadline:
                    status, _, body = smoke.req("GET", f"/api/v1/jobs/{job_id}",
                                                headers=member)
                    try:
                        state = json.loads(body).get("status")
                    except ValueError:
                        state = None
                    if state in TERMINAL_JOB_STATES:
                        final = state
                        break
                    time.sleep(2)
                smoke.note("job/terminal", final == "COMPLETED",
                           f"final={final} body={body[:160]}")
            status, _, body = smoke.req("GET", f"/api/v1/missions/{mission_id}/events",
                                        headers=member)
            smoke.note("mission/events", status == 200 and "MISSION_CREATED" in body,
                       f"status={status} events={body.count('\"type\":') or body.count(',\"type\"')}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    passed = sum(smoke.checks)
    total = len(smoke.checks)
    print(f"\n=== SMOKE VERDICT: {passed}/{total} passed ===", flush=True)
    print(f"isolated data kept for inspection: {data}", flush=True)
    return 0 if passed == total and total else 1


if __name__ == "__main__":
    sys.exit(main())
