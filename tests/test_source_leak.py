"""逆向克隆防护：客户可达面上源代码/内部路径/凭据形状零泄漏。

客户能接触的只有 HTTP API 与前端静态资产。本文件把四类出口逐一钉死：

* 错误卫生 —— 未处理异常只回纯文本 "Internal Server Error"；404/422 的
  detail 不含文件路径、traceback、内部模块名或环境变量名。回归点：
  secretbox 的主密钥失败文案曾带 master.key 的绝对路径与 FACAI_* 变量名，
  经 register-model / bind-channel 的 ``422 str(exc)`` 原样到达客户。
* 静态资产 —— /static 下 .py/.env/.map/密钥形状的后缀试探一律 404，绝不
  回源码；仓库顶层 .git/config 同样不可达。
* schema 面 —— FastAPI 的 /docs、/redoc、/openapi.json 关闭：登录客户也
  拿不到含 admin/ops 内部路由的完整 API 清单。
* 响应字段 —— mission 记录不带 workspace、产物行不带 path、事件 payload
  不带 path、run 结果不带 workspace/worktree 路径：部署布局
  （/opt/facai-cloud/data/workspaces/...）不进任何响应。

存储层为执行/取证保留原值；这里钉的是 API 边界（facai.api.public_*）。
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import facai.secretbox
from facai.api import (create_app, public_artifact, public_event, public_mission,
                       public_run_result)
from facai.channels.wechat import WeChatProvider

PASSWORD = "leak-probe-pass-123"


def build(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FACAI_MASTER_KEY", raising=False)
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    return app, client


def make_mission(client, title="泄漏探针"):
    return client.post("/api/v1/missions", json={
        "title": title, "original_input": f"原始要求：{title}", "steps": ["第一步"]}).json()


# --- 错误卫生：500/404/422 ------------------------------------------------------

def test_unhandled_exception_is_opaque_500(tmp_path, monkeypatch):
    """未处理异常只回纯文本 Internal Server Error：无 traceback、无路径、无模块名。"""
    app, client = build(tmp_path, monkeypatch)

    def boom(owner_id=None):
        raise RuntimeError("classified /opt/facai-cloud/data/master.key exploded")

    monkeypatch.setattr(app.state.store, "list", boom)
    raw = TestClient(app, raise_server_exceptions=False)
    session = raw.post("/api/v1/auth/login",
                       json={"username": "admin", "password": PASSWORD}).json()
    raw.headers.update({"X-CSRF-Token": session["csrf_token"]})
    response = raw.get("/api/v1/missions")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    for marker in ("RuntimeError", "traceback", "facai-cloud", "master.key", ".py"):
        assert marker not in response.text


def test_validation_422_details_carry_no_internal_shapes(tmp_path, monkeypatch):
    """自写 422 文案不泄漏内部形状：未知依赖、坏端点均只见业务语义。"""
    _, client = build(tmp_path, monkeypatch)
    r = client.post("/api/v1/missions", json={
        "title": "bad-dag", "original_input": "x",
        "steps": [{"label": "a", "depends_on": ["ghost"]}]})
    assert r.status_code == 422
    assert str(tmp_path) not in json.dumps(r.json())

    r = client.post("/api/v1/my/models", json={
        "name": "lan-model", "endpoint": "http://192.168.1.10:11434/v1",
        "model": "llama", "api_key": "sk-test-123"})
    assert r.status_code == 422
    detail = json.dumps(r.json(), ensure_ascii=False)
    assert "FACAI_ALLOW_PRIVATE_MODEL_ENDPOINTS" not in detail
    assert "FACAI_" not in detail
    assert str(tmp_path) not in detail


def test_validation_does_not_echo_passwords_or_whole_model_credentials(tmp_path, monkeypatch):
    _, client = build(tmp_path, monkeypatch)
    secret = 'synthetic-private-input-that-must-not-be-reflected'
    # A missing name otherwise makes Pydantic include the complete request body.
    response = client.post('/api/v1/my/models', json={
        'endpoint': 'https://example.invalid/v1', 'model': 'fixture', 'api_key': secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert all('input' not in error and 'ctx' not in error for error in response.json()['detail'])
    response = client.post('/api/v1/setup/bootstrap', json={
        'token': 'fixture-bootstrap-token', 'username': 'owner', 'password': secret*20})
    assert response.status_code == 422
    assert secret not in response.text and 'fixture-bootstrap-token' not in response.text
    assert response.json()['detail'][0]['loc'][-1] == 'password'


def test_master_key_failure_422_hides_path_and_env_name(tmp_path, monkeypatch):
    """主密钥文件损坏时，register-model 的 422 不带 master.key 路径/变量名。

    回归：SecretBoxError(f"{path} ...") 曾把服务器绝对路径送进客户可见 detail。
    """
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FACAI_MASTER_KEY", raising=False)
    (tmp_path / "master.key").write_bytes(b"not-a-fernet-key-at-all")
    client = TestClient(create_app(tmp_path))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})

    with pytest.raises(facai.secretbox.SecretBoxError) as exc:
        facai.secretbox.encrypt(tmp_path, "whatever")
    message = str(exc.value)
    assert str(tmp_path) not in message
    assert "master.key" not in message
    assert "FACAI" not in message

    r = client.post("/api/v1/my/models", json={
        "name": "m", "endpoint": "https://api.example.com/v1",
        "model": "m-1", "api_key": "sk-test-123"})
    assert r.status_code == 422
    detail = json.dumps(r.json(), ensure_ascii=False)
    assert str(tmp_path) not in detail
    assert "master.key" not in detail
    assert "FACAI" not in detail


# --- 静态资产与仓库形状 ----------------------------------------------------------

@pytest.mark.parametrize("probe", [
    "/static/api.py", "/static/facai.py", "/static/.env",
    "/static/facai-ui.css.map", "/static/sw.js.map", "/static/master.key",
    "/static/../secretbox.py", "/static/%2e%2e/api.py",
])
def test_static_suffix_probing_never_serves_source(tmp_path, monkeypatch, probe):
    _, client = build(tmp_path, monkeypatch)
    r = client.get(probe, follow_redirects=False)
    assert r.status_code in (401, 404), (probe, r.status_code)
    body = r.text
    for marker in ("Fernet", "import ", "def create_app", "SECRET"):
        assert marker not in body


def test_repository_toplevel_is_not_served(tmp_path, monkeypatch):
    _, client = build(tmp_path, monkeypatch)
    for probe in ("/.git/config", "/.env", "/facai/api.py", "/tests/conftest.py",
                  "/pyproject.toml", "/requirements.txt"):
        r = client.get(probe, follow_redirects=False)
        assert r.status_code in (401, 404), (probe, r.status_code)
        assert "import " not in r.text


# --- schema/docs 面 ------------------------------------------------------------

def test_schema_and_docs_endpoints_are_disabled(tmp_path, monkeypatch):
    """登录客户也拿不到 OpenAPI 清单（含 admin/ops 路由的完整枚举）。"""
    app, client = build(tmp_path, monkeypatch)
    client.post("/api/v1/auth/register",
                json={"username": "tenant-leak", "password": "tenant-pass-123"})
    tenant = TestClient(app)
    session = tenant.post("/api/v1/auth/login",
                          json={"username": "tenant-leak",
                                "password": "tenant-pass-123"}).json()
    tenant.headers.update({"X-CSRF-Token": session["csrf_token"]})
    for probe in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(probe).status_code == 404, probe
        assert tenant.get(probe).status_code == 404, probe


# --- 响应字段：文件系统布局不进响应 ----------------------------------------------

def test_mission_and_artifact_responses_carry_no_filesystem_layout(
        tmp_path, monkeypatch):
    """missions 列表/详情/auto、产物中心与 proof 都不带 workspace/path。"""
    app, client = build(tmp_path, monkeypatch)
    mission = make_mission(client)
    store = app.state.store
    workspace = Path(store.root) / "workspaces" / mission["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "out.txt").write_text("artifact", encoding="utf-8")
    store.add_artifact(mission["id"], "out.txt", workspace / "out.txt")

    for url in ("/api/v1/missions", f"/api/v1/missions/{mission['id']}"):
        text = client.get(url).text
        assert '"workspace"' not in text
        assert '"path"' not in text
        assert str(tmp_path) not in text

    auto = client.post("/api/v1/missions/auto", json={
        "title": "auto", "original_input": "一句话"}).json()
    assert "workspace" not in auto

    center = client.get("/api/v1/artifacts").json()
    assert center and all("path" not in row for row in center)
    assert str(tmp_path) not in json.dumps(center)

    proof = client.get(f"/api/v1/missions/{mission['id']}/proof").json()
    assert all("path" not in row for row in proof["artifacts"])
    assert str(tmp_path) not in json.dumps(proof, default=str)


def test_event_responses_carry_no_path_payload(tmp_path, monkeypatch):
    """/events 的 payload 不带 path 键（WORKTREE_* 曾带绝对路径）。

    SSE 流出口（/stream）与 /events 共用同一个 ``public_event`` 裁剪器
    （facai.api stream.generate 里同一处调用），形状由单元契约钉住；
    TestClient/ASGITransport 对 BaseHTTPMiddleware 包裹的无限 SSE 流
    无法取到响应头（既有限局，与生产 nginx SSE 通道无关），故不做实流断言。
    """
    app, client = build(tmp_path, monkeypatch)
    mission = make_mission(client)
    store = app.state.store
    store.append(mission["id"], "WORKTREE_CREATED",
                 {"path": str(store.root / "worktrees" / mission["id"]),
                  "branch": "facai/x"})
    events = client.get(f"/api/v1/missions/{mission['id']}/events").json()
    assert events
    for event in events:
        assert "path" not in (event.get("payload") or {})
    assert str(tmp_path) not in json.dumps(events)


def test_billing_responses_carry_no_internal_shapes(tmp_path, monkeypatch):
    """/billing/me、/billing/check、/pricing 只有价目与数字，无 env 键名/路径。"""
    _, client = build(tmp_path, monkeypatch)
    for url in ("/api/v1/billing/me", "/api/v1/billing/check", "/api/v1/pricing"):
        text = client.get(url).text
        assert "FACAI_" not in text, url
        assert str(tmp_path) not in text, url


def test_whatsapp_host_unconfigured_503_hides_internals(tmp_path, monkeypatch):
    """配对宿主未配置时诚实 503：不泄漏 env 名、桥地址或内部存储/会话形状。"""
    monkeypatch.delenv("FACAI_WHATSAPP_BRIDGE_URL", raising=False)
    _, client = build(tmp_path, monkeypatch)
    r = client.post("/api/v1/my/channels/whatsapp/host/start",
                    json={"phone": "8613800000000"})
    assert r.status_code == 503
    text = json.dumps(r.json(), ensure_ascii=False)
    for marker in ("FACAI_", "http", "bridge", "wa-auth", "baileys",
                   "data/providers", "hosting", ".env", "whatsapp.env"):
        assert marker not in text, marker


# --- wechat 登录失败不回显上游主机 ------------------------------------------------

def test_wechat_login_failure_does_not_echo_upstream_host(tmp_path):
    """上游 baseurl 落在非官方域时，失败 error 不得回显该主机名。"""
    provider = WeChatProvider({}, state_dir=tmp_path / "providers")

    async def handler(request):
        if request.url.path.endswith("get_bot_qrcode"):
            return httpx.Response(200, json={
                "qrcode": "qr-secret", "qrcode_img_content": "https://weixin.qq.com/qr"})
        return httpx.Response(200, json={
            "status": "confirmed", "bot_token": "bot-secret", "ilink_user_id": "uid-1",
            "baseurl": "https://internal-relay.corp.example:9000"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await provider.start_login(client)
            return await provider.poll_login(client)

    result = asyncio.run(scenario())
    assert result == {"state": "FAILED", "error": "INVALID_LOGIN_RESPONSE"}
    text = json.dumps(result)
    assert "internal-relay" not in text
    assert "bot-secret" not in text


# --- 边界裁剪器单元契约 ----------------------------------------------------------

def test_public_sanitizers_strip_only_layout_fields():
    """裁剪器只动布局字段：workspace/path 消失，其余字段原样保留。"""
    mission = public_mission({
        "id": "m1", "title": "t", "workspace": "/opt/facai-cloud/data/workspaces/m1",
        "steps": [{"id": "s1", "label": "l", "position": 1, "status": "WAITING",
                   "depends_on": [], "model_id": None}],
        "artifacts": [{"id": "a1", "mission_id": "m1", "name": "out.txt",
                       "path": "/opt/facai-cloud/data/workspaces/m1/out.txt",
                       "created_at": "2026-10-06"}]})
    assert "workspace" not in mission
    assert mission["id"] == "m1" and mission["title"] == "t"
    assert mission["artifacts"] == [{"id": "a1", "mission_id": "m1",
                                     "name": "out.txt", "created_at": "2026-10-06"}]

    event = public_event({"sequence": 1, "type": "WORKTREE_CREATED",
                          "payload": {"path": "/x", "branch": "b"},
                          "occurred_at": "2026-10-06"})
    assert event["payload"] == {"branch": "b"}
    assert public_event({"sequence": 2, "type": "OUTPUT",
                         "payload": {"stream": "stdout", "line": "hi"}})["payload"] == \
        {"stream": "stdout", "line": "hi"}

    artifact = public_artifact({"id": "a1", "name": "n", "path": "/x/n", "extra": 1})
    assert artifact == {"id": "a1", "name": "n", "extra": 1}

    run = public_run_result({"mission_id": "m1", "workspace": "/x/ws",
                             "status": "OPENCODE_COMPLETED", "exit_code": 0,
                             "worktree": {"path": "/x/wt", "branch": "b"},
                             "stdout": "ok"})
    assert "workspace" not in run
    assert run["worktree"] == {"branch": "b"}
    assert run["stdout"] == "ok" and run["exit_code"] == 0


# --- scheduler 崩溃文本落库前打码 --------------------------------------------------

def test_scheduler_crash_text_stores_no_filesystem_paths(tmp_path, monkeypatch):
    """平台崩溃的 str(exc) 带 OSError 路径/网关 URL，落库前必须打码。

    job error 与 JOB_FAILED/STEP_FAILED 事件都经 /jobs、/events 到客户；
    暂态路由分类仍读原始文本（打码只影响存储形状）。
    """
    import facai.scheduler as scheduler_module
    from facai.opencode import OpenCodeAdapter
    from facai.registries import Registries
    from facai.scheduler import Scheduler
    from facai.store import Store

    store = Store(tmp_path)
    mission = store.create("崩溃打码", "input", [{"id": "s1", "label": "A"}])
    store.bind(mission["id"], "opencode", "deepseek-flash")
    store.enqueue_job(mission["id"], "run it", step_id="s1")
    scheduler = Scheduler(store, OpenCodeAdapter(binary="opencode"),
                          Registries(store), tmp_path, worker_id="leak-test")

    secret_root = "/opt/facai-cloud/data/workspaces/xyz"
    monkeypatch.setattr(
        scheduler_module, "execute_mission_task",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(f"[Errno 2] No such file or directory: '{secret_root}',"
                         f" gateway https://internal-gw.corp.example/v1 down")))

    scheduler.run_pending()
    job = store.jobs(mission["id"])[0]
    assert job["status"] == "FAILED"
    assert secret_root not in job["error"]
    assert "internal-gw" not in job["error"]
    assert "[路径]" in job["error"]

    events = store.events(mission["id"])
    failed = [event for event in events if event["type"] == "JOB_FAILED"]
    assert failed and secret_root not in json.dumps(failed)
    assert "internal-gw" not in json.dumps(failed)
