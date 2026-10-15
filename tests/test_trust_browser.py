"""Real-browser Trust workflow. Uses local deterministic data, not model calls."""
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

import pytest


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_intent_to_evidence_review_and_download(protected_server, playwright_api, tmp_path, width, height):
    with playwright_api.sync_playwright() as p:
        executable = os.environ.get('FACAI_TEST_CHROMIUM')
        try:
            browser = p.chromium.launch(**({'executable_path': executable} if executable else {}),
                                        args=['--no-sandbox'])
        except Exception as exc:
            pytest.skip('Chromium unavailable: ' + str(exc))
        page = browser.new_page(viewport={'width': width, 'height': height}, accept_downloads=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(protected_server+'/login?next=%2Fworkspace')
        page.fill('input[name="username"]', 'admin')
        page.fill('input[name="password"]', 'mobile-pass')
        page.click('button[type="submit"]')
        page.wait_for_url('**/workspace')
        page.fill('#request', '分析这个项目')
        page.click('#compile button')
        page.wait_for_function("document.querySelector('#state').textContent.includes('NEED_CONTEXT')")
        page.fill('#action', 'analyze'); page.fill('#target', '/owner/project')
        page.click('#save')
        page.wait_for_function("document.querySelector('#revision').textContent.includes('Revision 2')")
        page.click('#confirm')
        page.wait_for_selector('#mission:not(.hidden)')
        mission_id = page.locator('#open-mission').get_attribute('href').split('mission=')[1]
        page.goto(protected_server+'/evidence-center?mission='+mission_id)
        page.wait_for_selector('#workspace:visible')
        page.locator('details > summary').first.click()
        assert '分析这个项目' in page.locator('#spec').inner_text()
        page.fill('#source', 'Local Owner observation, not external attestation')
        page.fill('#content', 'Local version output says 1')
        page.click('#note button')
        page.wait_for_function("document.querySelector('#revision').textContent.includes('Revision 1')")
        evidence_id = page.locator('#links option').first.get_attribute('value')
        page.fill('#statement', '<script>untrusted claim</script> version is 1')
        page.fill('#subject', 'project'); page.fill('#predicate', 'version'); page.fill('#value', '1')
        page.select_option('#links', evidence_id)
        page.click('#claim button')
        page.wait_for_function("document.querySelector('#revision').textContent.includes('Revision 2')")
        assert 'UNKNOWN' in page.locator('#claims').inner_text()
        assert page.locator('#claims script').count() == 0
        page.locator('#claims select').select_option('INFERENCE')
        page.locator('#claims input').fill('Explicit Owner review of local note; independently unverified')
        page.locator('#claims button').click()
        page.wait_for_function("document.querySelector('#revision').textContent.includes('Revision 3')")
        assert 'INFERENCE' in page.locator('#claims').inner_text()
        page.click('#snapshot')
        page.wait_for_selector('#proofs a.download')
        assert page.locator('#proofs button').is_disabled()  # No execution; cannot accept.
        with page.expect_download() as download_info:
            page.locator('#proofs a.download').first.click()
        download = download_info.value
        dest = tmp_path/'proof.zip'; download.save_as(dest)
        with zipfile.ZipFile(io.BytesIO(dest.read_bytes())) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            for name, sha in manifest['files'].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == sha
            assert json.loads(archive.read('claims.json'))[0]['status'] == 'INFERENCE'
        page.reload()
        page.wait_for_selector('#proofs a.download')
        assert page.locator('#claims').inner_text().startswith('INFERENCE')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert errors == []
        browser.close()
