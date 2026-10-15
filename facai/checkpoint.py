"""Git checkpoints for a mission workspace.

A checkpoint is a real commit in the mission's own workspace. When git is
unavailable the caller gets a structured reason instead of a fabricated
success, so Mission Control never invents a checkpoint that did not happen.
"""

import re
import subprocess
import threading
from pathlib import Path

GITIGNORE = ".facai/\n__pycache__/\n*.pyc\n"
AUTHOR = ["-c", "user.name=Facai", "-c", "user.email=facai@localhost"]

# Git worktree administration is not safe to run concurrently on one repository
# from several scheduler threads, so worktree add/remove are serialized here.
_WORKTREE_OPS_LOCK = threading.Lock()


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _git_available():
    try:
        return _run(["--version"], Path.cwd()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _git_is_valid(workspace):
    """Validate that *workspace itself* is a repository, including worktrees.

    ``git rev-parse`` succeeds inside any parent repository too. Accepting that
    result would let checkpoint ``git add`` / ``git commit`` modify the FACAI
    source checkout when a mission workspace lives beneath it.
    """
    workspace = Path(workspace).resolve()
    try:
        result = _run(["rev-parse", "--show-toplevel"], workspace)
        if result.returncode != 0 or not result.stdout.strip():
            return False
        top_level = Path(result.stdout.strip()).resolve()
        return top_level == workspace
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_repo(workspace):
    workspace = Path(workspace)
    if _git_is_valid(workspace):
        return True
    workspace.mkdir(parents=True, exist_ok=True)
    result = _run(["init"], workspace)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git init failed")
    ignore = workspace / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE, encoding="utf-8")
    return True


def checkpoint(workspace, message):
    workspace = Path(workspace)
    if not workspace.is_dir():
        return {"committed": False, "reason": "workspace missing"}
    if not _git_available():
        return {"committed": False, "reason": "git unavailable"}
    try:
        ensure_repo(workspace)
    except RuntimeError as exc:
        return {"committed": False, "reason": str(exc)}
    _run(["add", "-A"], workspace)
    commit = _run([*AUTHOR, "commit", "-m", message], workspace)
    if commit.returncode != 0:
        blob = f"{commit.stdout}\n{commit.stderr}"
        if "nothing to commit" in blob or "no changes added" in blob:
            return {"committed": False, "reason": "no changes"}
        return {"committed": False, "reason": commit.stderr.strip() or "commit failed"}
    commit_hash = _run(["rev-parse", "HEAD"], workspace).stdout.strip()
    stat = _run(["show", "--stat", "--format=", "HEAD"], workspace).stdout.strip()
    return {"committed": True, "hash": commit_hash, "stat": stat}


def diff(workspace, rev=None, limit=40000):
    workspace = Path(workspace)
    if not _git_is_valid(workspace) or not _git_available():
        return {"available": False, "diff": "", "truncated": False}
    target = rev or "HEAD"
    show = _run(["show", "--format=", "--patch", target], workspace)
    if show.returncode != 0:
        return {"available": False, "diff": "", "truncated": False, "reason": show.stderr.strip()}
    text = show.stdout
    return {"available": True, "diff": text[:limit], "truncated": len(text) > limit}


def _safe_suffix(suffix):
    """Restrict a worktree suffix to characters safe in paths and ref names."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", str(suffix)).strip("-.")
    return cleaned or "branch"


def isolation_ready(workspace):
    """Preflight only; callers must still handle actual worktree creation failure."""
    try:
        base = Path(workspace)
        return (_git_available() and _git_is_valid(base)
                and _run(['rev-parse', '--verify', 'HEAD'], base).returncode == 0)
    except (OSError, subprocess.SubprocessError):
        return False


def prepare_worktree(base_workspace, suffix):
    """Create an isolated linked worktree of the mission workspace.

    Returns ``{"path": ..., "branch": ...}`` for a worktree checked out at
    ``<workspaces>/<mission_id>-<suffix>`` on branch ``facai/<mission_id>-<suffix>``
    cut from HEAD, or ``None`` whenever isolation is impossible — git missing,
    the base not a repository yet (an empty workspace before the first
    checkpoint) or no commits on HEAD. Callers fall back to the shared
    workspace, so a git-less system keeps today's behavior.
    """
    try:
        base = Path(base_workspace)
        if not _git_available() or not _git_is_valid(base):
            return None
        head = _run(["rev-parse", "--verify", "HEAD"], base)
        if head.returncode != 0:
            return None  # repository has no commits to branch from
        name = f"{base.name}-{_safe_suffix(suffix)}"
        branch = f"facai/{name}"
        with _WORKTREE_OPS_LOCK:
            added = _run(["worktree", "add", "-b", branch, str(base.parent / name)], base)
            if added.returncode != 0:
                # A previous run of this same step can own the branch or path:
                # a merge conflict keeps its worktree for inspection and a
                # requeued job reuses the same step-derived suffix. Clear the
                # residue and retry once before giving up on isolation.
                _clear_worktree_residue_locked(base.parent / name, branch, base)
                added = _run(["worktree", "add", "-b", branch, str(base.parent / name)], base)
        if added.returncode != 0:
            return None
        return {"path": str(base.parent / name), "branch": branch}
    except Exception:
        return None


def _worktree_main_repo(path):
    """The main repository root that owns the linked worktree at *path*, or None."""
    try:
        common = _run(["rev-parse", "--path-format=absolute", "--git-common-dir"], Path(path))
        if common.returncode != 0 or not common.stdout.strip():
            return None
        main_root = Path(common.stdout.strip()).resolve().parent
        return main_root if main_root.is_dir() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _remove_worktree_unlocked(target, repo):
    """Remove a linked worktree and its branch; caller holds the ops lock."""
    target = Path(target)
    removed = _run(["worktree", "remove", "--force", str(target)], repo)
    _run(["branch", "-D", f"facai/{target.name}"], repo)
    return removed.returncode == 0


def _clear_worktree_residue_locked(target, branch, repo):
    """Best-effort cleanup of a leftover worktree/branch; caller holds the lock.

    Covers both residue shapes: a still-present worktree directory (kept after
    a merge conflict, or left by a crashed worker) and a stale registration
    whose directory is already gone (``worktree prune`` drops the admin entry,
    ``branch -D`` frees the name). Every step is individually best-effort --
    whatever does not apply simply fails and is ignored.
    """
    _run(["worktree", "prune"], repo)
    _run(["worktree", "remove", "--force", str(target)], repo)
    _run(["branch", "-D", branch], repo)


def remove_worktree(path, base=None):
    """Best-effort removal of a linked worktree and its branch; never raises.

    The worktree directory is removed with ``git worktree remove --force`` and
    its ``facai/<name>`` branch deleted, so a merged branch leaves no residue.
    """
    try:
        target = Path(path)
        repo = Path(base) if base is not None else _worktree_main_repo(target)
        if repo is None:
            return False
        with _WORKTREE_OPS_LOCK:
            return _remove_worktree_unlocked(target, repo)
    except Exception:
        return False
