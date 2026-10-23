"""Whole-percent mission progress: honest, integer and boundary-safe.

The progress bar is displayed in units of one percent, so every value the API
serialises must be an ``int`` in ``[0, 100]`` and the console's fill width,
``aria-valuenow`` and label must all come from the same number. Progress only
ever counts real COMPLETED steps; an empty pipeline is 0 and a mission is only
100% when every step is complete.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from facai import store as store_module
from facai.api import create_app
from facai.store import Store, measured_mission_progress, mission_progress


def test_measured_progress_has_two_decimal_resolution_without_active_guess():
    assert measured_mission_progress(1, 3) == 33.33
    assert measured_mission_progress(2, 3) == 66.66
    assert measured_mission_progress(0, 3) == 0.0
    assert measured_mission_progress(3, 3) == 100.0
    assert measured_mission_progress(0, 0) == 0.0


def test_api_reports_progress_basis_and_unknown_active_step(tmp_path):
    client = make_client(tmp_path)
    mission = create(client, "可测进度", ["规划", "执行", "验收"])
    first = mission["steps"][0]["id"]
    assert client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}",
                        json={"status": "RUNNING"}).status_code == 200
    running = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert running["progress_measured"] == 0.0
    assert running["progress_basis"] == {"unit": "completed_steps", "completed": 0,
                                         "total": 3, "active_unmeasured": 1}
    assert running["steps"][0]["progress_measured"] is None
    assert running["steps"][0]["progress_basis"] == "unmeasured_execution"
    assert client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}",
                        json={"status": "COMPLETED"}).status_code == 200
    done = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert done["progress_measured"] == 33.33
    assert done["progress_basis"]["completed"] == 1


def make_client(tmp_path):
    return TestClient(create_app(tmp_path))


def create(client, title, steps):
    response = client.post("/api/v1/missions", json={
        "title": title, "original_input": f"原始要求：{title}", "steps": list(steps)})
    assert response.status_code == 201
    return response.json()


# --- pure helper boundaries -------------------------------------------------

def test_progress_boundaries_are_integers():
    assert mission_progress(0, 0) == 0          # empty pipeline
    assert mission_progress(0, 1) == 0          # nothing done
    assert mission_progress(1, 1) == 100        # single step complete
    assert mission_progress(0, 100) == 0
    assert mission_progress(1, 100) == 1        # first step is visible
    assert mission_progress(99, 100) == 99
    assert mission_progress(100, 100) == 100
    assert mission_progress(50, 100) == 50


def test_progress_never_returns_a_float_or_leaves_the_range():
    for completed in range(0, 101):
        value = mission_progress(completed, 100)
        assert isinstance(value, int) and not isinstance(value, bool)
        assert 0 <= value <= 100
    # 1/3 is a real fraction but must never leak as 33.3333.
    assert mission_progress(1, 3) == 33
    assert mission_progress(2, 3) == 66
    assert mission_progress(1, 6) == 16
    assert mission_progress(5, 6) == 83


def test_partial_progress_advances_at_least_one_point_per_completed_step():
    total = 100
    previous = 0
    for completed in range(1, total + 1):
        value = mission_progress(completed, total)
        assert value - previous >= 1, (completed, previous, value)
        assert value <= 100
        previous = value
    assert previous == 100


def test_clamps_out_of_range_and_missing_inputs():
    assert mission_progress(-5, 10) == 0
    assert mission_progress(0, -1) == 0
    assert mission_progress(20, 10) == 100      # more done than steps cannot exceed 100
    assert mission_progress(1, 0) == 0


# --- store / API integration ------------------------------------------------

def test_empty_pipeline_is_zero_and_not_rounded(tmp_path):
    stored = Store(tmp_path).create("空流水线", "没有步骤", [])
    assert stored["progress"] == 0
    assert isinstance(stored["progress"], int)


def test_completed_steps_drive_integer_progress(tmp_path):
    client = make_client(tmp_path)
    mission = create(client, "百步流水线", [f"步骤{i}" for i in range(100)])
    assert mission["progress"] == 0

    first = mission["steps"][0]["id"]
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}", json={"status": "RUNNING"})
    running = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert running["progress"] == 1, "an active pipeline must be visible at the 1% minimum unit"
    assert running["steps"][0]["progress"] == 1

    client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}", json={"status": "COMPLETED"})
    assert client.get(f"/api/v1/missions/{mission['id']}").json()["progress"] == 1

    for step in mission["steps"][1:99]:
        client.patch(f"/api/v1/missions/{mission['id']}/steps/{step['id']}", json={"status": "RUNNING"})
        client.patch(f"/api/v1/missions/{mission['id']}/steps/{step['id']}", json={"status": "COMPLETED"})
    assert client.get(f"/api/v1/missions/{mission['id']}").json()["progress"] == 99

    last = mission["steps"][-1]["id"]
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{last}", json={"status": "RUNNING"})
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{last}", json={"status": "COMPLETED"})
    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail["progress"] == 100 and detail["status"] == "COMPLETED"


def test_failed_step_counts_as_zero_until_it_is_completed(tmp_path):
    client = make_client(tmp_path)
    mission = create(client, "失败与恢复", ["规划", "开发", "测试"])
    first = mission["steps"][0]["id"]
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}", json={"status": "RUNNING"})
    failed = client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}",
                          json={"status": "FAILED"}).json()
    assert failed["progress"] == 0 and failed["status"] == "FAILED"

    # A failed step can be retried; only its real completion moves progress.
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}", json={"status": "RUNNING"})
    recovered = client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}",
                             json={"status": "COMPLETED"}).json()
    assert recovered["progress"] == 33 and recovered["status"] == "RUNNING"


def test_api_serialises_progress_as_an_integer_everywhere(tmp_path):
    client = make_client(tmp_path)
    mission = create(client, "序列化核验", ["规划", "开发", "测试"])
    first = mission["steps"][0]["id"]
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}", json={"status": "RUNNING"})
    client.patch(f"/api/v1/missions/{mission['id']}/steps/{first}", json={"status": "COMPLETED"})

    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    listing = client.get("/api/v1/missions").json()[0]
    for value in (detail["progress"], listing["progress"]):
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value == 33
    # The list and the detail view agree, and neither carries a float.
    assert listing["progress"] == detail["progress"]
    assert b"33.3333" not in client.get("/api/v1/missions").content
    assert b"33.3333" not in client.get(f"/api/v1/missions/{mission['id']}").content


# --- console consistency ----------------------------------------------------

def test_console_renders_the_bar_from_one_normalised_percent():
    html = (Path(store_module.__file__).resolve().parent / "static" / "index.html").read_text(
        encoding="utf-8")
    assert "const progressPct=" in html, "the console must normalise progress to an integer"
    assert "Math.max(0,Math.min(100,n))" in html, "progressPct must clamp to 0-100"
    assert "${m.progress}%" not in html, "the console must not interpolate raw mission progress"

    bar = next(line for line in html.splitlines() if 'role="progressbar"' in line)
    # Fill width, aria value and the accessible label share the same variable.
    assert 'aria-valuenow="${pct}"' in bar
    assert 'aria-label="总体进度 ${pct}%"' in bar
    assert 'style="width:${pct}%"' in bar


# --- milestone-based step progress -------------------------------------------

def test_running_step_progress_reflects_checkpoint_milestone(tmp_path):
    """RUNNING is 1% at start and 90% once its Git checkpoint event exists."""
    store = Store(tmp_path)
    mission = store.create("里程碑", "原话", ["开发", "测试"], owner_id="admin")
    step_id = mission["steps"][0]["id"]

    assert mission["steps"][0]["progress"] == 0
    store.step_status(mission["id"], step_id, "RUNNING")
    running = store.get(mission["id"])
    assert running["steps"][0]["progress"] == 1

    store.append(mission["id"], "GIT_CHECKPOINT", {"commit_hash": "abc"},
                 step_id=step_id)
    with_checkpoint = store.get(mission["id"])
    assert with_checkpoint["steps"][0]["progress"] == 90
    assert with_checkpoint["steps"][1]["progress"] == 0  # untouched sibling

    store.step_status(mission["id"], step_id, "COMPLETED")
    done = store.get(mission["id"])
    assert done["steps"][0]["progress"] == 100


# --- A10: Agent 级可计量单位（派发归因 × 事件结局，零时间推演）---------------

def test_agent_progress_is_attributed_and_measured(tmp_path):
    client = make_client(tmp_path)
    mission = create(client, "车队计量", ["规划", "执行", "验收"])
    store = client.app.state.store
    j1 = store.enqueue_job(mission["id"], "p1", dispatch={"agent_id": "alpha"})
    j2 = store.enqueue_job(mission["id"], "p2", dispatch={"agent_id": "alpha"})
    j3 = store.enqueue_job(mission["id"], "p3", dispatch={"agent_id": "beta"})
    store.enqueue_job(mission["id"], "p4")  # 无派发归因：不进入任何 Agent 的账
    store.finish_job(j1["id"], "COMPLETED")
    store.finish_job(j2["id"], "FAILED")
    claimed = store.claim_job("worker-1", 60)
    assert claimed["id"] == j3["id"]  # beta 的任务进入 RUNNING

    agents = {row["agent_id"]: row for row in store.agent_progress()}
    assert set(agents) == {"alpha", "beta"}  # 未归因任务不制造假分母
    assert agents["alpha"] == {
        "agent_id": "alpha", "assigned": 2, "completed": 1, "failed": 1,
        "running": 0, "progress_measured": 50.0,
        "progress_basis": {"unit": "completed_jobs_of_attributed_jobs",
                           "completed": 1, "assigned": 2}}
    assert agents["beta"]["assigned"] == 1 and agents["beta"]["running"] == 1
    assert agents["beta"]["progress_measured"] == 0.0  # 运行中只压低，不发明


def test_attribution_is_stable_and_completion_lands_on_dispatcher(tmp_path):
    client = make_client(tmp_path)
    mission = create(client, "归因稳定", ["执行", "复核"])
    store = client.app.state.store
    job = store.enqueue_job(mission["id"], "p", dispatch={"agent_id": "goose"})
    store.enqueue_job(mission["id"], "p2", dispatch={"agent_id": "opencode"})
    store.finish_job(job["id"], "COMPLETED")
    agents = {row["agent_id"]: row for row in store.agent_progress()}
    # 每个任务只有一次 DISPATCH_QUEUED 归因：结局记在派发者名下，不漂移
    assert agents["goose"]["completed"] == 1
    assert agents["opencode"]["assigned"] == 1
    assert agents["opencode"]["completed"] == 0  # 仍在队列，不得预支完成数


def test_fleet_agent_progress_requires_admin_session(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from facai.api import create_app
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "fleet-admin-pass")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    client = TestClient(create_app(tmp_path))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": "fleet-admin-pass"}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    assert client.get("/api/v1/fleet/agent-progress").status_code == 200
    tenant = client.post("/api/v1/auth/register", json={
        "username": "fleet-tenant", "password": "fleet-pass-1234"})
    assert tenant.status_code == 201
    client.headers.update({"X-CSRF-Token": tenant.json()["csrf_token"]})
    assert client.get("/api/v1/fleet/agent-progress").status_code == 403
