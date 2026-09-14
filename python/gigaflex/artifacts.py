"""File snapshots for explicitly selected, Git-ignored task artifacts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .git import GitService


def artifact_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    result = set()
    for value in paths:
        path = Path(value)
        if path.is_absolute() or not path.parts or ".." in path.parts or any(
            part.lower() == ".git" for part in path.parts
        ):
            raise ValueError(f"artifact path must be relative to the repository and outside .git: {value}")
        result.add(path)
    return tuple(sorted(result))


def _local_path(root: Path, relative: Path) -> Path:
    artifact_paths((relative,))
    target = root / relative
    for parent in target.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"artifact parent is a symlink: {relative}")
    return target


def _fingerprint(path: Path) -> str:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "link:" + os.readlink(path)
    if not stat.S_ISREG(mode):
        raise ValueError(f"artifact is not a regular file or symlink: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"file:{stat.S_IMODE(mode):o}:{digest.hexdigest()}"


@dataclass
class ArtifactSnapshot:
    state: dict[str, str]
    directory: Optional[Path] = None

    def signature(self) -> str:
        return hashlib.sha256(json.dumps(self.state, sort_keys=True).encode()).hexdigest()

    @classmethod
    def capture(
        cls, git: GitService, paths: Iterable[Path], directory: Optional[Path] = None,
        excluded_paths: Iterable[Path] = (),
    ) -> ArtifactSnapshot:
        selected = artifact_paths(paths)
        state: dict[str, str] = {}
        if selected:
            root = git.repo_root()
            for path in selected:
                _local_path(root, path)
            output = git.run(
                "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
                *(f":(literal){path.as_posix()}" for path in selected),
            ).stdout
            excluded = tuple(excluded_paths)
            for name in sorted(set(output.split("\0")) - {""}):
                relative = Path(name)
                if any(relative == path or path in relative.parents for path in excluded):
                    continue
                source = _local_path(root, relative)
                if directory is not None:
                    target = directory / "files" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target, follow_symlinks=False)
                    source = target
                state[name] = _fingerprint(source)
        snapshot = cls(state, directory)
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "manifest.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
        return snapshot

    @classmethod
    def load(cls, directory: Path) -> ArtifactSnapshot:
        state = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("invalid artifact snapshot manifest")
        for name, fingerprint in state.items():
            source = _local_path(directory / "files", Path(name))
            if _fingerprint(source) != fingerprint:
                raise ValueError(f"saved artifact differs from its manifest: {name}")
        return cls(state, directory)

    def changed_paths(self, other: ArtifactSnapshot) -> set[Path]:
        return {
            Path(name) for name in self.state.keys() | other.state.keys()
            if self.state.get(name) != other.state.get(name)
        }

    def install(self, root: Path, paths: Iterable[Path]) -> None:
        """Replace only selected files, without touching Git's index or following links."""
        selected = artifact_paths(paths)
        for relative in sorted(selected, key=lambda path: (-len(path.parts), path)):
            target = _local_path(root, relative)
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                target.rmdir()  # Never remove a directory containing unrelated files.
            for parent in target.parents:
                if parent == root:
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
        for relative in selected:
            if relative.as_posix() not in self.state:
                continue
            if self.directory is None:
                raise ValueError("artifact snapshot has no stored files")
            target = _local_path(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = _local_path(self.directory / "files", relative)
            shutil.copy2(source, target, follow_symlinks=False)
