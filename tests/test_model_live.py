"""Live upstream connectivity probe — pytest.mark.live, opt-in only.

注册表纪律（MODEL-001 ⑤）"真实连接和模拟失败分开验证"的 live 半边：
模拟层次全部留在 test_gateway.py（MockTransport）/ test_model_connectivity.py
（monkeypatch）/ test_router.py（stub adapter），本文件只装真实上游探测。

默认套件永不触网：pytest.ini 注册了 ``live`` marker，常规跑法是
``-m "not live"``（conftest 的 hermetic_upstream 全局断网桩保持生效）。
手动验收（部署前/换 key 后）显式跑::

    FACAI_LIVE_API_KEY=sk-... python -m pytest tests/test_model_live.py -m live

绕开全局网络桩的方式（两处，均有标注）：
1. ``hermetic_upstream`` 会删光所有 *_API_KEY 环境变量并把 facai.api.complete
   换成假的 auth 失败桩。真实 key 在**模块导入期**（fixture 运行之前）捕获到
   模块常量，用例体内再 monkeypatch.setenv 回写；complete 用 monkeypatch 还原
   成真实的 facai.gateway.complete（fixtures 先于测试体运行，测试体的
   monkeypatch 覆盖后者的桩）。
2. ``hermetic_model_registration_dns`` 的 DNS stub 按模块名白名单生效，
   本文件不在名单内，天然走真实解析。

密钥纪律：本文件只读环境变量名取值，代码里没有任何 key 字面量。
"""

import os

import pytest
from fastapi.testclient import TestClient

import facai.gateway
from facai.api import create_app
from facai.gateway import OUTCOME_LABELS

# 导入期捕获（先于 conftest 的 autouse 断网桩删除 *_API_KEY）。
LIVE_API_KEY = os.environ.get("FACAI_LIVE_API_KEY", "")
LIVE_ENDPOINT = os.environ.get("FACAI_LIVE_ENDPOINT", "https://api.deepseek.com")
LIVE_MODEL = os.environ.get("FACAI_LIVE_MODEL", "deepseek-chat")
LIVE_KEY_ENV = "FACAI_LIVE_API_KEY"          # 网关经 api_key_env 从进程环境取值

PASSWORD = "live-probe-pass"
MODEL_KEY = "facai-live-probe"


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    return create_app(tmp_path)


@pytest.mark.live
def test_manual_probe_reaches_real_upstream(tmp_path, monkeypatch):
    """手动探测端点全链路打真实上游（含真实 DNS/TLS/计费调用）。

    断言的是"真实上游完成一次对话并回报 token"——不是模拟分类，所以
    这条用例只应在给了真实 key 的验收场景跑；无 key 时诚实 skip，
    绝不伪造成功。
    """
    if not LIVE_API_KEY:
        pytest.skip("FACAI_LIVE_API_KEY not set — live probe skipped (no fake upstream)")

    # 还原真实网关：覆盖 hermetic_upstream 的 no_upstream 桩（见模块 docstring）。
    monkeypatch.setattr("facai.api.complete", facai.gateway.complete)
    # 网关按 api_key_env 读进程环境；断网桩删掉的值在这里回写（仅本用例内）。
    monkeypatch.setenv(LIVE_KEY_ENV, LIVE_API_KEY)

    client = TestClient(_app(tmp_path, monkeypatch))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    headers = {"X-CSRF-Token": session["csrf_token"]}

    registered = client.put(f"/api/v1/registries/models/{MODEL_KEY}", headers=headers, json={
        "name": "Live Probe", "provider": "Live",
        "endpoint": LIVE_ENDPOINT, "model": LIVE_MODEL,
        "api_key_env": LIVE_KEY_ENV})
    assert registered.status_code == 200, registered.text

    response = client.post(f"/api/v1/registries/models/{MODEL_KEY}/test", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    # 真实连接验收：上游真的完成了一次对话，并回报了真实 token 用量。
    assert body["model_id"] == MODEL_KEY
    assert body["status"] in OUTCOME_LABELS
    assert body["ok"] is True, f"live upstream did not complete: {body['status']} / {body['label']}"
    assert isinstance(body["latency_ms"], (int, float)) and body["latency_ms"] >= 0
    assert body["usage"] and body["usage"].get("total_tokens", 0) > 0
    # 密钥纪律：凭据永不回显。
    assert LIVE_API_KEY not in response.text
