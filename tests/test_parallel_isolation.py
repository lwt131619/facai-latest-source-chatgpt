"""Parallel same-mission branches: isolated git worktrees behind a merge gate.

Git-dependent cases mirror the HAS_GIT pattern from test_checkpoints_and_cost.py
and skip cleanly where git is unavailable; the fallback test runs everywhere
because it simulates a git-less host by monkeypatching the worktree factory.
"""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import facai.checkpoint as checkpoint_module
from facai.checkpoint import prepare_worktree, remove_worktree
from facai.execution import merge_worktree
from facai.opencode import OpenCodeAdapter, resolve_override_workspace
from facai.registries import Registries
from facai.scheduler import Scheduler
from facai.store import Store

HAS_GIT = shutil.which("git") is not None

GIT_AUTHOR = ["-c", "user.name=Facai-Test", "-c", "user.email=facai-test@localhost"]


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _commit_all(repo, message):
    assert _git(["add", "-A"], repo).returncode == 0
    committed = _git([*GIT_AUTHOR, "commit", "-m", message], repo)
    assert committed.returncode == 0, committed.stderr


def _seed_repo(workspace):
    """The state a first checkpoint leaves behind: an initialized repo, one commit."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".gitignore").write_text(".facai/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    assert _git(["init"], workspace).returncode == 0
    _commit_all(workspace, "seed checkpoint")


def _build_scheduler(tmp_path, runner, concurrency=2):
    store = Store(tmp_path)
    adapter = OpenCodeAdapter(binary="opencode", runner=runner)
    scheduler = Scheduler(store, adapter, Registries(store), tmp_path,
                          concurrency=concurrency, worker_id="parallel-test")
    return store, scheduler


class DisjointFileRunner:
    """Fake OpenCode CLI: writes one file named after its run directory.

    Records the execution directory of every call so tests can prove distinct
    workspaces. Files are disjoint by construction (one name per directory).
    """

    def __init__(self):
        self.dirs = []

    def __call__(self, command, cwd, timeout):
        self.dirs.append(str(cwd))
        dir_flag = command.index("--dir")
        assert command[dir_flag + 1] == str(cwd)
        (Path(cwd) / f"{Path(cwd).name}.txt").write_text(
            f"made by {Path(cwd).name}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0,
                                           '{"type":"text","text":"ok"}\n', "")


def make_dag_mission(store, title):
    mission = store.create(title, f"input {title}", [
        {"id": "step-a", "label": "A"},
        {"id": "step-b", "label": "B"},
        {"id": "step-c", "label": "C", "depends_on": ["step-a", "step-b"]},
    ])
    store.bind(mission["id"], "opencode", "deepseek-flash")
    return store.get(mission["id"])


# --- (a) prepare_worktree ------------------------------------------------------


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_prepare_worktree_creates_real_linked_worktree(tmp_path):
    base = tmp_path / "workspaces" / "m1"
    _seed_repo(base)

    worktree = prepare_worktree(base, suffix="abcd1234")

    assert worktree == {"path": str(base.parent / "m1-abcd1234"),
                        "branch": "facai/m1-abcd1234"}
    path = Path(worktree["path"])
    assert (path / ".git").is_file()  # linked worktree uses a .git pointer file
    assert (path / ".git").read_text(encoding="utf-8").startswith("gitdir: ")
    assert (path / "seed.txt").exists()  # branched from the checkpointed HEAD

    remove_worktree(path, base=base)
    assert not path.exists()
    assert _git(["branch", "--list", "facai/m1-abcd1234"], base).stdout.strip() == ""


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_prepare_worktree_returns_none_on_empty_dir_or_commitless_repo(tmp_path):
    empty = tmp_path / "workspaces" / "m2"
    empty.mkdir(parents=True)
    assert prepare_worktree(empty, suffix="x") is None  # not a repository yet

    fresh = tmp_path / "workspaces" / "m3"
    fresh.mkdir(parents=True)
    assert _git(["init"], fresh).returncode == 0
    assert prepare_worktree(fresh, suffix="x") is None  # HEAD missing: no commits


def test_prepare_worktree_returns_none_when_git_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "_git_available", lambda: False)
    base = tmp_path / "workspaces" / "m4"
    base.mkdir(parents=True)
    assert prepare_worktree(base, suffix="x") is None


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_prepare_worktree_clears_residue_and_retries_same_suffix(tmp_path):
    """A leftover worktree/branch of the same step no longer wedges isolation.

    A merge conflict keeps its worktree for inspection and a requeued job
    reuses the same step-derived suffix, so the next prepare for that step used
    to fail unconditionally and silently drop isolation. prepare_worktree now
    clears the residue and retries once, for both residue shapes: the directory
    still present, and only the stale registration plus branch left.
    """
    base = tmp_path / "workspaces" / "m-residue"
    _seed_repo(base)

    first = prepare_worktree(base, suffix="step00003")
    assert first == {"path": str(base.parent / "m-residue-step00003"),
                     "branch": "facai/m-residue-step00003"}
    # Residue shape 1: the conflict path keeps the whole worktree (with
    # uncommitted inspection state); re-preparing the same suffix must clear
    # it and cut a fresh branch from HEAD.
    residue = Path(first["path"]) / "left-for-inspection.txt"
    residue.write_text("old branch state\n", encoding="utf-8")

    second = prepare_worktree(base, suffix="step00003")
    assert second == first
    assert not residue.exists()
    assert (Path(second["path"]) / "seed.txt").exists()
    linked = [line for line in _git(["worktree", "list", "--porcelain"], base).stdout.splitlines()
              if line.startswith("worktree ")]
    assert len(linked) == 2  # the base plus exactly one re-created worktree

    # Residue shape 2: the directory is already gone by hand, leaving a stale
    # registration and the branch behind — the same retry path recovers.
    shutil.rmtree(second["path"])
    third = prepare_worktree(base, suffix="step00003")
    assert third == first
    assert Path(third["path"]).exists()
    assert _git(["branch", "--list", "facai/m-residue-step00003"],
                base).stdout.strip() != ""

    remove_worktree(third["path"], base=base)
    assert _git(["branch", "--list", "facai/m-residue-step00003"],
                base).stdout.strip() == ""


# --- (b)/(c) merge gate --------------------------------------------------------


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_merge_worktree_merges_disjoint_branch_and_removes_worktree(tmp_path):
    base = tmp_path / "workspaces" / "mission-merge"
    _seed_repo(base)
    worktree = prepare_worktree(base, suffix="step00001")
    path = Path(worktree["path"])
    (path / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _commit_all(path, "branch checkpoint")

    store = Store(tmp_path / "ledger")
    result = merge_worktree(store, base, worktree, "mission-merge", "step00001")

    assert result["merged"] is True
    assert "feature.txt" in result["stat"]
    assert (base / "feature.txt").read_text(encoding="utf-8") == "feature work\n"
    assert not path.exists()  # merged branches leave no worktree behind
    assert _git(["branch", "--list", "facai/mission-merge-step00001"],
                base).stdout.strip() == ""


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_merge_worktree_conflict_aborts_and_keeps_worktree(tmp_path):
    base = tmp_path / "workspaces" / "mission-conflict"
    _seed_repo(base)
    worktree = prepare_worktree(base, suffix="step00002")
    path = Path(worktree["path"])
    (path / "shared.txt").write_text("branch version\n", encoding="utf-8")
    _commit_all(path, "branch change")
    # The base moves the same file after the branch diverged.
    (base / "shared.txt").write_text("base version\n", encoding="utf-8")
    _commit_all(base, "base change")

    store = Store(tmp_path / "ledger")
    result = merge_worktree(store, base, worktree, "mission-conflict", "step00002")

    assert result == {"merged": False, "error": "CONFLICT"}
    assert path.exists()  # kept for inspection
    assert (path / "shared.txt").read_text(encoding="utf-8") == "branch version\n"
    assert (base / "shared.txt").read_text(encoding="utf-8") == "base version\n"
    # No merge is left half-open in the base workspace.
    assert "UU" not in _git(["status", "--porcelain"], base).stdout


# --- (d) end-to-end parallel branches ------------------------------------------


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_parallel_branches_execute_in_distinct_worktrees_and_merge(tmp_path):
    runner = DisjointFileRunner()
    store, scheduler = _build_scheduler(tmp_path, runner, concurrency=2)
    mission = make_dag_mission(store, "并行")
    workspace = Path(mission["workspace"])
    _seed_repo(workspace)  # first checkpoint state, so branches can be cut
    steps = {step["label"]: step["id"] for step in mission["steps"]}
    for label in ("A", "B", "C"):
        store.enqueue_job(mission["id"], f"task {label}", step_id=steps[label])

    assert scheduler.run_pending() == 3

    jobs = store.jobs(mission["id"])
    assert all(job["status"] == "COMPLETED" for job in jobs)
    detail = store.get(mission["id"])
    assert detail["progress"] == 100
    assert all(step["status"] == "COMPLETED" for step in detail["steps"])

    # A and B ran in two distinct isolated worktree paths, not the shared tree.
    a_dir = next(d for d in runner.dirs if f"{mission['id']}-step-a" in d)
    b_dir = next(d for d in runner.dirs if f"{mission['id']}-step-b" in d)
    assert a_dir != b_dir
    assert workspace not in (Path(a_dir), Path(b_dir))
    # C ran after both dependencies completed, in the shared workspace.
    c_dir = next(d for d in runner.dirs if d == str(workspace))
    assert runner.dirs.index(c_dir) > max(runner.dirs.index(a_dir), runner.dirs.index(b_dir))

    # Both branches merged into the shared workspace; worktrees cleaned up.
    assert (workspace / (Path(a_dir).name+'.txt')).exists()
    assert (workspace / (Path(b_dir).name+'.txt')).exists()
    assert not Path(a_dir).exists() and not Path(b_dir).exists()

    events = store.events(mission["id"])
    types = [event["type"] for event in events]
    assert types.count("WORKTREE_CREATED") == 2
    assert types.count("WORKTREE_MERGED") == 2
    assert "WORKTREE_MERGE_FAILED" not in types
    created = {event["payload"]["branch"] for event in events
               if event["type"] == "WORKTREE_CREATED"}
    assert created == {'facai/'+Path(a_dir).name, 'facai/'+Path(b_dir).name}
    assert all(any(job['id'] in branch for job in jobs) for branch in created)
    merged = next(event for event in events if event["type"] == "WORKTREE_MERGED")
    assert f"{mission['id']}-step-" in merged["payload"]["result"]


@pytest.mark.skipif(not HAS_GIT, reason="git is required for worktree isolation")
def test_worktree_run_logs_survive_merge_in_shared_workspace(tmp_path):
    """Raw run logs of parallel branches stay readable after their merge.

    The worktree directory is deleted on merge success, so logs persisted
    there used to leave the artifacts center with rows that 404 forever.
    Worktree runs now write into the shared mission workspace under a
    step-prefixed name; solo runs keep the historical unprefixed name.
    """
    runner = DisjointFileRunner()
    store, scheduler = _build_scheduler(tmp_path, runner, concurrency=2)
    mission = make_dag_mission(store, "日志")
    workspace = Path(mission["workspace"])
    _seed_repo(workspace)
    steps = {step["label"]: step["id"] for step in mission["steps"]}
    for label in ("A", "B", "C"):
        store.enqueue_job(mission["id"], f"task {label}", step_id=steps[label])

    assert scheduler.run_pending() == 3

    # The worktrees themselves are gone after the merges ...
    assert not any(Path(d).exists() for d in runner.dirs if d != str(workspace))
    # ... but every log artifact is alive in the shared mission workspace,
    # satisfying exactly the parent-chain check the artifacts API applies.
    # (DisjointFileRunner emits stdout only, so one artifact per run.)
    artifacts = store.recent_artifacts()
    assert len(artifacts) == 3
    for row in artifacts:
        path = Path(row["path"])
        assert workspace in path.parents
        assert path.exists()
        assert path.read_text(encoding="utf-8") == '{"type":"text","text":"ok"}\n'
    for label in ("A", "B"):
        names = {row["name"] for row in artifacts
                 if row["name"].startswith(f"{steps[label][:8]}-opencode-")}
        assert len(names) == 1  # step-prefixed, not tied to the worktree path
    shared_names = {row["name"] for row in artifacts if row["name"].startswith("opencode-")}
    assert len(shared_names) == 1  # the solo C run keeps the unprefixed name


# --- (e) git-less fallback ------------------------------------------------------


class OverlapProbe(DisjointFileRunner):
    def __init__(self):
        super().__init__()
        self.lock=threading.Lock();self.active=0;self.peak=0

    def __call__(self, command, cwd, timeout):
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active)
        try:
            time.sleep(.03)
            return super().__call__(command,cwd,timeout)
        finally:
            with self.lock:self.active-=1


def test_worktree_unavailable_serializes_shared_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_OPENCODE_SERIAL','0')
    monkeypatch.setattr(checkpoint_module, "prepare_worktree",
                        lambda base_workspace, suffix: None)
    runner = OverlapProbe()
    store, scheduler = _build_scheduler(tmp_path, runner, concurrency=2)
    mission = make_dag_mission(store, "回退")
    steps = {step["label"]: step["id"] for step in mission["steps"]}
    for label in ("A", "B", "C"):
        store.enqueue_job(mission["id"], f"task {label}", step_id=steps[label])

    assert scheduler.run_pending() == 3
    assert all(job["status"] == "COMPLETED" for job in store.jobs(mission["id"]))
    detail = store.get(mission["id"])
    assert detail["progress"] == 100

    # Every execution (including the parallel A/B batch) used the shared
    # mission workspace — exactly today's single-workspace behavior.
    assert set(runner.dirs) == {str(mission["workspace"])}
    assert runner.peak == 1  # Even when the backend itself permits parallel calls.
    assert all(job['workspace_mode']=='SHARED' for job in store.jobs(mission['id']))
    events = store.events(mission["id"])
    worktree_events = [event for event in events
                       if event["type"].startswith("WORKTREE")]
    assert worktree_events == []
    assert any(event['type']=='WORKSPACE_SERIALIZED' for event in events)


@pytest.mark.skipif(not HAS_GIT, reason='git is required')
def test_creation_failure_never_runs_parallel_in_shared_tree(tmp_path,monkeypatch):
    monkeypatch.setenv('FACAI_OPENCODE_SERIAL','0')
    runner=OverlapProbe();store,scheduler=_build_scheduler(tmp_path,runner,concurrency=2)
    mission=make_dag_mission(store,'isolation failure');_seed_repo(Path(mission['workspace']))
    for step in mission['steps']:
        store.enqueue_job(mission['id'],'fixture',step_id=step['id'])
    monkeypatch.setattr(checkpoint_module,'prepare_worktree',lambda *args,**kwargs:None)
    assert len(scheduler.run_once())==2
    assert runner.dirs==[] and store.costs(mission['id'])==[]
    assert all(job['status']=='QUEUED' and job['attempts']==0 for job in store.jobs(mission['id']))
    assert sum(e['type']=='WORKTREE_UNAVAILABLE' for e in store.events(mission['id']))==2
    assert not any(e['type']=='OPENCODE_STARTED' for e in store.events(mission['id']))
    # A subsequent serial reservation remains runnable without git isolation.
    monkeypatch.setattr(checkpoint_module,'isolation_ready',lambda _:False)
    with store.connect() as db:db.execute('UPDATE jobs SET not_before=NULL')
    assert scheduler.run_pending()==3 and runner.peak==1
    assert all(job['status']=='COMPLETED' for job in store.jobs(mission['id']))


@pytest.mark.skipif(not HAS_GIT, reason='git is required')
def test_step_prefix_collisions_cannot_reuse_another_live_worktree(tmp_path,monkeypatch):
    monkeypatch.setenv('FACAI_OPENCODE_SERIAL','0')
    runner=OverlapProbe();store,scheduler=_build_scheduler(tmp_path,runner,concurrency=2)
    mission=store.create('prefix fixture','fixture',[{'id':'analysis-one','label':'One'}, {'id':'analysis-two','label':'Two'}])
    store.bind(mission['id'],'opencode','deepseek-flash');_seed_repo(Path(mission['workspace']))
    for step in mission['steps']:store.enqueue_job(mission['id'],'fixture',step_id=step['id'])
    assert scheduler.run_pending()==2
    assert len(set(runner.dirs))==2
    assert all(job['status']=='COMPLETED' for job in store.jobs(mission['id']))
    assert all((Path(mission['workspace'])/(Path(directory).name+'.txt')).exists() for directory in runner.dirs)


# --- workspace override validation ---------------------------------------------


def test_workspace_override_must_resolve_under_mission_root(tmp_path):
    override = tmp_path / "workspaces" / "m1" / "m1-branch01"
    assert resolve_override_workspace(str(override), tmp_path) == override.resolve()

    with pytest.raises(ValueError):
        resolve_override_workspace(str(tmp_path), tmp_path)  # outside root/workspaces
    with pytest.raises(ValueError):
        resolve_override_workspace(str(tmp_path / "workspaces"), tmp_path)  # shared root itself

    seen = []

    def runner(command, cwd, timeout):
        seen.append(str(cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    adapter = OpenCodeAdapter(binary="opencode", runner=runner)
    result = adapter.execute("m1", tmp_path, "task", workspace_override=str(override))
    assert result["workspace"] == str(override)
    assert seen == [str(override)]
