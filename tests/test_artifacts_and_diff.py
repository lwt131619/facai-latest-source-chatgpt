"""Artifact content inspection and Git diff are mission-scoped and safe."""

import subprocess

from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter


class FakeRunner:
    def __init__(self, stdout="{\"type\":\"text\",\"text\":\"done\"}\n", returncode=0):
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, command, cwd, timeout):
        (cwd / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "")


def setup(tmp_path):
    client = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary='opencode', runner=FakeRunner())))
    a = client.post('/api/v1/missions', json={
        'title': 'A', 'original_input': 'A', 'steps': ['开发']}).json()
    b = client.post('/api/v1/missions', json={
        'title': 'B', 'original_input': 'B', 'steps': ['开发']}).json()
    for mission in (a, b):
        client.put(f"/api/v1/missions/{mission['id']}/binding",
                   json={'agent_id': 'opencode', 'model_id': 'deepseek-flash'})
    client.post(f"/api/v1/missions/{a['id']}/opencode/run", json={'prompt': 'write app.py'})
    return client, a, b


def test_artifact_content_is_readable_and_mission_scoped(tmp_path):
    client, a, b = setup(tmp_path)
    artifacts = client.get(f"/api/v1/missions/{a['id']}").json()['artifacts']
    assert len(artifacts) == 1
    artifact_id = artifacts[0]['id']

    body = client.get(f"/api/v1/missions/{a['id']}/artifacts/{artifact_id}").json()
    assert 'path' not in body
    assert body['name'] == artifacts[0]['name']
    assert 'done' in body['content'] and body['truncated'] is False

    # Another mission cannot read this artifact, and unknown ids are 404.
    assert client.get(f"/api/v1/missions/{b['id']}/artifacts/{artifact_id}").status_code == 404
    assert client.get(f"/api/v1/missions/{a['id']}/artifacts/unknown").status_code == 404


def test_mission_diff_shows_code_changes(tmp_path):
    client, a, b = setup(tmp_path)
    diff = client.get(f"/api/v1/missions/{a['id']}/diff").json()
    assert diff['available'] is True and diff['checkpoint']
    assert 'app.py' in diff['diff']
    # A mission without checkpoints reports honestly that no diff exists.
    empty = client.get(f"/api/v1/missions/{b['id']}/diff").json()
    assert empty['available'] is False and empty['diff'] == ''
