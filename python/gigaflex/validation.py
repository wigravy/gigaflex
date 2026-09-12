"""Explicit project checks and exact repository-state bindings."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from .git import GitService


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout: float = 300
    phases: tuple[str, ...] = ("review", "finalize")


def parse_validation_commands(raw: str) -> tuple[ValidationCommand, ...]:
    values = json.loads(raw)
    if not isinstance(values, list):
        raise ValueError("validation_commands must be a JSON list")
    commands = []
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict) or set(value) - {"name", "argv", "cwd", "timeout", "phases"}:
            raise ValueError(f"invalid validation command {index}")
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and '\0' not in arg for arg in argv) or not argv[0]:
            raise ValueError("validation argv must be a nonempty list of strings")
        cwd = value.get("cwd", ".")
        if not isinstance(cwd, str) or '\0' in cwd or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            raise ValueError("validation cwd must stay inside the execution worktree")
        timeout = value.get("timeout", 300)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("validation timeout must be a positive finite number")
        phases = value.get("phases", ["review", "finalize"])
        if not isinstance(phases, list) or not phases or any(item not in ("task", "review", "finalize") for item in phases):
            raise ValueError("validation phases must contain task, review, or finalize")
        name = value.get("name", f"check-{index}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("validation name must be nonempty")
        commands.append(ValidationCommand(name, tuple(argv), cwd, timeout, tuple(phases)))
    return tuple(commands)


@dataclass(frozen=True)
class RepositoryState:
    head: str
    tree: str
    index: str


def repository_state(git: GitService, excluded: tuple[Path, ...] = ()) -> RepositoryState:
    index = git.run("write-tree").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="gigaflex-state-") as temporary:
        snapshot = git.create_review_snapshot(Path(temporary) / "index", excluded, index_ref=index)
    return RepositoryState(git.head_commit(), git.tree_id(snapshot), index)


def run_validation(command: ValidationCommand, root: Path) -> dict[str, object]:
    started = time.monotonic()
    result = {**asdict(command), "status": "failed", "returncode": None, "output": ""}
    cwd = (root / command.cwd).resolve()
    if not cwd.is_relative_to(root.resolve()):
        result["output"] = "validation cwd resolves outside the execution worktree"
        return result
    with tempfile.TemporaryFile() as output:
        process = None
        try:
            process = subprocess.Popen(command.argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
            process.wait(timeout=command.timeout)
            result["returncode"] = process.returncode
            result["status"] = "passed" if process.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            result["status"] = "timed_out"
        except OSError as exc:
            result["output"] = str(exc)
        finally:
            if process is not None and process.poll() is None:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
            output.seek(0, 2)
            size = output.tell()
            output.seek(max(0, size - 50_000))
            result["output"] = str(result["output"]) + output.read().decode("utf-8", errors="replace")
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result
