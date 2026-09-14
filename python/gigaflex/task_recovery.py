from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import shlex
import re
import shutil
from typing import TYPE_CHECKING
import uuid

from .git import GitError, GitService, TaskWorktree
from .checkpoint import ResumeError
from .artifacts import ArtifactSnapshot, artifact_paths

if TYPE_CHECKING:
    from .git import TaskWorktreeManager


class TaskRecoveryError(GitError):
    task_recovery: dict[str, object]


class TaskRecoveryInterrupted(KeyboardInterrupt):
    task_recovery: dict[str, object]


def workspace_record(workspace: TaskWorktree, label: str) -> dict[str, object]:
    return {
        "task": label,
        "repository": str(workspace.repo_root),
        "original_worktree": str(workspace.path),
        "base_commit": workspace.base_commit,
        "snapshot_commit": workspace.snapshot_commit,
        "original_dirty_paths": sorted(str(path) for path in workspace.original_dirty_paths),
        "original_index_tree": workspace.original_index_tree,
        "original_branch": workspace.original_branch,
        "phase": workspace.phase,
        "phase_context": workspace.phase_context,
        "artifact_paths": [str(path) for path in workspace.manager.artifact_paths],
    }


def task_interruption(exc, message: str, workspace: TaskWorktree, label: str, *, retained: bool = False):
    error = TaskRecoveryInterrupted(message) if isinstance(exc, KeyboardInterrupt) else TaskRecoveryError(message)
    error.task_recovery = workspace_record(workspace, label)
    error.task_recovery["directory"] = str(workspace.recovery_path) if workspace.recovery_path else ""
    error.task_recovery["retained"] = retained
    return error


def save_task_recovery(workspace: TaskWorktree, label: str, reason: str) -> Path:
    """Bundle an unpromoted task's commits, index, and non-ignored file state.

    The bundle lives in the common Git directory, outside every worktree. A
    failed save raises so the caller can retain the original workspace instead.
    """
    source_git = workspace.manager.git
    common_dir = Path(source_git.run("rev-parse", "--git-common-dir").stdout.strip())
    common_dir = (source_git.cwd / common_dir).resolve()
    recovery_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    recovery_dir = common_dir / "gigaflex" / "recovery" / recovery_id
    recovery_dir.mkdir(parents=True, mode=0o700)
    task_git = GitService(workspace.path)
    if workspace.manager.artifact_paths:
        if workspace.artifact_snapshot is None or workspace.artifact_snapshot.directory is None:
            raise GitError("task artifact input snapshot is missing")
        shutil.copytree(workspace.artifact_snapshot.directory, recovery_dir / "artifacts-input", symlinks=True)
        ArtifactSnapshot.load(recovery_dir / "artifacts-input")
        workspace.manager.capture_artifacts(task_git, recovery_dir / "artifacts-worktree")
    head = task_git.resolve_commit("HEAD")
    index_tree = task_git.run("write-tree").stdout.strip()
    index_commit = task_git.run(
        "commit-tree", index_tree, "-p", head,
        input_text="gigaflex: recovery index\n",
        env={
            "GIT_AUTHOR_NAME": "GigaFlex", "GIT_AUTHOR_EMAIL": "gigaflex@localhost",
            "GIT_COMMITTER_NAME": "GigaFlex", "GIT_COMMITTER_EMAIL": "gigaflex@localhost",
        },
    ).stdout.strip()
    # Seed from the real index so force-added ignored paths keep their working
    # contents as well as the staged version recorded in index_commit.
    working_commit = task_git.create_review_snapshot(
        recovery_dir / "snapshot.index", index_ref=index_commit,
    )
    commits = {
        "head": head,
        "input": workspace.snapshot_commit,
        "index": index_commit,
        "worktree": working_commit,
    }
    for commit in (index_commit, working_commit):
        for entry in task_git.run("ls-tree", "-r", "-z", commit).stdout.split("\0"):
            metadata, separator, name = entry.partition("\t")
            if not separator or not metadata.startswith("160000 "):
                continue
            nested = workspace.path / name
            if nested.is_dir() and any(nested.iterdir()):
                # A parent bundle stores only a gitlink, not nested commits or
                # local files. Retain the workspace instead of losing them.
                raise GitError(f"cannot bundle populated nested repository or submodule: {name}")
    prefix = f"refs/gigaflex/recovery/{recovery_id}"
    refs = {name: f"{prefix}/{name}" for name in commits}
    created_refs: dict[str, str] = {}
    bundle = recovery_dir / "task.bundle"
    try:
        for name, commit in commits.items():
            task_git.run("update-ref", refs[name], commit, "")
            created_refs[refs[name]] = commit
        task_git.run(
            "bundle", "create", str(bundle), *refs.values(), f"^{workspace.base_commit}",
        )
        task_git.run("bundle", "verify", str(bundle))
        manifest = {
            **workspace_record(workspace, label),
            "version": 2,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "commits": commits,
            "refs": refs,
            "bundle": bundle.name,
        }
        (recovery_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        repo = shlex.quote(str(workspace.repo_root))
        bundle_arg = shlex.quote(str(bundle))
        refspec = shlex.quote(f"{prefix}/*:{prefix}/*")
        (recovery_dir / "README.txt").write_text(
            "GigaFlex task recovery\n\n"
            "This task was not promoted. Inspect the recovered work before accepting it.\n"
            "The bundle requires the original base commit in manifest.json.\n"
            "It preserves task commits, staged state, tracked files, and non-ignored\n"
            "untracked files. Configured ignored task artifacts are stored separately\n"
            "in artifacts-input/ and artifacts-worktree/ and restored by --resume.\n"
            "Other ignored untracked and external files are not included.\n\n"
            "Replace /path/to/new-recovery-worktree with a NEW directory. Run:\n\n"
            f"git -C {repo} fetch --no-tags {bundle_arg} {refspec}\n"
            f"git -C {repo} worktree add --detach /path/to/new-recovery-worktree {refs['worktree']}\n"
            f"git -C /path/to/new-recovery-worktree reset --mixed {head}\n"
            f"git -C /path/to/new-recovery-worktree read-tree {refs['index']}\n\n"
            "These commands reconstruct the failed task in the new worktree, including\n"
            "its staged/unstaged changes. They do not apply it to the execution branch.\n"
            f"The original task input can be inspected at {refs['input']}.\n"
            "Recovery bundles are retained until you remove them.\n",
            encoding="utf-8",
        )
    finally:
        for ref, commit in created_refs.items():
            # The verified bundle owns the saved objects; temporary refs must
            # not keep accumulating. A ref cleanup failure cannot lose work.
            task_git.run("update-ref", "-d", ref, commit, check=False)
    return recovery_dir


def restore_task_recovery(
    manager: TaskWorktreeManager, label: str, recovery: dict[str, object], root: Path,
) -> TaskWorktree:
    """Restore only into an isolated worktree, with an unchanged execution checkout."""
    retained = bool(recovery.get("retained"))
    directory = Path(str(recovery.get("directory", ""))).resolve()
    try:
        manifest = recovery if retained else json.loads((directory / "manifest.json").read_text())
        if not retained and manifest.get("version") != 2:
            raise ValueError("this bundle supports manual recovery only")
        if manifest["task"] != label or Path(manifest["repository"]).resolve() != manager.repo_root:
            raise ValueError("saved task or execution checkout differs from this run")
        for key in ("base_commit", "snapshot_commit", "original_index_tree"):
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", manifest[key]):
                raise ValueError(f"invalid saved {key}")
        if manager.git.current_branch() != manifest["original_branch"]:
            raise ValueError("execution branch changed since the task was saved")
        if artifact_paths(Path(value) for value in manifest.get("artifact_paths", [])) != artifact_paths(manager.artifact_paths):
            raise ValueError("task_artifact_paths changed since the task was saved")
        path = Path(manifest["original_worktree"]) if retained else root / "task"
        if retained:
            if not path.is_absolute() or not path.parent.name.startswith("gigaflex-task-"):
                raise ValueError("invalid retained task worktree path")
            retained_git = GitService(path)
            if retained_git.repo_root() != path.resolve() or not (path / ".git").is_file():
                raise ValueError("retained directory is not the original linked worktree")
            if _common_dir(retained_git) != _common_dir(manager.git):
                raise ValueError("retained worktree belongs to another repository")
        artifacts = None
        if manager.artifact_paths:
            if not retained:
                shutil.copytree(directory / "artifacts-input", root / "artifacts-input", symlinks=True)
            artifacts = ArtifactSnapshot.load(path.parent / "artifacts-input")
        workspace = TaskWorktree(
            manager, path, manager.repo_root, manifest["base_commit"], manifest["snapshot_commit"],
            frozenset(Path(value) for value in manifest["original_dirty_paths"]),
            original_index_tree=manifest["original_index_tree"],
            original_branch=manifest["original_branch"], resumed=True,
            phase=manifest.get("phase", "task"), phase_context=manifest.get("phase_context", {}),
            artifact_snapshot=artifacts,
        )
        temporary_refs = []
        try:
            if not retained:
                bundle = directory / "task.bundle"
                manager.git.run("bundle", "verify", str(bundle))
                for name in ("input", "index", "worktree"):
                    source = manifest["refs"][name]
                    manager.git.run("check-ref-format", source)
                    target = f"refs/gigaflex/resume/{uuid.uuid4().hex}/{name}"
                    temporary_refs.append(target)
                    manager.git.run("fetch", "--no-tags", "--no-write-fetch-head", str(bundle), f"{source}:{target}")
                    if manager.git.resolve_commit(target) != manifest["commits"][name]:
                        raise ValueError("bundle commit differs from its manifest")
                if manifest["commits"]["input"] != workspace.snapshot_commit:
                    raise ValueError("bundle input differs from the saved task baseline")
            manager._assert_main_unchanged(workspace, "resume-check.index")
            if manager.git.run("write-tree").stdout.strip() != workspace.original_index_tree:
                raise ValueError("main index changed since the task was saved")
            if not retained:
                head = manifest["commits"]["head"]
                if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
                    raise ValueError("invalid saved task HEAD")
                manager.git.add_detached_worktree(path, manifest["commits"]["worktree"])
                try:
                    task_git = GitService(path)
                    task_git.run("reset", "--mixed", head)
                    task_git.run("read-tree", manifest["commits"]["index"])
                    if manager.artifact_paths:
                        saved_artifacts = ArtifactSnapshot.load(directory / "artifacts-worktree")
                        saved_artifacts.install(path, (Path(name) for name in saved_artifacts.state))
                except BaseException:
                    manager.git.remove_worktree(path)
                    raise
            return workspace
        finally:
            for ref in temporary_refs:
                manager.git.run("update-ref", "-d", ref, check=False)
    except (OSError, ValueError, KeyError, TypeError, GitError) as exc:
        raise ResumeError(
            f"cannot resume saved task: {exc}; saved work has been retained. "
            "Use --restart to work from the current checkout instead, or inspect the saved work manually."
        ) from exc


def _common_dir(git: GitService) -> Path:
    return (git.cwd / git.run("rev-parse", "--git-common-dir").stdout.strip()).resolve()


def lock_task_recovery(manager: TaskWorktreeManager, recovery: dict[str, object]):
    """Keep two resume processes from using the same saved work concurrently."""
    identity = str(recovery.get("directory") or recovery.get("original_worktree"))
    folder = _common_dir(manager.git) / "gigaflex" / "recovery-locks"
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")).open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ResumeError("saved task is already being resumed; wait for that process to finish") from exc
    return handle
