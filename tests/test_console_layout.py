"""Desktop-first-screen and deep-link acceptance for Mission Control.

The product complaint was that opening Mission Control showed the creation
forms but not the selected mission's real progress, so these tests prove in a
real Chromium that a 1440x900 first screen shows the progress bar, the current
stage and the pipeline, that creation/registration are collapsed by default,
and that a ``?mission=<id>`` deep link survives the login round-trip.
"""

import base64
import json
import os
from pathlib import Path

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


def _json_headers(extra=None):
    headers = {"Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def _create_mission(request, base, title, steps, headers=None):
    response = request.post(
        base + "/api/v1/missions",
        data=json.dumps({"title": title, "original_input": f"原始要求：{title}", "steps": steps}),
        headers=_json_headers(headers))
    assert response.ok, response.text
    return response.json()


def _advance(request, base, mission_id, step_id, status, headers=None):
    response = request.patch(
        f"{base}/api/v1/missions/{mission_id}/steps/{step_id}",
        data=json.dumps({"status": status}), headers=_json_headers(headers))
    assert response.ok, response.text


def test_desktop_first_screen_shows_real_progress_and_pipeline(console_server, playwright_api):
    base = console_server
    errors = []
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        mission = _create_mission(page.request, base, "桌面首屏核验", ["规划", "开发", "测试"])
        first = mission["steps"][0]["id"]
        _advance(page.request, base, mission["id"], first, "RUNNING")
        _advance(page.request, base, mission["id"], first, "COMPLETED")

        page.goto(f"{base}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail [role=progressbar]")

        # One of three completed steps is 33.33%; active work is not guessed.
        assert page.get_attribute("#detail [role=progressbar]", "aria-valuenow") == "33.33"
        assert "33%" in page.inner_text("#detail .overview .pct")

        # The complaint: progress must be on the first screen without scrolling.
        box = page.eval_on_selector(
            "#detail [role=progressbar]",
            "el => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; }")
        assert 0 <= box["top"] and box["bottom"] <= 900, f"progress bar below the fold: {box}"

        current_bottom = page.eval_on_selector("#detail #current-stage", "el => el.getBoundingClientRect().bottom")
        assert current_bottom <= 900, "current stage below the fold"
        assert "开发" in page.inner_text("#detail #current-stage"), "current stage should be the next waiting step"

        stage_top = page.eval_on_selector("#detail .pipeline .stage", "el => el.getBoundingClientRect().top")
        assert stage_top <= 900, "pipeline not visible on the first screen"

        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"desktop horizontal overflow of {overflow}px"

        # Creation and registration are secondary, collapsed sections on desktop too.
        assert page.eval_on_selector("#create-panel", "el => el.open") is False
        assert page.eval_on_selector("#registry-panel", "el => el.open") is False
        assert page.eval_on_selector("#create-panel", "el => !!el.querySelector('form')") is True

        browser.close()
    assert not errors, f"console errors: {errors}"


def test_progress_bar_width_aria_and_label_agree(console_server, playwright_api):
    base = console_server
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        mission = _create_mission(page.request, base, "进度一致性", ["规划", "开发", "测试"])
        first = mission["steps"][0]["id"]
        _advance(page.request, base, mission["id"], first, "RUNNING")
        _advance(page.request, base, mission["id"], first, "COMPLETED")

        page.goto(f"{base}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail [role=progressbar]")

        rendered = page.eval_on_selector(
            "#detail .overview",
            "el => { const bar = el.querySelector('[role=progressbar]');"
            " const fill = bar.querySelector('span');"
            " return { now: bar.getAttribute('aria-valuenow'),"
            " label: bar.getAttribute('aria-label'),"
            " width: fill.style.width,"
            " text: el.querySelector('.pct').textContent }; }")
        # Width, aria-valuenow and the visible label share the measured basis.
        assert rendered["now"] == "33.33"
        assert rendered["width"] == "33.33%"
        assert "33.33%" in rendered["text"]
        assert "33.33%" in rendered["label"]
        browser.close()


def test_pipeline_nodes_and_integer_stage_progress_follow_backend(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        mission = _create_mission(page.request, console_server, "节点状态", ["规划", "开发", "测试"])
        first, second = (step["id"] for step in mission["steps"][:2])
        _advance(page.request, console_server, mission["id"], first, "RUNNING")
        _advance(page.request, console_server, mission["id"], first, "COMPLETED")
        _advance(page.request, console_server, mission["id"], second, "RUNNING")

        backend = page.request.get(f"{console_server}/api/v1/missions/{mission['id']}").json()
        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail .pipeline .stage")

        stages = page.eval_on_selector_all(
            "#detail .pipeline .stage:not([data-step=''])",
            "els => els.map(el => ({label: el.querySelector('.rail-title b').textContent.trim(),"
            "aria: el.getAttribute('aria-label'), node: el.querySelector('.node').textContent.trim(),"
            "pct: el.querySelector('.rail-metrics').textContent.split('进度 ')[1].split(' · ')[0].split('%')[0],"
            "width: el.querySelector('.stage-progress span').style.width}))")
        assert len(stages) == len(backend["steps"]) == 3
        for rendered, actual in zip(stages, backend["steps"]):
            pct = (f"{actual['progress_measured']:.2f}%"
                   if actual['progress_measured'] is not None else "未计量")
            assert rendered["label"] == actual["label"]
            assert rendered["pct"] == pct.removesuffix("%")
            assert rendered["width"] == (f"{actual['progress_measured']:g}%"
                                         if actual['progress_measured'] is not None else "0%")
            assert f"进度 {pct}" in rendered["aria"]
        assert [stage["pct"] for stage in stages] == ["100.00", "未计量", "0.00"]
        assert stages[0]["node"] == "✓" and stages[1]["node"] == "2"

        # The rail runs top-to-bottom in dependency order, joined by the flow line.
        boxes = page.eval_on_selector_all(
            "#detail .pipeline .workflow-step", "els => els.map(el => { const r=el.getBoundingClientRect(); return {x:r.x,y:r.y}; })")
        assert all(boxes[i]["y"] < boxes[i+1]["y"] for i in range(len(boxes)-1))
        line = page.evaluate(
            "getComputedStyle(document.querySelector('#detail .rail-steps'), '::before').backgroundImage")
        assert "gradient" in line, "the run rail must draw its flow line"
        browser.close()


def test_dag_branches_merges_and_real_step_events_render(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        response = page.request.post(
            f"{console_server}/api/v1/missions",
            data=json.dumps({"title": "分支汇合", "original_input": "dag", "steps": [
                {"label": "准备"},
                {"label": "支路 A", "depends_on": ["准备"]},
                {"label": "支路 B", "depends_on": ["准备"]},
                {"label": "汇合", "depends_on": ["支路 A", "支路 B"]},
            ]}), headers=_json_headers())
        assert response.ok, response.text
        mission = response.json()
        root, branch_a, branch_b, join = mission["steps"]
        _advance(page.request, console_server, mission["id"], root["id"], "RUNNING")
        _advance(page.request, console_server, mission["id"], root["id"], "COMPLETED")
        _advance(page.request, console_server, mission["id"], branch_a["id"], "RUNNING")
        for line in ("older: warning", "renderer: missing asset"):
            error_response = page.request.post(
                f"{console_server}/api/v1/missions/{mission['id']}/events",
                data=json.dumps({"type": "ERROR", "step_id": branch_b["id"],
                                 "payload": {"line": line}}), headers=_json_headers())
            assert error_response.ok, error_response.text

        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail .pipeline-graph .workflow-step")
        geometry = page.eval_on_selector_all(
            "#detail .pipeline-graph .workflow-step",
            "els => els.map(el => ({id:el.dataset.step,label:el.querySelector('.label').textContent.trim(),"
            "deps:el.querySelector('.stage-deps').textContent,"
            "x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y,error:el.querySelector('.stage-error')?.textContent||'',"
            "meta:el.querySelector('.meta').textContent}))")
        assert len(geometry) == 4
        by_label = {step["label"]: step for step in geometry}
        assert by_label["准备"]["deps"] == "起点阶段"
        assert by_label["支路 A"]["deps"] == "依赖：准备"
        assert by_label["支路 B"]["deps"] == "依赖：准备"
        assert by_label["汇合"]["deps"] == "依赖：支路 A、支路 B"
        assert by_label["准备"]["x"] < by_label["支路 A"]["x"] < by_label["汇合"]["x"]
        assert by_label["支路 A"]["y"] != by_label["支路 B"]["y"]
        assert by_label["准备"]["error"] == ""
        assert "最近错误：renderer: missing asset" in by_label["支路 B"]["error"]
        assert "用时" in by_label["准备"]["meta"]
        paths = page.eval_on_selector_all(
            "#workflow-edges path", "els => els.map(el => ({class:el.getAttribute('class'),d:el.getAttribute('d'),stroke:getComputedStyle(el).stroke}))")
        assert len(paths) == 4 and all(path["d"].startswith("M ") for path in paths)
        assert any(path["class"] == "complete" and "74, 222, 128" in path["stroke"] for path in paths)

        # On a phone, the same dependency graph stacks topologically and keeps its wires.
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        overflow = page.evaluate("document.documentElement.scrollWidth-document.documentElement.clientWidth")
        metrics = page.evaluate("""() => [...document.querySelectorAll('body *')].map(el => {
          const r=el.getBoundingClientRect(),s=getComputedStyle(el);
          return {tag:el.tagName,id:el.id,cls:el.className?.baseVal||el.className,
            left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width),
            scrollWidth:el.scrollWidth,display:s.display,minWidth:s.minWidth,overflow:s.overflow};
        }).filter(x=>x.right>innerWidth+1||x.left< -1).slice(0,12)""")
        assert overflow <= 1, f"mobile DAG horizontal overflow: {overflow}px; elements: {metrics}"
        mobile_rows = page.eval_on_selector_all(
            "#detail .pipeline-graph .workflow-step", "els => els.map(el => el.getBoundingClientRect().y)")
        assert mobile_rows == sorted(mobile_rows)
        assert page.locator("#workflow-edges path").count() == 4
        browser.close()


def test_parallel_running_nodes_show_real_progress_and_reduced_motion(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        response = page.request.post(
            f"{console_server}/api/v1/missions",
            data=json.dumps({"title": "并行运行反馈", "original_input": "并行测试", "steps": [
                {"label": "准备"},
                {"label": "工作 A", "depends_on": ["准备"]},
                {"label": "工作 B", "depends_on": ["准备"]},
                {"label": "等待节点", "depends_on": ["工作 A", "工作 B"]},
            ]}), headers=_json_headers())
        assert response.ok, response.text
        mission = response.json()
        root, first, second, waiting = mission["steps"]
        _advance(page.request, console_server, mission["id"], root["id"], "RUNNING")
        _advance(page.request, console_server, mission["id"], root["id"], "COMPLETED")
        _advance(page.request, console_server, mission["id"], first["id"], "RUNNING")
        _advance(page.request, console_server, mission["id"], second["id"], "RUNNING")
        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail .pipeline-graph .workflow-step")
        page.wait_for_function("""() => {
          const cards = [...document.querySelectorAll('#detail .pipeline-graph .workflow-step')];
          const labels = cards.map(card => card.querySelector('.label')?.textContent.trim());
          return ['工作 A', '工作 B', '等待节点'].every(label => labels.includes(label))
            && !!document.querySelector('#workflow-edges path.running');
        }""")

        result = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('#detail .pipeline-graph .workflow-step')];
          const byLabel = label => cards.find(card => card.querySelector('.label').textContent.trim() === label);
          const active = byLabel('工作 A');
          const other = byLabel('工作 B');
          const waiting = byLabel('等待节点');
          return {
            activePercent: active.querySelector('.meta span:last-child').textContent.trim(),
            activeWidth: active.querySelector('.stage-progress span').style.width,
            activeMotion: getComputedStyle(active.querySelector('.node'), '::before').animationName,
            activeIndicatorMotion: getComputedStyle(active, '::before').animationName,
            edgeMotion: getComputedStyle(document.querySelector('#workflow-edges path.running')).animationName,
            waitingMotion: getComputedStyle(waiting.querySelector('.node'), '::before').animationName,
            parallelChips: cards.filter(card => card.querySelector('.parallel-chip')).length,
            overview: document.querySelector('#detail .pipeline-overview').textContent,
            prefersReduced: matchMedia('(prefers-reduced-motion: reduce)').matches
          };
        }""")
        assert result["activePercent"].startswith("未计量")
        assert result["activeWidth"] == "0%"
        assert result["activeMotion"] == "facai-orbit"
        assert result["activeIndicatorMotion"] == "facai-indicator-flow"
        assert result["edgeMotion"] == "facai-edge-flow"
        assert result["waitingMotion"] == "none"
        assert result["parallelChips"] == 2
        assert "2 路并行" in result["overview"]

        page.emulate_media(reduced_motion="reduce")
        reduced = page.evaluate("""() => ({
          node: getComputedStyle(document.querySelector('.stage.RUNNING .node'), '::before').animationName,
          indicator: getComputedStyle(document.querySelector('.stage.RUNNING'), '::before').animationName,
          edge: getComputedStyle(document.querySelector('#workflow-edges path.running')).animationName
        })""")
        assert reduced["node"] == "none"
        assert reduced["indicator"] == "none"
        assert reduced["edge"] == "none"
        browser.close()


def test_pipeline_status_percentages_and_latest_step_events_are_truthful(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        response = page.request.post(
            f"{console_server}/api/v1/missions",
            data=json.dumps({"title": "阶段状态验收", "original_input": "状态覆盖", "steps": [
                {"label": "失败前置"},
                {"label": "卡住节点", "depends_on": ["失败前置"]},
                {"label": "工作节点"},
                {"label": "等待节点"},
                {"label": "完成节点"},
            ]}), headers=_json_headers())
        assert response.ok, response.text
        mission = response.json()
        failed, blocked, running, waiting, completed = mission["steps"]
        _advance(page.request, console_server, mission["id"], failed["id"], "RUNNING")
        _advance(page.request, console_server, mission["id"], failed["id"], "FAILED")
        _advance(page.request, console_server, mission["id"], running["id"], "RUNNING")
        for line in ("较早的输出", "最新事件：正在处理输入"):
            event_response = page.request.post(
                f"{console_server}/api/v1/missions/{mission['id']}/events",
                data=json.dumps({"type": "OUTPUT", "step_id": running["id"],
                                 "payload": {"line": line}}), headers=_json_headers())
            assert event_response.ok, event_response.text
        _advance(page.request, console_server, mission["id"], completed["id"], "RUNNING")
        _advance(page.request, console_server, mission["id"], completed["id"], "COMPLETED")

        backend = page.request.get(f"{console_server}/api/v1/missions/{mission['id']}").json()
        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail .pipeline-graph .workflow-step")
        cards = page.eval_on_selector_all(
            "#detail .pipeline-graph .workflow-step",
            "els => els.map(el => ({label:el.querySelector('.label').textContent.trim(),"
            "status:el.classList.contains('BLOCKED')?'BLOCKED':el.classList.contains('FAILED')?'FAILED':"
            "el.classList.contains('RUNNING')?'RUNNING':el.classList.contains('COMPLETED')?'COMPLETED':'WAITING',"
            "statusText:el.querySelector('.pill').textContent.trim(),"
            "percent:el.querySelector('.meta span:last-child').textContent.trim().split(' · ')[0],"
            "width:el.querySelector('.stage-progress span').style.width,"
            "recent:el.querySelector('.stage-event')?.textContent.trim()||'',"
            "error:el.querySelector('.stage-error')?.textContent.trim()||'',aria:el.getAttribute('aria-label')}))")
        announcement = page.locator("#pipeline-announcement").inner_text()
        by_label = {card["label"]: card for card in cards}
        backend_by_label = {step["label"]: step for step in backend["steps"]}
        for label, card in by_label.items():
            actual = backend_by_label[label]
            percent = (f"{actual['progress_measured']:.2f}%"
                       if actual['progress_measured'] is not None else "未计量")
            assert card["percent"] == percent
            assert card["width"] == (f"{actual['progress_measured']:g}%"
                                     if actual['progress_measured'] is not None else "0%")
            assert f"进度 {percent}" in card["aria"]
        assert [(name, by_label[name]["statusText"]) for name in
                ("工作节点", "等待节点", "卡住节点", "失败前置")] == [
                    ("工作节点", "工作中"), ("等待节点", "等待中"),
                    ("卡住节点", "卡住"), ("失败前置", "失败")]
        assert by_label["卡住节点"]["status"] == "BLOCKED"
        assert "依赖阻塞" in by_label["卡住节点"]["recent"]
        assert "依赖 失败前置 未满足" in by_label["卡住节点"]["recent"]
        assert "正在处理输入" in by_label["工作节点"]["recent"]
        assert "较早的输出" not in by_label["工作节点"]["recent"]
        assert by_label["失败前置"]["error"] == "最近错误：阶段失败"
        assert by_label["等待节点"]["recent"] == ""
        assert page.locator("#pipeline-announcement").get_attribute("role") == "status"
        assert page.locator("#pipeline-announcement").get_attribute("aria-live") == "polite"
        assert "工作节点：工作中，进度 未计量" in announcement
        assert "卡住节点：卡住" in announcement and "依赖阻塞" in announcement
        assert "失败前置：失败" in announcement
        _advance(page.request, console_server, mission["id"], failed["id"], "RUNNING")
        page.reload(wait_until="load")
        page.wait_for_selector("#detail .pipeline-graph .workflow-step")
        recovered_announcement = page.locator("#pipeline-announcement").inner_text()
        assert "卡住节点：等待中" in recovered_announcement
        assert "最近事件：依赖解除" in recovered_announcement
        browser.close()


def test_billing_totals_use_reported_usage_and_configurable_multiplier(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        mission = _create_mission(page.request, console_server, "用量与费用", ["代码阶段", "模型阶段"])
        code_step, model_step = mission["steps"]
        events = [
            {"type": "OPENCODE_COMPLETED", "step_id": code_step["id"],
             "payload": {"cost": 0.002, "tokens": {"total": 120, "input_tokens": 80, "output_tokens": 40}}},
            {"type": "COST_RECORDED", "payload": {"amount": 0.002, "tokens": 120, "currency": "USD"}},
            {"type": "MODEL_COMPLETED", "step_id": model_step["id"],
             "payload": {"usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}},
            {"type": "COST_RECORDED", "payload": {"amount": 0.003, "tokens": 30, "currency": "USD"}},
        ]
        for event in events:
            response = page.request.post(
                f"{console_server}/api/v1/missions/{mission['id']}/events",
                data=json.dumps(event), headers=_json_headers())
            assert response.ok, response.text

        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail .billing-panel:not(.my-billing)")
        panel = page.locator("#detail .billing-panel:not(.my-billing)")
        assert panel.locator(".billing-metric").nth(0).inner_text().endswith("2")
        assert panel.locator(".billing-metric").nth(1).inner_text().endswith("100")
        assert panel.locator(".billing-metric").nth(2).inner_text().endswith("50")
        assert panel.locator(".billing-metric").nth(3).inner_text().endswith("150")
        assert "$0.005" in panel.locator(".billing-metric").nth(4).inner_text()
        assert "$0.05" in panel.locator(".billing-metric").nth(5).inner_text()  # default multiplier is now 10

        code_metrics = page.locator(f".workflow-step[data-step='{code_step['id']}'] .rail-metrics").inner_text()
        model_metrics = page.locator(f".workflow-step[data-step='{model_step['id']}'] .rail-metrics").inner_text()
        assert "调用 1 次 · Token 120" in code_metrics
        assert "$0.02" in code_metrics  # 10x default, money trimmed to 2-4 decimals
        assert "调用 1 次 · Token 30" in model_metrics
        assert "倍率估算 未报告" in model_metrics, "stage cost must not be guessed from mission-only ledger rows"

        # The multiplier control is hidden (operator pricing is preset at 10x);
        # changing it still works through the persisted localStorage setting.
        page.evaluate("localStorage.setItem('facai.billingMultiplier','2.5'); rerender()")
        assert "$0.0125" in panel.locator(".billing-metric").nth(5).inner_text()
        assert "$0.005" in page.locator(f".workflow-step[data-step='{code_step['id']}'] .rail-metrics").inner_text()
        assert page.evaluate("localStorage.getItem('facai.billingMultiplier')") == "2.5"
        browser.close()


def test_wechat_qr_authorization_opens_modal_and_shows_loading_and_qr(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        page.route("**/api/v1/channels/wechat/login", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"state": "WAITING_FOR_SCAN",
                             "qr_image": "data:image/png;base64,aGVsbG8="})))
        page.goto(console_server)
        page.locator("#channel-panel").evaluate("element => { element.open = true; }")
        page.locator("#wechat-login").click()

        dialog = page.locator("#wechat-dialog")
        dialog.wait_for(state="visible")
        # The QR arrives through an async fetch after the modal opens, so wait
        # for the loaded state instead of asserting before the response lands.
        page.wait_for_selector("#wqdlg-qr-wrap img", state="visible")
        assert page.locator("#wqdlg-status").inner_text() == "请用个人微信扫码"
        assert page.locator("#wqdlg-qr-wrap img").is_visible()
        page.locator("#wqdlg-close").click()
        dialog.wait_for(state="hidden")
        browser.close()


def test_workflow_cards_top_align_and_clamp_verbose_event_detail(console_server, playwright_api):
    """Regression: verbose latest-event detail used to wrap in a narrow column and
    stretch sibling WAITING cards to a huge blank height. Cards must top-align,
    keep the kind/time on one row, and clamp the detail to a full-width second row
    while retaining the full text in the accessible title. Uses a branchy DAG so
    the canvas layout (not the linear run rail) is what gets asserted."""
    base = console_server
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        response = page.request.post(
            f"{base}/api/v1/missions",
            data=json.dumps({"title": "卡片布局回归", "original_input": "dag", "steps": [
                {"label": "准备"},
                {"label": "支路 A", "depends_on": ["准备"]},
                {"label": "支路 B", "depends_on": ["准备"]},
                {"label": "汇合", "depends_on": ["支路 A", "支路 B"]},
            ]}), headers=_json_headers())
        assert response.ok, response.text
        mission = response.json()
        _root, branch_a, branch_b, _join = mission["steps"]
        _advance(page.request, base, mission["id"], _root["id"], "RUNNING")
        _advance(page.request, base, mission["id"], _root["id"], "COMPLETED")
        _advance(page.request, base, mission["id"], branch_a["id"], "RUNNING")
        long_line = "正在处理输入 " + "很长的日志内容 " * 60
        response = page.request.post(
            f"{base}/api/v1/missions/{mission['id']}/events",
            data=json.dumps({"type": "OUTPUT", "step_id": branch_a["id"],
                             "payload": {"line": long_line}}), headers=_json_headers())
        assert response.ok, response.text

        page.goto(f"{base}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail .pipeline-graph .workflow-step")
        layout = page.evaluate("""() => {
          const canvas = document.querySelector('#detail .pipeline-canvas');
          const cards = [...document.querySelectorAll('#detail .pipeline-graph .workflow-step')];
          const byLabel = label => cards.find(card => card.querySelector('.label').textContent.trim() === label);
          const running = byLabel('支路 A');
          const waiting = byLabel('支路 B');
          const detail = running.querySelector('.stage-event .event-detail');
          const detailStyle = getComputedStyle(detail);
          return {
            align: getComputedStyle(canvas).alignItems,
            runningTop: running.getBoundingClientRect().top,
            waitingTop: waiting.getBoundingClientRect().top,
            runningLeft: running.getBoundingClientRect().x,
            waitingLeft: waiting.getBoundingClientRect().x,
            runningHeight: running.getBoundingClientRect().height,
            waitingHeight: waiting.getBoundingClientRect().height,
            detailColumnStart: detailStyle.gridColumnStart,
            detailColumnEnd: detailStyle.gridColumnEnd,
            detailLines: detailStyle.webkitLineClamp,
            detailTitle: running.querySelector('.stage-event').getAttribute('title'),
          };
        }""")
        assert layout["align"] == "start", "workflow grid must top-align instead of stretching siblings"
        # Branch siblings share a column (branch lanes stack vertically), so the
        # regression to guard is "no cross-row stretching", not same-top alignment.
        assert abs(layout["runningLeft"] - layout["waitingLeft"]) <= 1, \
            "same-column branch cards must share a left edge"
        assert layout["waitingHeight"] < layout["runningHeight"] - 10, \
            "a WAITING sibling must not be stretched to the tall RUNNING card"
        assert layout["detailColumnStart"] == "1" and layout["detailColumnEnd"] == "-1", \
            "verbose event detail must occupy a full-width second row"
        assert layout["detailLines"] == "3", "verbose event detail should be clamped to a few lines"
        assert "很长的日志内容" in (layout["detailTitle"] or ""), \
            "full event text must remain accessible in the title"
        browser.close()


def test_deep_link_mission_is_restored_after_login(protected_server, playwright_api):
    base = protected_server
    basic = base64.b64encode(b"admin:mobile-pass").decode()
    auth = {"Authorization": f"Basic {basic}"}
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        mission = _create_mission(page.request, base, "登录后恢复", ["规划", "开发"], headers=auth)

        page.goto(f"{base}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#login-form")
        assert "/login" in page.url and "next=" in page.url

        page.fill("#password", "mobile-pass")
        page.click("#submit")
        page.wait_for_selector("#detail [role=progressbar]", timeout=10000)

        assert f"mission={mission['id']}" in page.url, "deep link must survive login"
        assert mission["title"] in page.inner_text("#detail h2")
        browser.close()


TG_TOKEN = "111222333:AAAliceBotToken_abc123XYZ"


def test_my_channels_panel_binds_and_unbinds_through_the_real_backend(
        console_server, playwright_api, monkeypatch):
    """客户持久渠道 MVP 的控制台 UI：Telegram/LINE 两行在同一面板里完成
    绑定→状态回显（bound/chat_id/created_at）→解绑，全程打真实后端
    /my/channels/*（本地校验+落库，不触网），并以 console-error 断言收尾。"""
    # 本用例的契约是「不触网」：绑定端点落库前的真实凭证预检由 API 层测试
    # 覆盖（tests/test_credential_preflight.py），这里按开关语义整体跳过，
    # 服务线程与用例同进程、逐请求读环境变量，立即生效。
    monkeypatch.setenv("FACAI_SKIP_CREDENTIAL_PREFLIGHT", "1")
    base = console_server
    errors = []
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(base, wait_until="load")
        # 面板默认折叠：先等 JS 落位（attached），展开后再做可见性断言
        page.wait_for_selector('[data-channel-bound="telegram"]', state="attached")
        page.locator("#my-channels-panel").evaluate("element => { element.open = true; }")
        page.wait_for_selector('[data-channel-bound="telegram"]')
        # 初始 HTML 的 pill 是「加载中…」占位，真实状态要等 loadMyChannels
        # 异步拉 /my/channels 后才写入：必须等文本落位再读，否则全量跑
        # 高负载时 fetch 未返回就读文本，必踩「加载中… ≠ 未绑定」竞态。
        page.wait_for_selector('[data-channel-bound="telegram"]:has-text("未绑定")')
        assert page.inner_text('[data-channel-bound="telegram"]') == "未绑定"
        page.wait_for_selector('[data-channel-bound="line"]:has-text("未绑定")')
        assert page.inner_text('[data-channel-bound="line"]') == "未绑定"
        assert page.locator('[data-unbind-channel="telegram"]').is_hidden()

        # token 框必须是密码框：凭据在屏幕上不以明文出现
        assert page.get_attribute("#tg-bot-token", "type") == "password"
        assert page.get_attribute("#line-access-token", "type") == "password"
        assert page.get_attribute("#line-channel-secret", "type") == "password"

        # 先走一遍 422：非法 token 被后端拒绝，错误原样透出且状态保持未绑定
        page.fill("#tg-bot-token", "abc:def")
        page.fill("#tg-chat-id", "556677")
        page.click("#my-channel-telegram-form button[type=submit]")
        page.wait_for_selector('#my-channel-telegram-state:has-text("绑定失败")')
        assert page.inner_text('[data-channel-bound="telegram"]') == "未绑定"

        # 合法绑定：pill 翻转为已绑定，meta 行回显接收 ID 与绑定时间
        page.fill("#tg-bot-token", TG_TOKEN)
        page.click("#my-channel-telegram-form button[type=submit]")
        page.wait_for_selector('[data-channel-bound="telegram"]:has-text("已绑定")')
        meta = page.inner_text('[data-channel-meta="telegram"]')
        assert "556677" in meta and "绑定于" in meta
        assert page.locator('[data-unbind-channel="telegram"]').is_visible()
        listing = page.request.get(f"{base}/api/v1/my/channels").json()
        assert listing["telegram"]["bound"] is True and listing["telegram"]["chat_id"] == "556677"

        # LINE 同路径：三凭据一次提交，同一回显口径
        page.fill("#line-access-token", "eyJhbGciOiJIUzLineAccessTokenValuesHere")
        page.fill("#line-channel-secret", "0123456789abcdef0123456789abcdef")
        page.fill("#line-user-id", "U4af9989d1241dd7f6f8f6d2f8b1e9c0a")
        page.click("#my-channel-line-form button[type=submit]")
        page.wait_for_selector('[data-channel-bound="line"]:has-text("已绑定")')
        assert "U4af9989d1241dd7f6f8f6d2f8b1e9c0a" in page.inner_text('[data-channel-meta="line"]')

        # 解绑回落：pill 回到未绑定，解绑按钮重新隐藏，后端如实 bound=False
        page.click('[data-unbind-channel="telegram"]')
        page.wait_for_selector('[data-channel-bound="telegram"]:has-text("未绑定")')
        assert page.locator('[data-unbind-channel="telegram"]').is_hidden()
        listing = page.request.get(f"{base}/api/v1/my/channels").json()
        assert listing["telegram"]["bound"] is False
        assert listing["line"]["bound"] is True  # LINE 绑定不受 Telegram 解绑影响

        browser.close()
    # 绑定失败展示（服务端 422 原样透出）是本用例故意探的一次：Chromium 会把
    # 任何 4xx fetch 记成 console error（"Failed to load resource: ... 422"），
    # 这是预期内的资源日志而非页面缺陷，精确过滤后其余 console error 仍须为零。
    unexpected = [e for e in errors if "422" not in e]
    assert not unexpected, f"console errors: {unexpected}"


def test_model_matrix_renders_real_stats_columns_and_watermark_note():
    """MODEL-001 ④：模型矩阵面板必须展示真实统计列（延迟/错误率/调用次数，
    全部来自 /api/v1/modelmatrix 快照字段）、提供走真实 API 的水位线编辑入口，
    并对代码内置的冷却常量做诚实只读说明——不做假编辑框、不放演示数据。"""
    html = (Path(__file__).resolve().parents[1] / "facai" / "static" /
            "command-center.html").read_text(encoding="utf-8")
    # 统计列：字段名必须钉在 matrix 快照的真实字段上
    for field in ("latency_recent_avg_ms", "latency_avg_ms", "error_rate",
                  "reserved_count", "reported_count", "ok_count", "fail_count"):
        assert field in html, f"missing real matrix field: {field}"
    for label in ("延迟", "错误率", "调用"):
        assert label in html
    # 无样本诚实显示 —，不编造
    assert 'latency == null ? "—"' in html
    # 水位线编辑：入口按钮 + 真实端点（写回注册表的 PUT limits）
    assert 'data-act="limits"' in html
    assert "水位线" in html
    assert "/api/v1/modelmatrix/${m}/limits" in html
    assert "rate_limit_per_min" in html and "max_concurrency" in html
    # 内置常量的诚实只读说明（值来自 matrix API constants，非前端硬编码）
    assert "代码内置常量" in html
    assert "matrix.constants" in html
    assert "cooldown_base_s" in html and "cooldown_max_s" in html


def test_skill_usage_drawer_reads_owner_scoped_results(console_server, playwright_api):
    """The skill card fetches observed usage; empty history stays explicitly zero."""
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        registered = page.request.put(
            console_server + "/api/v1/registries/skills/code-review",
            data=json.dumps({"name": "code-review", "description": "Review code",
                             "version": "1.2.0", "source_url": "https://github.com/example/review",
                             "license": "MIT", "review_status": "approved"}),
            headers={"Content-Type": "application/json"})
        assert registered.ok, registered.text
        # Keep boot() waiting on session while the page is already visible.
        # A click during that window must open the drawer, not be discarded.
        page.add_init_script("""
          const facaiFetch = window.fetch.bind(window);
          window.fetch = (...args) => String(args[0]).includes('/api/v1/auth/session')
            ? new Promise(resolve => setTimeout(() => resolve(facaiFetch(...args)), 1500))
            : facaiFetch(...args);
        """)
        page.goto(console_server + "/command-center", wait_until="load")
        # Slow CI runners: the registries fetch + drawer render can outlast the
        # 30s default; patience only, assertions untouched.
        page.set_default_timeout(60000)
        page.locator("#reg").click()
        page.locator('button[data-skill="code-review"]').click()
        usage = page.locator('[data-usage-for="code-review"]')
        usage.get_by_text("归因终态 0", exact=False).wait_for()
        assert "正文交给运行时 0" in usage.inner_text()
        assert "仅元数据归因 0" in usage.inner_text()
        assert "已存正文版本 无正文版本" in usage.inner_text()
        assert "运行完成不代表事实已验证" in usage.inner_text()
        browser.close()


def test_skill_source_revision_can_be_reviewed_without_executing_it(console_server, playwright_api):
    """Owner clicks an exact persisted revision; the body is rendered as text."""
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        content = ("---\nname: audit-skill\ndescription: Review a repository\n"
                   "version: 1.0.0\nlicense: MIT\n"
                   "source_url: https://example.com/audit-skill\n---\n"
                   "Inspect files only. <script>window.skillInjected=true</script>\n")
        registered = page.request.post(
            console_server + "/api/v1/skills/import",
            data=json.dumps({"content": content}),
            headers={"Content-Type": "application/json"})
        assert registered.ok, registered.text
        page.goto(console_server + "/command-center", wait_until="load")
        page.locator("#reg").click()
        page.locator('button[data-skill="audit-skill"]').click()
        page.get_by_role("button", name="查看 v1.0.0 正文", exact=False).click()
        source = page.locator(".skill-source")
        source.get_by_text("Inspect files only", exact=False).wait_for()
        assert "Inspect files only" in source.inner_text()
        assert source.locator("script").count() == 0
        assert page.evaluate("window.skillInjected === true") is False
        def accept_restore(dialog):
            dialog.accept("2.0.0" if dialog.type == "prompt" else None)
        page.on("dialog", accept_restore)
        page.get_by_role("button", name="以新版本发布 v1.0.0 正文").click()
        page.wait_for_function("""() => document.getElementById('regdrawer').innerText.includes('v2.0.0')""")
        metadata = page.request.get(console_server + "/api/v1/registries/skills").json()["audit-skill"]
        assert metadata["version"] == "2.0.0"
        latest = page.request.get(console_server + "/api/v1/registries/skills/audit-skill/source",
                                  params={"version": "2.0.0", "body_sha256": metadata["body_sha256"]})
        assert latest.ok and "Inspect files only" in latest.json()["body"]
        browser.close()


def test_mobile_me_tab_keeps_channel_binding_form_an_honest_static_demo():
    """mobile.html「我的」区绑定入口：与总控台同名的表单必须存在、凭据框
    type=password、真实 API 以注释位标注；整个页面保持零 fetch 的静态演示
    口径（基线 §33，不制造执行感）。"""
    html = (Path(__file__).resolve().parents[1] / "facai" / "static" / "mobile.html").read_text(
        encoding="utf-8")
    assert 'id="mobile-channel-binding-form"' in html
    assert "我的通知渠道" in html
    # 真实 API 调用注释位：五条 my/channels 端点全部在注释里标注
    for endpoint in ("PUT    /api/v1/my/channels/telegram",
                     "PUT    /api/v1/my/channels/line",
                     "GET    /api/v1/my/channels",
                     "DELETE /api/v1/my/channels/telegram",
                     "DELETE /api/v1/my/channels/line"):
        assert endpoint in html, f"missing API comment marker: {endpoint}"
    assert html.count('type="password"') >= 3  # tg token + line token/secret
    # 移动端已真实接线（HANDOFF §二十二 起废止静态演示口径）：请求统一走
    # apiRequest 层（登录/下单/进度/绑定），真实端点字面量由 test_mobile_surface 钉住。
    assert "apiRequest(" in html



def test_command_center_pins_real_metering_fields_and_honest_empty_states():
    """MODEL-001 每Key计量 + A10 车队计量：抽屉面板字段必须钉在真实 API 快照
    字段上（key-usage / fleet agent-progress），空态诚实（无分母不显示数），
    口径文案写明计数来源；成本无单价显示 —，不发明估价。"""
    html = (Path(__file__).resolve().parents[1] / "facai" / "static" /
            "command-center.html").read_text(encoding="utf-8")
    assert '"/api/v1/modelmatrix/key-usage"' in html
    assert '"/api/v1/fleet/agent-progress"' in html
    for field in ("calls", "completed", "rate_limited", "auth_failed",
                  "input_tokens", "output_tokens", "estimated_cost",
                  "assigned", "failed", "running", "progress_measured"):
        assert field in html, f"missing metering field: {field}"
    assert "进程内计数，重启清零" in html
    assert "tokens 为上游自报" in html
    assert "completed_jobs_of_attributed_jobs" in html
    assert "没有分母的 Agent 不显示" in html
    assert "没有每Key分母就不显示数" in html
    assert 'estimated_cost == null ? "—"' in html


def test_command_center_drawer_renders_metering_blocks(console_server, playwright_api):
    """抽屉实开：车队计量与每Key计量两块真实渲染；受控环境无归因任务时
    空态口径文案可见，绝不编造进度或每Key计数。"""
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(console_server + "/command-center", wait_until="load")
        # Slow CI runners: drawer fetches several APIs before render.
        page.set_default_timeout(60000)
        page.locator("#reg").click()
        # 77055f0 教训：wait_for_function 默认 RAF 轮询会被慢 runner 饿死，
        # 新增等待一律固定间隔轮询。
        page.wait_for_function(
            "() => document.getElementById('regdrawer').innerText.includes('车队计量')",
            polling=100)
        text = page.locator("#regdrawer").inner_text()
        assert "每Key计量" in text
        assert "没有分母的 Agent 不显示" in text
        assert "没有每Key分母就不显示数" in text
        assert "进程内计数" in text
        assert "completed_jobs_of_attributed_jobs" in text
        browser.close()
