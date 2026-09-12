"""A separately checked bookkeeping transaction after result verification."""
from pathlib import Path

from .git import GitError, GitService, TaskWorktreeManager, move_plan_to_completed
from .plan import parse_plan_file
from .validation import repository_state


def archive_plan(manager: TaskWorktreeManager, plan: Path, message: str, *, recovery=None, expected=None) -> Path:
    source = plan.resolve().relative_to(manager.repo_root)
    context = (manager.resume("archive plan", recovery) if recovery else manager.create("archive plan"))
    with context as workspace:
        workspace.phase = "archive"
        if expected is not None and repository_state(manager.git, manager.ignored_paths) != expected:
            raise GitError("repository changed after verification; plan archive was not applied")
        git = GitService(workspace.path)
        original = manager.git.run("rev-parse", f"{workspace.snapshot_commit}:{source.as_posix()}").stdout.strip()
        if recovery:
            if workspace.phase_context.get("source") != source.as_posix():
                raise GitError("saved plan archive belongs to another plan")
            target = Path(workspace.phase_context["target"])
            if target.is_absolute() or ".." in target.parts or target.parent != source.parent / "completed":
                raise GitError("invalid saved plan archive destination")
        else:
            current = workspace.path / source
            if parse_plan_file(current).has_uncompleted_tasks():
                raise GitError("cannot archive a plan with incomplete tasks")
            moved = move_plan_to_completed(current)
            target = moved.relative_to(workspace.path)
            workspace.phase_context = {"source": source.as_posix(), "target": target.as_posix()}
            git.commit_paths([source, target], message)
        if (workspace.path / source).exists() or git.run("hash-object", f"--path={target}", str(target)).stdout.strip() != original:
            raise GitError("plan archive must preserve the verified plan contents exactly")
        if recovery and set(git.dirty_paths()).issubset({source, target}) and git.is_dirty():
            git.commit_paths([source, target], message)
        if git.is_dirty() or git.changed_paths_between(workspace.snapshot_commit, "HEAD") != {source, target}:
            raise GitError("plan archive changed files outside its bookkeeping boundary")
        workspace.promote(git.head_commit())
        return workspace.repo_root / target
