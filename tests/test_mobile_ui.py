"""Real-browser verification of the mobile console and login flow.

These tests run the FastAPI app under uvicorn and drive a Chromium (Playwright)
device viewport. They are skipped when Playwright or its browser is unavailable
so the core test suite stays runnable everywhere. Shared fixtures live in
``tests/conftest.py``.
"""

import json
import os

import pytest


def _launch(playwright):
    try:
        executable = os.getenv('FACAI_TEST_CHROMIUM')
        browser = playwright.chromium.launch(**({'executable_path': executable, 'args': ['--no-sandbox']} if executable else {}))
    except Exception as exc:  # browser binary not installed
        pytest.skip(f"Playwright chromium unavailable: {exc}")
    # RUNNING 阶段的循环动画会让 Playwright 的 stability 检测偶发超时；
    # 统一模拟"减少动画"偏好，页面 CSS 本就尊重该偏好。
    browser.on("page", lambda page: page.emulate_media(reduced_motion="reduce"))
    return browser


def _phone(browser):
    return browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                            is_mobile=True, has_touch=True)


def _create_mission(request, base, title="移动端核验"):
    response = request.post(
        base + "/api/v1/missions",
        data=json.dumps({"title": title, "original_input": "原始要求逐字保留",
                         "steps": ["规划", "开发", "测试"]}),
        headers={"Content-Type": "application/json"})
    assert response.ok, response.text
    return response.json()


def test_mobile_console_layout_has_no_overflow_and_is_tappable(console_server, tmp_path, playwright_api):
    errors = []
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = _phone(browser)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # Creation lives in a collapsible secondary section that starts closed.
        page.goto(console_server + "/", wait_until="load")
        assert page.eval_on_selector("#create-panel", "el => el.open") is False
        page.click("#create-panel > summary")
        assert page.eval_on_selector("#create-panel", "el => el.open") is True
        page.fill('input[name="title"]', "移动端核验")
        page.fill('textarea[name="original_input"]', "原始要求逐字保留")
        page.fill('textarea[name="steps"]', "规划\n开发")
        page.click('#create-form button[type="submit"]')
        page.wait_for_selector("#detail .pipeline .stage")

        # Re-open the deep link the way a real user would; the progress bar and the
        # current stage must be visible in the first 390x844 screen.
        mission_id = page.url.split("mission=")[-1]
        page.goto(f"{console_server}/?mission={mission_id}", wait_until="load")
        page.wait_for_selector("#detail [role=progressbar]")
        progress = page.eval_on_selector(
            "#detail [role=progressbar]",
            "el => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; }")
        assert 0 <= progress["top"] and progress["bottom"] <= 844, \
            f"progress bar not on the first phone screen: {progress}"
        current = page.eval_on_selector("#detail #current-stage", "el => el.getBoundingClientRect().bottom")
        assert current <= 844, "current stage not on the first phone screen"

        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"horizontal overflow of {overflow}px"

        widths = page.eval_on_selector_all(
            "#detail .pipeline .stage", "els => els.map(e => e.getBoundingClientRect().width)")
        assert widths and max(widths) - min(widths) < 5, "pipeline should stack vertically on phones"

        heights = page.eval_on_selector_all(
            "#detail .pipeline .stage", "els => els.map(e => e.getBoundingClientRect().height)")
        assert min(heights) >= 44, f"stage tap targets too small: {heights}"

        # The stage button re-renders while live output streams in, so a
        # stability-waiting click races the renderer; the tap-target assertion
        # only needs the element hit, which force=True provides.
        page.click("#detail .pipeline .workflow-step[data-step]", force=True)
        page.wait_for_selector("#detail #timeline")
        action_heights = page.eval_on_selector_all(
            "#detail .actions button", "els => els.map(e => e.getBoundingClientRect().height)")
        assert min(action_heights) >= 40, f"action tap targets too small: {action_heights}"

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        assert page.eval_on_selector("header", "e => e.getBoundingClientRect().top") == 0, \
            "header must stay sticky on phones"
        assert page.get_attribute("#detail #timeline", "aria-live") == "polite"

        shot = tmp_path / "mobile-console.png"
        page.screenshot(path=str(shot), full_page=True)
        assert shot.exists() and shot.stat().st_size > 0
        browser.close()
    assert not errors, f"console errors: {errors}"


def test_mobile_login_flow(protected_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = _phone(browser)
        page.goto(protected_server + "/", wait_until="load")
        page.wait_for_selector("#login-form")

        page.fill("#password", "wrong")
        page.click("#submit")
        page.wait_for_function("document.querySelector('#error').textContent.length > 0")

        page.fill("#password", "mobile-pass")
        page.click("#submit")
        page.wait_for_selector("header #logout", timeout=10000)
        assert page.url.rstrip("/") == protected_server

        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1
        browser.close()


def test_mobile_billing_metrics_wrap_without_horizontal_overflow(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = _phone(browser)
        response = page.request.post(
            f"{console_server}/api/v1/missions",
            data=json.dumps({"title": "移动端费用显示", "original_input": "用量验收", "steps": ["长阶段名称用于费用布局验证"]}),
            headers={"Content-Type": "application/json"})
        assert response.ok, response.text
        mission = response.json()
        step = mission["steps"][0]
        for event in (
            {"type": "OPENCODE_COMPLETED", "step_id": step["id"], "payload": {
                "cost": 0.001234, "tokens": {"total": 7656, "input_tokens": 4000, "output_tokens": 3656}}},
            {"type": "COST_RECORDED", "payload": {"amount": 0.001234, "tokens": 7656, "currency": "USD"}},
        ):
            created = page.request.post(
                f"{console_server}/api/v1/missions/{mission['id']}/events",
                data=json.dumps(event), headers={"Content-Type": "application/json"})
            assert created.ok, created.text
        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        panel = page.locator("#detail .billing-panel:not(.my-billing)")
        panel.wait_for()
        assert "7,656" in panel.inner_text()
        assert "$0.0123" in panel.locator(".billing-metric").nth(5).inner_text()  # money shows 2-4 decimals, multiplier default 10
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"billing UI caused horizontal overflow of {overflow}px"
        panel_box = panel.bounding_box()
        viewport_width = page.evaluate("document.documentElement.clientWidth")
        assert panel_box["x"] >= 0 and panel_box["x"] + panel_box["width"] <= viewport_width + 1
        browser.close()


# ── mobile.html 真实接口路径（登录→下单→监控→绑定）────────────────────────────
# mobile.html 已从静态演示升级为真实客户端；以下用例打真实后端：
# 注册/登录、POST /missions/auto 下单、GET /missions/{id} 轮询进度、
# /my/channels/* 绑定与解绑、/auth/logout 退出。

MOBILE_PAGE = "/static/mobile.html"
MOBILE_USER = "phoneuser"
MOBILE_PASS = "phone-pass-123"
MOBILE_TG_TOKEN = "111222333:AAAmobileFakeToken_abc123XYZ"


def _goto_mobile(page, base):
    page.goto(base + MOBILE_PAGE, wait_until="load")
    page.wait_for_selector("#login-form")


def _expected_errors(*needles):
    """Chromium 会把任何 4xx fetch 记成 console error；本文件故意探的
    失败路径（401 错密码 / 422 非法凭据）与 SSE 断流噪声按口径过滤。"""
    def _keep(error):
        return not any(needle in error for needle in needles)
    return _keep


def test_mobile_register_order_and_monitor_follow_the_real_backend(
        protected_server, playwright_api):
    """注册即登录 → 一句话真下单 → 监控渲染后端真实进度（不是本地假数）。"""
    base = protected_server
    errors = []
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = _phone(browser)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        _goto_mobile(page, base)
        # 未登录事实：登录页是第一屏，首页不展示演示数据
        assert page.eval_on_selector("#screen-login", "el => el.classList.contains('active')")
        page.click('.bottomnav .nav-btn[data-nav="screen-home"]')
        assert "未登录：不展示演示数据" in page.inner_text("#home-active")
        # 从首页未登录提示回到登录页（真实用户路径）
        page.click('#home-active .facai-btn')
        page.wait_for_selector("#screen-login.active")

        # 错密码 → 诚实报错，不假成功
        page.fill("#login-username", "admin")
        page.fill("#login-password", "totally-wrong")
        page.click("#login-submit")
        page.wait_for_function(
            "document.querySelector('#login-error').textContent.includes('用户名或密码不正确')")
        assert "用户名或密码不正确" in page.inner_text("#login-error")

        # 切注册模式，注册即登录（201 + 会话 cookie + csrf_token）
        page.click("#login-toggle")
        page.fill("#login-username", MOBILE_USER)
        page.fill("#login-password", MOBILE_PASS)
        page.click("#login-submit")
        page.wait_for_selector("#screen-home.active")
        assert page.inner_text("#home-session-chip") == MOBILE_USER

        # 一句话真下单：POST /missions/auto → 直达监控并显示任务短编号
        page.fill("#wish", "做一个购物网站")
        page.click(".input-row .send")
        page.wait_for_selector("#screen-monitor.active")
        page.wait_for_function("document.querySelector('#monitor-body').hidden === false")
        mission_id = page.evaluate("window.facaiCurrentMissionId()")
        assert mission_id, "下单后监控视图应持有真实 mission id"
        assert mission_id[:8] in page.inner_text("#monitor-id"), "监控页必须显示任务短编号"

        # 进度跟随后端：推进一步，点「刷新进度」立即拉取，渲染 == 服务端数值
        csrf = page.evaluate("window.facaiCsrf()")
        assert csrf, "登录响应的 csrf_token 应已落内存"
        first_step = page.request.get(
            f"{base}/api/v1/missions/{mission_id}").json()["steps"][0]["id"]
        advanced = page.request.patch(
            f"{base}/api/v1/missions/{mission_id}/steps/{first_step}",
            data=json.dumps({"status": "RUNNING"}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
        assert advanced.ok, advanced.text
        page.click("#monitor-refresh")
        page.wait_for_function(
            "document.querySelector('#monitor-ring').getAttribute('aria-valuenow') !== '0'")
        backend = page.request.get(f"{base}/api/v1/missions/{mission_id}").json()
        assert page.get_attribute("#monitor-ring", "aria-valuenow") == str(backend["progress"])
        assert "发财正在" in page.inner_text("#monitor-doing-text")
        assert page.locator("#monitor-steps li").count() == len(backend["steps"])

        # 任务过程：真实事件流（含 PROMPT_CONVERTED，无假时间）
        page.click("#monitor-timeline-btn")
        page.wait_for_selector("#screen-timeline.active")
        page.wait_for_function(
            "document.querySelectorAll('#timeline-list .facai-timeline-item').length > 0")
        timeline = page.inner_text("#timeline-list")
        assert "PROMPT_CONVERTED" in timeline
        browser.close()
    keep = _expected_errors("401", "/stream", "ERR_ABORTED")
    unexpected = [e for e in errors if keep(e)]
    assert not unexpected, f"console errors: {unexpected}"


def test_mobile_channels_bind_unbind_and_logout_through_the_real_backend(
        protected_server, playwright_api, monkeypatch):
    """「我的」页渠道的真实绑定：422 原样透出 → 合法绑定回显 → 解绑 → 退出登录。

    与 tests/test_console_layout.py 的同面板用例同口径：绑定端点落库前的
    真实凭证预检按 FACAI_SKIP_CREDENTIAL_PREFLIGHT=1 跳过（服务线程与用例
    同进程、逐请求读环境变量），本用例的契约是不触网。
    """
    monkeypatch.setenv("FACAI_SKIP_CREDENTIAL_PREFLIGHT", "1")
    base = protected_server
    errors = []
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = _phone(browser)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        _goto_mobile(page, base)
        page.fill("#login-username", "admin")
        page.fill("#login-password", "mobile-pass")
        page.click("#login-submit")
        page.wait_for_selector("#screen-home.active")

        # 进入「我的」读取真实绑定状态：初始全未绑定、解绑隐藏
        page.click('.bottomnav .nav-btn[data-nav="screen-me"]')
        page.wait_for_selector('[data-channel-bound="telegram"]:has-text("未绑定")')
        assert page.inner_text('[data-channel-bound="discord"]') == "未绑定"
        assert page.inner_text('[data-channel-bound="slack"]') == "未绑定"
        assert page.locator('[data-unbind-channel="telegram"]').is_hidden()
        # 凭据框保持密码框
        for field in ("#m-tg-bot-token", "#m-line-access-token",
                      "#m-discord-bot-token", "#m-slack-bot-token"):
            assert page.get_attribute(field, "type") == "password"

        # 空表单 → 本地诚实提示，不发请求
        page.click('#mobile-channel-binding-form button[type="submit"]')
        assert "请先填写完整的凭据" in page.inner_text('[data-channel-error="telegram"]')

        # 非法 token → 后端 422 原样透出，状态保持未绑定
        page.fill("#m-tg-bot-token", "abc:def")
        page.fill("#m-tg-chat-id", "556677")
        page.click('#mobile-channel-binding-form button[type="submit"]')
        page.wait_for_function(
            "document.querySelector('[data-channel-error=\"telegram\"]').textContent.includes('绑定失败')")
        assert page.inner_text('[data-channel-bound="telegram"]') == "未绑定"

        # 合法绑定 → pill 翻转、meta 回显接收 ID 与绑定时间、解绑可见
        page.fill("#m-tg-bot-token", MOBILE_TG_TOKEN)
        page.click('#mobile-channel-binding-form button[type="submit"]')
        page.wait_for_selector('[data-channel-bound="telegram"]:has-text("已绑定")')
        meta = page.inner_text('[data-channel-meta="telegram"]')
        assert "556677" in meta and "绑定于" in meta
        assert page.locator('[data-unbind-channel="telegram"]').is_visible()
        listing = page.request.get(f"{base}/api/v1/my/channels").json()
        assert listing["telegram"]["bound"] is True and listing["telegram"]["chat_id"] == "556677"

        # Discord 同一路径（四个渠道同一契约，再验一个非 Telegram 的）
        page.fill("#m-discord-bot-token",
                  "MTIzNDU2Nzg5MDEyMzQ1Njc4.discordMobileFakeToken.xYz0987654321ABCDEFGh")
        page.fill("#m-discord-channel-id", "987654321098765432")
        page.click('form[data-channel-form="discord"] button[type="submit"]')
        page.wait_for_selector('[data-channel-bound="discord"]:has-text("已绑定")')

        # 解绑回落：pill 回未绑定、解绑按钮重新隐藏、后端如实 bound=False
        page.click('[data-unbind-channel="telegram"]')
        page.wait_for_selector('[data-channel-bound="telegram"]:has-text("未绑定")')
        assert page.locator('[data-unbind-channel="telegram"]').is_hidden()
        listing = page.request.get(f"{base}/api/v1/my/channels").json()
        assert listing["telegram"]["bound"] is False
        assert listing["discord"]["bound"] is True

        # 退出登录：会话清除，回登录页，会话 chip 恢复「未登录」
        page.click("#me-logout-btn")
        page.wait_for_selector("#screen-login.active")
        page.click('.bottomnav .nav-btn[data-nav="screen-home"]')
        assert page.inner_text("#home-session-chip") == "未登录"
        # 退出后下单入口诚实拦截
        page.fill("#wish", "退出后再试一单")
        page.click(".input-row .send")
        page.wait_for_selector("#screen-login.active")
        browser.close()
    keep = _expected_errors("422")
    unexpected = [e for e in errors if keep(e)]
    assert not unexpected, f"console errors: {unexpected}"


def test_mobile_files_real_artifacts_preview_download_and_session_clear(protected_server, tmp_path, playwright_api):
    """Phone users see only their artifacts; text stays inert and clears on logout."""
    from pathlib import Path
    from facai.store import Store

    store = Store(tmp_path)
    mission = store.create('手机文件验收', '保存原话', ['输出报告'], owner_id='admin')
    content = '<script>window.artifactExecuted=true</script>\n真实报告'
    path = Path(mission['workspace']) / 'report.txt'
    path.write_text(content, encoding='utf-8')
    store.add_artifact(mission['id'], path.name, path)
    other = store.create('其他用户', '原话', ['输出'], owner_id='someone-else')
    private = Path(other['workspace']) / 'private.txt'
    private.write_text('另一租户私有数据', encoding='utf-8')
    store.add_artifact(other['id'], private.name, private)
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = _phone(browser)
        _goto_mobile(page, protected_server)
        page.fill('#login-username', 'admin')
        page.fill('#login-password', 'mobile-pass')
        page.click('#login-submit')
        page.wait_for_selector('#screen-home.active')
        page.click('[data-nav="screen-files"]')
        page.wait_for_selector('#files-list .artifact-card')
        assert page.locator('#files-list .artifact-card').count() == 1
        assert 'private.txt' not in page.inner_text('#files-list')
        page.click('#files-list button')
        page.wait_for_function("document.querySelector('#artifact-content').textContent.includes('真实报告')")
        assert page.inner_text('#artifact-content') == content
        assert page.evaluate('window.artifactExecuted === undefined')
        with page.expect_download() as downloaded:
            page.click('#artifact-download')
        assert Path(downloaded.value.path()).read_text(encoding='utf-8') == content
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.click('[data-nav="screen-me"]')
        page.click('#me-logout-btn')
        page.wait_for_selector('#screen-login.active')
        assert page.inner_text('#artifact-content') == ''
        page.click('[data-nav="screen-files"]')
        assert '请先登录' in page.inner_text('#files-status')
        assert page.locator('#files-list .artifact-card').count() == 0
        browser.close()


def test_mobile_pwa_shell_available_offline_without_caching_api(protected_server, playwright_api):
    """Loopback is a secure browser context; cached shell still boots honestly offline."""
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True)
        page = context.new_page()
        page.goto(protected_server + MOBILE_PAGE)
        page.evaluate('navigator.serviceWorker.ready.then(() => true)')
        page.wait_for_function('navigator.serviceWorker.controller !== null')
        manifest = page.request.get(protected_server + '/static/mobile.webmanifest').json()
        assert manifest['start_url'] == MOBILE_PAGE
        page.wait_for_function("async () => !!(await (await caches.open('facai-shell-v2')).match('/static/mobile.html'))")
        cached = page.evaluate("caches.open('facai-shell-v2').then(c => c.keys()).then(keys => keys.map(k => new URL(k.url).pathname))")
        assert '/static/facai-ui.css' in cached
        assert not any(path.startswith('/api/') for path in cached)
        context.set_offline(True)
        page.reload(wait_until='load')
        page.wait_for_selector('#screen-login.active')
        assert '连不上服务器' in page.inner_text('#login-error')
        context.set_offline(False)
        context.close()
        browser.close()
