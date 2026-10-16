"""Facai visual-identity and first-screen regression checks.

These run in a real Chromium against the live FastAPI app. They lock in the
product's visual contract (anchor brand mark, deep-sea grid/ripple decoration,
glass panels and cyan progress) without replacing the deeper behavioural tests
in ``test_console_layout.py`` and ``test_mobile_ui.py``. They are skipped when
Playwright or its browser is unavailable so the core suite stays runnable.
"""

import json
import os

import pytest


def _launch(playwright):
    try:
        return playwright.chromium.launch(args=['--no-sandbox'], **({'executable_path': os.environ['FACAI_TEST_CHROMIUM']} if os.getenv('FACAI_TEST_CHROMIUM') else {}))
    except Exception as exc:  # browser binary not installed
        pytest.skip(f"Playwright chromium unavailable: {exc}")


def _create_mission(request, base, title="视觉核验", steps=("规划", "开发", "测试")):
    response = request.post(
        base + "/api/v1/missions",
        data=json.dumps({"title": title, "original_input": f"原始要求：{title}",
                         "steps": list(steps)}),
        headers={"Content-Type": "application/json"})
    assert response.ok, response.text
    return response.json()


def _advance(request, base, mission_id, step_id, status):
    response = request.patch(
        f"{base}/api/v1/missions/{mission_id}/steps/{step_id}",
        data=json.dumps({"status": status}),
        headers={"Content-Type": "application/json"})
    assert response.ok, response.text


def test_brand_mark_and_decorative_layers_are_present(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        mission = _create_mission(page.request, console_server)

        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail [role=progressbar]")

        # The Facai anchor brand mark is an accessible, labelled SVG.
        mark = page.query_selector(".brand-mark svg")
        assert mark is not None, "anchor brand mark missing"
        assert page.get_attribute(".brand-mark", "aria-label") == "Facai 船锚标识"

        # Grid/ripple decoration is purely visual and never intercepts input.
        decor = page.eval_on_selector(
            ".bg-decor",
            "el => ({hidden: el.getAttribute('aria-hidden'),"
            " grid: !!el.querySelector('.grid'), ripple: !!el.querySelector('.ripple'),"
            " pointer: getComputedStyle(el).pointerEvents,"
            " overflow: getComputedStyle(el).overflow})")
        assert decor["hidden"] == "true" and decor["grid"] and decor["ripple"]
        assert decor["pointer"] == "none" and decor["overflow"] == "hidden"
        browser.close()


def test_first_screen_progress_survives_the_theme(console_server, playwright_api):
    base = console_server
    errors = []
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        mission = _create_mission(page.request, base, "首屏进度核验")
        first, second = mission["steps"][0]["id"], mission["steps"][1]["id"]
        _advance(page.request, base, mission["id"], first, "RUNNING")
        _advance(page.request, base, mission["id"], first, "COMPLETED")
        _advance(page.request, base, mission["id"], second, "RUNNING")

        page.goto(f"{base}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail [role=progressbar]")

        # Progress is still derived from server data (1/3), visible without scrolling.
        assert page.get_attribute("#detail [role=progressbar]", "aria-valuenow") == "33"
        box = page.eval_on_selector(
            "#detail [role=progressbar]",
            "el => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; }")
        assert 0 <= box["top"] and box["bottom"] <= 900, f"progress bar below the fold: {box}"

        # The progress fill uses the cyan-blue brand gradient, not a flat colour.
        fill = page.eval_on_selector(
            "#detail [role=progressbar] span", "el => getComputedStyle(el).backgroundImage")
        assert "gradient" in fill, f"progress fill is not a gradient: {fill}"

        # The running stage shows its persisted 1% value as a static fill.
        running = page.query_selector("#detail .pipeline .stage.RUNNING")
        assert running is not None
        assert "进度 1%" in running.get_attribute("aria-label")
        assert running.query_selector(".stage-progress span").evaluate("el => el.style.width") == "1%"
        assert page.query_selector("#detail .stage-activity") is None, \
            "stage progress must come from backend state, without a fake activity sweep"
        assert "开发" in page.inner_text("#detail #current-stage")

        # Glass panels actually apply a backdrop blur on the console.
        backdrop = page.eval_on_selector(
            "main .panel", "el => getComputedStyle(el).backdropFilter || getComputedStyle(el).webkitBackdropFilter")
        assert "blur" in backdrop, f"panels are not glassy: {backdrop}"

        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"desktop horizontal overflow of {overflow}px"
        browser.close()
    assert not errors, f"console errors: {errors}"


def test_mobile_theme_has_no_overflow_and_keeps_progress(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                is_mobile=True, has_touch=True)
        mission = _create_mission(page.request, console_server, "移动视觉核验",
                                  ("规划", "开发", "测试"))
        first = mission["steps"][0]["id"]
        _advance(page.request, console_server, mission["id"], first, "RUNNING")
        _advance(page.request, console_server, mission["id"], first, "COMPLETED")

        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        page.wait_for_selector("#detail [role=progressbar]")

        # The decorative layers must never introduce horizontal scroll on a phone.
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"mobile horizontal overflow of {overflow}px"

        progress = page.eval_on_selector(
            "#detail [role=progressbar]",
            "el => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; }")
        assert 0 <= progress["top"] and progress["bottom"] <= 844, \
            f"progress bar not on the first phone screen: {progress}"

        # Stage cards stay tappable and stack vertically.
        widths = page.eval_on_selector_all(
            "#detail .pipeline .stage", "els => els.map(e => e.getBoundingClientRect().width)")
        assert widths and max(widths) - min(widths) < 5
        heights = page.eval_on_selector_all(
            "#detail .pipeline .stage", "els => els.map(e => e.getBoundingClientRect().height)")
        assert min(heights) >= 44, f"stage tap targets too small: {heights}"
        assert page.query_selector(".brand-mark svg") is not None
        browser.close()


def test_login_page_carries_the_same_brand(protected_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True,
                                has_touch=True)
        page.goto(protected_server + "/", wait_until="load")
        page.wait_for_selector("#login-form")

        assert page.query_selector(".brand-mark svg") is not None
        assert page.query_selector(".bg-decor .ripple") is not None
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"login horizontal overflow of {overflow}px"
        browser.close()
