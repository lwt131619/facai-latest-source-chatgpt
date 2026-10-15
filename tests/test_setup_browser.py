"""First-install browser flow on a real loopback uvicorn server."""
import json
import os
import socket
import threading
import time
import urllib.request

import pytest

from facai.api import create_app
from facai.onboarding import write_private


@pytest.fixture
def fresh_server(tmp_path, monkeypatch):
    monkeypatch.delenv('FACAI_USERS_JSON', raising=False)
    monkeypatch.setenv('FACAI_ADMIN_USER', 'admin')
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'initial-owner-fixture-5932')
    monkeypatch.setenv('FACAI_BOOTSTRAP_TOKEN', 'local-bootstrap-fixture-token')
    monkeypatch.setenv('FACAI_LOCAL_OWNER_FILE', str(tmp_path/'local-owner.json'))
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_CHANNEL_NOTIFY_ENABLED', '0')
    write_private(tmp_path/'local-owner.json', {'version':2,'password':'initial-owner-fixture-5932','setup_complete':False})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path),host='127.0.0.1',port=port,log_level='warning'))
    thread = threading.Thread(target=server.run,daemon=True); thread.start()
    base='http://127.0.0.1:'+str(port)
    for _ in range(100):
        try:
            urllib.request.urlopen(base+'/health',timeout=.5).close(); break
        except OSError: time.sleep(.05)
    else:
        raise AssertionError('Local server did not start')
    yield base, tmp_path
    server.should_exit=True; thread.join(timeout=5)


@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
def test_first_run_owner_login_diagnostics_and_reload(fresh_server,playwright_api,width,height):
    base,root=fresh_server
    with playwright_api.sync_playwright() as p:
        executable=os.getenv('FACAI_TEST_CHROMIUM')
        try:
            browser=p.chromium.launch(**({'executable_path':executable} if executable else {}),args=['--no-sandbox'])
        except Exception as exc:
            pytest.skip('Chromium unavailable: '+str(exc))
        page=browser.new_page(viewport={'width':width,'height':height})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/setup#bootstrap=local-bootstrap-fixture-token')
        page.wait_for_selector('#owner-card:visible')
        assert '#' not in page.url
        page.fill('#username','owner')
        page.fill('#password','Grove-orbit-5932!')
        page.click('#create-owner')
        page.wait_for_selector('#recovery-result:visible')
        recovery=page.locator('#recovery-output').input_value()
        assert len(recovery)>=40 and page.locator('#recovery-output').get_attribute('type')=='password'
        persisted=json.loads((root/'local-owner.json').read_text())
        assert persisted['setup_complete'] and 'password' not in persisted
        assert recovery not in (root/'local-owner.json').read_text()
        page.locator('#recovery-result a').click()
        page.fill('#username','owner');page.fill('#password','Grove-orbit-5932!')
        page.click('#login-form button[type=submit]')
        page.wait_for_url('**/setup')
        page.wait_for_selector('#configured:visible')
        page.wait_for_function("document.querySelector('#checks').textContent.includes('database')")
        assert 'MODEL_NOT_VERIFIED' in page.locator('#blocking').inner_text()
        page.select_option('#provider','ollama')
        page.fill('#name','Browser local fixture');page.fill('#model','fixture')
        assert page.locator('#key').is_disabled()
        page.click('#model-form button')
        page.wait_for_function("document.querySelector('#models').textContent.includes('Browser local fixture')")
        assert 'NOT_TESTED' in page.locator('#models').inner_text()
        # Never click probe: this test must not call a real model or Ollama.
        page.check('#acknowledge');page.click('#finish')
        page.wait_for_function("document.querySelector('#blocking').textContent.includes('设置状态已保存')")
        page.reload();page.wait_for_selector('#configured:visible')
        page.wait_for_function("document.querySelector('#models').textContent.includes('Browser local fixture')")
        assert page.locator('#owner-card').is_hidden()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert errors==[]
        browser.close()
