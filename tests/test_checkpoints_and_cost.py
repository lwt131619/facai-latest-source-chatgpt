"""Git checkpoints, cost ledger and restart recovery."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter, summarize_output

HAS_GIT = shutil.which("git") is not None


class FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, command, cwd, timeout):
        (cwd / "app.py").write_text("print('hi')\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def json_event(**part):
    return json.dumps({"type": part.get("type", "step_finish"), "part": part})


def create_and_bind(client, title='任务'):
    mission = client.post('/api/v1/missions', json={
        'title': title, 'original_input': f'要求 {title}', 'steps': ['开发']}).json()
    client.put(f"/api/v1/missions/{mission['id']}/binding",
               json={'agent_id': 'opencode', 'model_id': 'deepseek-flash'})
    return mission


def test_summarize_output_reads_text_cost_and_tokens():
    stdout = "\n".join([
        json_event(type="text", text="hello"),
        json_event(cost=0.001, tokens={"total": 100}),
        json_event(cost=0.0002, tokens={"total": 140}),
        "not json",
    ])
    summary = summarize_output(stdout)
    assert summary["text"] == "hello"
    assert summary["cost"] == pytest.approx(0.0012)
    assert summary["tokens"] == 140


@pytest.mark.skipif(not HAS_GIT, reason="git is required for checkpoint tests")
def test_run_records_cost_ledger_and_git_checkpoint(tmp_path):
    stdout = json_event(type="text", text="done") + "\n" + json_event(cost=0.003, tokens={"total": 500})
    client = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary='opencode', runner=FakeRunner(stdout=stdout))))
    mission = create_and_bind(client)

    body = client.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                       json={'prompt': 'write app.py', 'step_id': mission['steps'][0]['id']}).json()
    assert body['cost'] == pytest.approx(500 / 1_000_000 * 218)
    assert body['tokens'] == 500
    assert body['checkpoint']

    # 逆向克隆防护：run 响应不再携带服务器 workspace 路径，从 store 推导。
    workspace = Path(client.app.state.store.root) / 'workspaces' / mission['id']
    assert (workspace / '.git').is_dir()
    tracked = subprocess.run(['git', 'show', '--stat', '--format=', 'HEAD'],
                             cwd=workspace, capture_output=True, text=True).stdout
    assert 'app.py' in tracked
    assert '.facai' not in tracked

    detail = client.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail['cost_total'] == pytest.approx(500 / 1_000_000 * 218)
    assert len(detail['checkpoints']) == 1
    assert detail['checkpoints'][0]['commit_hash'] == body['checkpoint']
    events = client.get(f"/api/v1/missions/{mission['id']}/events").json()
    types = [event['type'] for event in events]
    assert 'COST_RECORDED' in types and 'GIT_CHECKPOINT' in types
    cost_event = next(event for event in events if event['type'] == 'COST_RECORDED')
    assert cost_event['payload']['amount'] == pytest.approx(500 / 1_000_000 * 218)


def test_checkpoint_skips_cleanly_when_no_changes(tmp_path):
    from facai.checkpoint import checkpoint
    workspace = tmp_path / 'empty'
    workspace.mkdir()
    first = checkpoint(workspace, 'init')
    assert first['committed'] is True  # records the generated .gitignore
    second = checkpoint(workspace, 'again')
    assert second['committed'] is False and second['reason'] in {'no changes', 'git unavailable'}


@pytest.mark.skipif(not HAS_GIT, reason="git is required for checkpoint tests")
def test_checkpoint_initializes_nested_workspace_without_committing_parent(tmp_path):
    """A workspace under the source checkout must get its own Git boundary."""
    from facai.checkpoint import checkpoint

    # Build an actual parent repository rather than depending on this test
    # suite having been checked out with .git (development ZIPs omit history).
    repo_root = tmp_path / 'parent-repository'
    repo_root.mkdir()
    subprocess.run(['git', 'init', str(repo_root)], check=True, capture_output=True)
    (repo_root / 'parent.txt').write_text('parent content', encoding='utf-8')
    subprocess.run(['git', 'add', 'parent.txt'], cwd=repo_root, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-m', 'initial parent'], cwd=repo_root, check=True, capture_output=True)
    before = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo_root,
                            capture_output=True, text=True, check=True).stdout.strip()
    workspace = repo_root / 'nested-workspace'
    workspace.mkdir()
    (workspace / 'result.txt').write_text('mission result', encoding='utf-8')

    result = checkpoint(workspace, 'mission checkpoint')

    assert result['committed'] is True
    assert (workspace / '.git').is_dir()
    top_level = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=workspace,
                               capture_output=True, text=True, check=True).stdout.strip()
    assert Path(top_level).resolve() == workspace.resolve()
    after = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo_root,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert after == before


@pytest.mark.skipif(not HAS_GIT, reason="git is required for checkpoint tests")
def test_checkpoint_works_with_linked_worktree(tmp_path):
    """A real linked worktree uses a .git pointer file and remains intact."""
    from facai.checkpoint import checkpoint, diff

    main_repo = tmp_path / 'main-repo'
    main_repo.mkdir()
    assert subprocess.run(['git', 'init', str(main_repo)], capture_output=True).returncode == 0
    (main_repo / 'main.txt').write_text('main content', encoding='utf-8')
    subprocess.run(['git', 'add', 'main.txt'], cwd=main_repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-m', 'initial'], cwd=main_repo, check=True,
                   capture_output=True, text=True)

    worktree = tmp_path / 'linked-worktree'
    subprocess.run(['git', 'worktree', 'add', '--detach', str(worktree), 'HEAD'],
                   cwd=main_repo, check=True, capture_output=True, text=True)
    git_pointer = worktree / '.git'
    assert git_pointer.is_file()
    assert git_pointer.read_text(encoding='utf-8').startswith('gitdir: ')

    (worktree / 'linked.txt').write_text('linked content', encoding='utf-8')
    first = checkpoint(worktree, 'worktree checkpoint')
    assert first['committed'] is True and first['hash']
    assert git_pointer.is_file()
    assert diff(worktree)['available'] is True


def test_state_survives_process_restart_without_raw_keys(tmp_path):
    client = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary='opencode', runner=FakeRunner())))
    mission = create_and_bind(client, '持久化')
    client.put('/api/v1/registries/models/persisted', json={
        'provider': 'local', 'endpoint': 'http://127.0.0.1:11434/v1',
        'model': 'example', 'api_key_env': 'LOCAL_KEY'})
    client.post(f"/api/v1/missions/{mission['id']}/events", json={'type': 'NOTE', 'payload': {'text': 'hi'}})

    reopened = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary='opencode', runner=FakeRunner())))
    restored = reopened.get(f"/api/v1/missions/{mission['id']}").json()
    assert restored['agent_id'] == 'opencode' and restored['model_id'] == 'deepseek-flash'
    assert any(event['type'] == 'NOTE' for event in reopened.get(f"/api/v1/missions/{mission['id']}/events").json())
    assert reopened.get('/api/v1/registries/models').json()['persisted']['api_key_env'] == 'LOCAL_KEY'

    with sqlite3.connect(tmp_path / 'facai.sqlite3') as db:
        blob = ' '.join(str(row[0]) for row in db.execute("SELECT value FROM registries"))
    assert 'sk-' not in blob and 'must-not' not in blob
