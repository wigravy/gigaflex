"""A separately checked bookkeeping transaction after result verification."""
from pathlib import Path

from .git import GitError, GitService, TaskWorktreeManager, completed_plan_path, move_plan_to_completed
from .plan import parse_plan_file
from .validation import repository_state


def archive_plan(manager: TaskWorktreeManager, plan: Path, message: str, *, recovery=None, expected=None) -> Path:
    source = plan.resolve().relative_to(manager.repo_root)
    context = (manager.resume("archive plan", recovery) if recovery else manager.create("archive plan"))
    with context as workspace:
        workspace.phase = "archive"
        if expected is not None and repository_state(manager.git, manager.ignored_paths) != expected:
            # No archive work has started. Do not save a packet that could skip
            # review of the changed checkout on the next launch.
            workspace.discard_verified()
            raise GitError("repository changed after verification; plan archive was not applied")
        git = GitService(workspace.path)
        original = manager.git.run("rev-parse", f"{workspace.snapshot_commit}:{source.as_posix()}").stdout.strip()
        current = workspace.path / source
        if recovery and not workspace.phase_context:
            # Older packets recorded no context if the move failed early.
            if git.head_commit() != workspace.snapshot_commit or git.is_dirty() or not current.is_file():
                raise GitError("saved plan archive has no destination and contains unfinished changes")
        if workspace.phase_context.get("source", source.as_posix()) != source.as_posix():
            raise GitError("saved plan archive belongs to another plan")
        workspace.phase_context["source"] = source.as_posix()
        if "target" in workspace.phase_context:
            target = Path(workspace.phase_context["target"])
            if target.is_absolute() or ".." in target.parts or target.parent != source.parent / "completed":
                raise GitError("invalid saved plan archive destination")
        else:
            target = completed_plan_path(current).relative_to(workspace.path)
            workspace.phase_context["target"] = target.as_posix()
        if current.exists():
            if git.run("hash-object", f"--path={source}", str(source)).stdout.strip() != original:
                raise GitError("plan archive source differs from the verified plan")
            if parse_plan_file(current).has_uncompleted_tasks():
                raise GitError("cannot archive a plan with incomplete tasks")
            move_plan_to_completed(current, target=workspace.path / target)
        _verify_plan(git, source, target, original)
        if not set(git.dirty_paths()).issubset({source, target}):
            raise GitError("plan archive changed files outside its bookkeeping boundary")
        # The candidate owns only this rename. Stage its remaining changes as a
        # whole: a deleted path may already be absent from the restored index.
        git.commit_all_if_dirty(message)
        _verify_plan(git, source, target, original)
        if git.run("rev-parse", f"HEAD:{target.as_posix()}").stdout.strip() != original:
            raise GitError("plan archive commit differs from the verified plan")
        if git.is_dirty() or git.changed_paths_between(workspace.snapshot_commit, "HEAD") != {source, target}:
            raise GitError("plan archive changed files outside its bookkeeping boundary")
        workspace.promote(git.head_commit())
        return workspace.repo_root / target


def _verify_plan(git: GitService, source: Path, target: Path, original: str) -> None:
    if ((git.cwd / source).exists() or (git.cwd / source).is_symlink() or (git.cwd / target).is_symlink()
            or git.run("hash-object", f"--path={target}", str(target)).stdout.strip() != original):
        raise GitError("plan archive must preserve the verified plan contents exactly")
