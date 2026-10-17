"""Mission-scoped OpenCode execution: isolation, capture and honest status."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter


class FakeRunner:
    """Deterministic stand-in for the OpenCode CLI; never calls a model."""

    def __init__(self, returncode=0, stdout="", stderr="", write_text=None, timeout=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.write_text = write_text
        self.timeout = timeout
        self.calls = []

    def __call__(self, command, cwd, timeout):
        self.calls.append({"command": list(command), "cwd": str(cwd), "timeout": timeout})
        if self.timeout:
            raise subprocess.TimeoutExpired(command, timeout)
        if self.write_text is not None:
            (cwd / "run.txt").write_text(self.write_text.format(cwd=cwd), encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


class ResultAdapter:
    """Adapter that returns an exact result build(mission_id, root) so result validation is exercised."""

    def __init__(self, build):
        self.build = build
        self.calls = []

    def execute(self, mission_id, root, prompt, model_ref=None, timeout=300, on_output=None):
        self.calls.append(mission_id)
        return self.build(mission_id, root)


def make_client(tmp_path, adapter):
    return TestClient(create_app(tmp_path, opencode_adapter=adapter))


def create(client, title):
    return client.post('/api/v1/missions', json={
        'title': title, 'original_input': f'要求：{title}', 'steps': ['开发', '测试']}).json()


def bind(client, mission, model='deepseek-flash'):
    return client.put(f"/api/v1/missions/{mission['id']}/binding",
                      json={'agent_id': 'opencode', 'model_id': model})


def test_execute_isolates_two_workspaces_and_captures_output(tmp_path):
    runner = FakeRunner(stdout='line one\nline two\n', stderr='warn\n')
    adapter = OpenCodeAdapter(binary='opencode', runner=runner)

    first = adapter.execute('mission-a', tmp_path, 'task A')
    second = adapter.execute('mission-b', tmp_path, 'task B')

    assert first['status'] == 'OPENCODE_COMPLETED'
    assert first['stdout'] == 'line one\nline two\n'
    assert first['stderr'] == 'warn\n'
    assert 'mission-a' in first['workspace'] and 'mission-b' in second['workspace']
    assert first['workspace'] != second['workspace']
    # Each run happened in its own workspace directory.
    assert first['workspace'] in runner.calls[0]['cwd']
    assert second['workspace'] in runner.calls[1]['cwd']
    assert runner.calls[0]['cwd'] != runner.calls[1]['cwd']
    # No overlap: a file written in one workspace is absent from the other.
    (tmp_path / 'workspaces' / 'mission-a' / 'only-a.txt').write_text('a', encoding='utf-8')
    assert not (tmp_path / 'workspaces' / 'mission-b' / 'only-a.txt').exists()


def test_execute_attributes_failure_and_timeout(tmp_path):
    adapter = OpenCodeAdapter(binary='opencode', runner=FakeRunner(returncode=2, stderr='boom'))
    failed = adapter.execute('m', tmp_path, 'task')
    assert failed['status'] == 'OPENCODE_FAILED' and failed['exit_code'] == 2

    timing_out = OpenCodeAdapter(binary='opencode', runner=FakeRunner(timeout=True))
    result = timing_out.execute('m', tmp_path, 'task', timeout=5)
    assert result['status'] == 'OPENCODE_TIMEOUT' and result['exit_code'] is None


def test_default_runner_streams_both_pipes_as_lines(tmp_path):
    seen = []
    script = "import sys; print('first'); print('api_key=secret-value', file=sys.stderr)"
    completed = OpenCodeAdapter._run([sys.executable, '-u', '-c', script], tmp_path, 5,
                                     on_output=lambda stream, line: seen.append((stream, line)))
    assert completed.returncode == 0
    assert ('stdout', 'first') in seen
    assert ('stderr', 'api_key=[REDACTED]') in seen
    assert 'secret-value' in completed.stderr  # execute() redacts before persistence/response


def test_command_carries_bound_model_without_secrets(tmp_path):
    adapter = OpenCodeAdapter(binary='opencode', runner=FakeRunner())
    command = adapter.command('do work', tmp_path, 'deepseek/deepseek-flash')
    assert command[:4] == ['opencode', 'run', '--format', 'json']
    assert '--model' in command and 'deepseek/deepseek-flash' in command
    assert '--dir' in command
    assert command[-1] == 'do work'


def test_run_endpoint_records_mission_scoped_events_and_artifacts(tmp_path):
    runner = FakeRunner(stdout='{"type":"text","text":"完成了"}\n', stderr='')
    client = make_client(tmp_path, OpenCodeAdapter(binary='opencode', runner=runner))
    a, b = create(client, 'A'), create(client, 'B')
    bind(client, a)
    bind(client, b)

    response = client.post(f"/api/v1/missions/{a['id']}/opencode/run",
                           json={'prompt': '写一个 hello.py', 'step_id': a['steps'][0]['id']})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'OPENCODE_COMPLETED'
    assert body['model_id'] == 'deepseek-flash'
    assert len(body['artifacts']) == 1 and body['artifacts'][0].endswith('.stdout.log')
    assert '完成了' in body['stdout']

    detail = client.get(f"/api/v1/missions/{a['id']}").json()
    assert detail['status'] == 'RUNNING'  # one of two steps complete, not a whole mission success
    assert detail['steps'][0]['status'] == 'COMPLETED'
    assert len(detail['artifacts']) == 1
    assert [s['status'] for s in detail['sessions']] == ['COMPLETED']

    events = client.get(f"/api/v1/missions/{a['id']}/events").json()
    types = [event['type'] for event in events]
    assert 'OPENCODE_STARTED' in types and 'OPENCODE_COMPLETED' in types and 'OUTPUT' in types
    assert types.index('OPENCODE_STARTED') < types.index('OUTPUT') < types.index('OPENCODE_COMPLETED')
    assert all(event['mission_id'] == a['id'] for event in events)

    # The second mission is untouched: no session, no artifact, no captured output.
    other = client.get(f"/api/v1/missions/{b['id']}").json()
    assert other['sessions'] == [] and other['artifacts'] == []
    other_events = client.get(f"/api/v1/missions/{b['id']}/events").json()
    assert not any(event['type'].startswith('OPENCODE') or event['type'] == 'OUTPUT'
                   for event in other_events)


def test_bound_skill_body_reaches_runtime_and_terminal_trace(tmp_path):
    runner = FakeRunner(stdout='{"type":"text","text":"review done"}\n')
    client = make_client(tmp_path, OpenCodeAdapter(binary='opencode', runner=runner))
    source = 'Review the diff against acceptance criteria.'
    digest = client.app.state.store.skill_source_register('code-review', '1.2.0', source)
    client.app.state.registries.register('skills', 'code-review', {
        'name': 'code-review', 'description': 'Review code', 'license': 'MIT',
        'source_url': 'https://example.com/code-review', 'version': '1.2.0',
        'body_sha256': digest,
    })
    mission = create(client, 'Review project')
    binding = client.put(f"/api/v1/missions/{mission['id']}/binding", json={
        'agent_id': 'opencode', 'model_id': 'deepseek-flash', 'skill_id': 'code-review'})
    assert binding.status_code == 200, binding.text
    assert binding.json()['skill_body_sha256'] == digest
    newer = 'Use a different review method.'
    newer_digest = client.app.state.store.skill_source_register('code-review', '2.0.0', newer)
    client.app.state.registries.register('skills', 'code-review', {
        'name': 'code-review', 'description': 'Review code', 'license': 'MIT',
        'source_url': 'https://example.com/code-review', 'version': '2.0.0',
        'body_sha256': newer_digest,
    })
    response = client.post(f"/api/v1/missions/{mission['id']}/opencode/run", json={
        'prompt': 'Check project', 'step_id': mission['steps'][0]['id']})
    assert response.status_code == 200, response.text
    command = runner.calls[0]['command']
    assert source in command[-1] and newer not in command[-1]
    assert command[-1].startswith('Check project\n')
    events = client.get(f"/api/v1/missions/{mission['id']}/events").json()
    started = next(e for e in events if e['type'] == 'OPENCODE_STARTED')['payload']
    ended = next(e for e in events if e['type'] == 'OPENCODE_COMPLETED')['payload']
    assert started['prompt'] == 'Check project' and source not in str(started)
    assert ended['skill_invocation_verified'] is True
    assert ended['skill_body_sha256'] == digest
    assert ended['skill_version'] == '1.2.0'
    assert source not in str(ended)
    usage = client.get('/api/v1/registries/skills/code-review/usage').json()
    assert usage['verified_invocations'] == 1
    assert usage['skill_invocation_verified'] is True


def test_streaming_runner_records_redacted_output_during_run_and_once(tmp_path):
    client_holder = {}
    mission_holder = {}

    class StreamingRunner:
        def __call__(self, command, cwd, timeout, on_output=None):
            # At this point the endpoint's run is still inside the runner. A
            # separate read sees the event before the runner returns.
            on_output('stdout', 'working')
            live = client_holder['client'].get(
                f"/api/v1/missions/{mission_holder['id']}/events").json()
            assert any(event['type'] == 'OUTPUT' and event['payload']['line'] == 'working'
                       for event in live)
            on_output('stderr', 'api_key=live-secret')
            return subprocess.CompletedProcess(command, 0,
                                               'working\napi_key=live-secret\n', '')

    client = make_client(tmp_path, OpenCodeAdapter(binary='opencode', runner=StreamingRunner()))
    mission = create(client, 'stream')
    client_holder['client'], mission_holder['id'] = client, mission['id']
    bind(client, mission)
    response = client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                           json={'prompt': 'stream output'})
    assert response.status_code == 200, response.text
    events = client.get(f"/api/v1/missions/{mission['id']}/events").json()
    emitted = [event for event in events if event['type'] in {'OUTPUT', 'ERROR'}]
    assert [event['payload']['line'] for event in emitted] == ['working', 'api_key=[REDACTED]']
    assert all(event['mission_id'] == mission['id'] and event['agent_id'] == 'opencode'
               for event in emitted)
    assert '[REDACTED]' in response.json()['stdout']
    assert 'live-secret' not in response.text


def test_run_endpoint_marks_step_failed_on_nonzero_exit(tmp_path):
    runner = FakeRunner(returncode=1, stdout='', stderr='tests failed\n')
    client = make_client(tmp_path, OpenCodeAdapter(binary='opencode', runner=runner))
    mission = create(client, 'A')
    bind(client, mission)
    client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                json={'prompt': 'task', 'step_id': mission['steps'][0]['id']})
    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail['steps'][0]['status'] == 'FAILED'
    assert detail['status'] == 'FAILED'
    events = client.get(f"/api/v1/missions/{mission['id']}/events").json()
    # TRUST auto-collection may append TRUST_EVIDENCE_COLLECTED after the
    # failure event (same transaction); the failure itself must still be in
    # the stream, with only trust-audit events allowed after it.
    types = [event['type'] for event in events]
    assert 'OPENCODE_FAILED' in types
    assert not set(types[types.index('OPENCODE_FAILED') + 1:]) - {'TRUST_EVIDENCE_COLLECTED'}
    assert any(event['type'] == 'ERROR' for event in events)


def test_run_endpoint_refuses_an_already_completed_step(tmp_path):
    """A completed step must be refused before a model call, not after.

    Re-running it used to execute the task and write a checkpoint/cost, then fail
    the final transition, leaving an orphaned run.
    """
    runner = FakeRunner(stdout='done\n')
    client = make_client(tmp_path, OpenCodeAdapter(binary='opencode', runner=runner))
    mission = create(client, 'A')
    bind(client, mission)
    step_id = mission['steps'][0]['id']

    first = client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                        json={'prompt': 'task', 'step_id': step_id})
    assert first.status_code == 200 and first.json()['status'] == 'OPENCODE_COMPLETED'
    calls_after_first = len(runner.calls)

    second = client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                         json={'prompt': 'task again', 'step_id': step_id})
    assert second.status_code == 422
    assert len(runner.calls) == calls_after_first  # no second execution
    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail['steps'][0]['status'] == 'COMPLETED'
    assert len(detail['sessions']) == 1


def test_run_endpoint_requires_opencode_binding(tmp_path):
    client = make_client(tmp_path, OpenCodeAdapter(binary='opencode', runner=FakeRunner()))
    mission = create(client, 'A')
    assert client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                       json={'prompt': 'task'}).status_code == 409


def _workspace(mission_id, root):
    return str(Path(root) / 'workspaces' / mission_id)


def _well_formed(mission_id, root, status='OPENCODE_COMPLETED', exit_code=0):
    return {'workspace': _workspace(mission_id, root), 'status': status, 'exit_code': exit_code,
            'stdout': 'ok\n', 'stderr': ''}


MALFORMED_CASES = {
    'not_a_mapping': lambda mission_id, root: None,
    'missing_workspace': lambda mission_id, root: {
        'status': 'OPENCODE_COMPLETED', 'exit_code': 0, 'stdout': '', 'stderr': ''},
    'workspace_outside_root': lambda mission_id, root: {
        **_well_formed(mission_id, root), 'workspace': str(root)},
    'workspace_shared_root': lambda mission_id, root: {
        **_well_formed(mission_id, root), 'workspace': str(Path(root) / 'workspaces')},
    'invalid_status': lambda mission_id, root: {
        **_well_formed(mission_id, root), 'status': 'BANANA'},
    'completed_nonzero_exit': lambda mission_id, root: {
        **_well_formed(mission_id, root), 'exit_code': 1},
    'failed_zero_exit': lambda mission_id, root: {
        **_well_formed(mission_id, root, status='OPENCODE_FAILED', exit_code=0)},
    'timeout_with_exit': lambda mission_id, root: {
        **_well_formed(mission_id, root, status='OPENCODE_TIMEOUT', exit_code=1)},
    'missing_exit_code': lambda mission_id, root: {
        'workspace': _workspace(mission_id, root), 'status': 'OPENCODE_FAILED',
        'stdout': '', 'stderr': 'boom\n'},
    'invalid_exit_code': lambda mission_id, root: {
        **_well_formed(mission_id, root), 'exit_code': 'zero'},
    'invalid_stdout': lambda mission_id, root: {
        **_well_formed(mission_id, root), 'stdout': ['not', 'text']},
}


@pytest.mark.parametrize('build', list(MALFORMED_CASES.values()), ids=list(MALFORMED_CASES))
def test_malformed_adapter_result_fails_session_and_step_honestly(tmp_path, build):
    """A bad adapter result must not crash after the session starts or leave it dangling."""
    adapter = ResultAdapter(build)
    client = make_client(tmp_path, adapter)
    mission = create(client, 'malformed')
    bind(client, mission)
    step_id = mission['steps'][0]['id']

    response = client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                           json={'prompt': 'task', 'step_id': step_id})
    assert response.status_code == 422, response.text

    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail['status'] == 'FAILED'
    assert detail['steps'][0]['status'] == 'FAILED'
    assert [s['status'] for s in detail['sessions']] == ['FAILED']
    assert detail['artifacts'] == []

    events = client.get(f"/api/v1/missions/{mission['id']}/events").json()
    types = [event['type'] for event in events]
    assert 'OPENCODE_FAILED' in types and 'OPENCODE_COMPLETED' not in types
    failed = events[types.index('OPENCODE_FAILED')]
    assert failed['payload']['error']
    assert failed['mission_id'] == mission['id']
    assert failed['session_id'] == detail['sessions'][0]['id']
    assert failed['step_id'] == step_id


def test_well_formed_adapter_result_keeps_existing_semantics(tmp_path):
    """Validation must not disturb a valid result: it still completes and persists output."""
    adapter = ResultAdapter(lambda mission_id, root: _well_formed(mission_id, root))
    client = make_client(tmp_path, adapter)
    mission = create(client, 'valid')
    bind(client, mission)
    step_id = mission['steps'][0]['id']

    response = client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                           json={'prompt': 'task', 'step_id': step_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'OPENCODE_COMPLETED' and body['exit_code'] == 0
    assert len(body['artifacts']) == 1 and body['artifacts'][0].endswith('.stdout.log')

    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail['steps'][0]['status'] == 'COMPLETED'
    assert [s['status'] for s in detail['sessions']] == ['COMPLETED']
    assert len(detail['artifacts']) == 1
