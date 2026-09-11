from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Optional


TASK_HEADER_RE = re.compile(
    r"^#{2,3}\s+(?:Task|Iteration|Задача|Итерация)\s+(?:№\s*)?([^:]+?):\s*(.*)$",
    re.IGNORECASE,
)
OPENSPEC_TASK_HEADER_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
CHECKBOX_RE = re.compile(r"^\s*-\s+\[([ xX])\]\s*(.*)$")
TITLE_RE = re.compile(r"^#\s+(.*)$")
FORMAT_IN_TEXT_RE = re.compile(r"\[\s*[ xX]?\s*\]")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*\r?$")


@dataclass(frozen=True)
class Checkbox:
    text: str
    checked: bool
    implicit: bool = False

    @property
    def actionable(self) -> bool:
        return FORMAT_IN_TEXT_RE.search(self.text) is None


@dataclass
class Task:
    number: int
    title: str
    checkboxes: list[Checkbox] = field(default_factory=list)
    section: str = ""
    start_line: int = field(default=0, compare=False, repr=False)
    end_line: int = field(default=0, compare=False, repr=False)

    @property
    def complete(self) -> bool:
        return not self.has_uncompleted_actionable_work()

    def has_uncompleted_actionable_work(self) -> bool:
        return any(not cb.checked and cb.actionable for cb in self.checkboxes)

    @property
    def has_implicit_tracking(self) -> bool:
        return any(cb.implicit for cb in self.checkboxes)


@dataclass
class Plan:
    title: str = ""
    tasks: list[Task] = field(default_factory=list)

    def first_uncompleted_task_index(self) -> Optional[int]:
        for index, task in enumerate(self.tasks, start=1):
            if task.has_uncompleted_actionable_work():
                return index
        return None

    def first_uncompleted_task(self) -> Optional[Task]:
        index = self.first_uncompleted_task_index()
        return self.tasks[index - 1] if index is not None else None

    def tasks_matching(self, number: int, title: str) -> list[Task]:
        return [
            task
            for task in self.tasks
            if task.number == number and task.title == title
        ]

    def has_uncompleted_tasks(self) -> bool:
        return self.first_uncompleted_task_index() is not None


@dataclass(frozen=True)
class PlanSource:
    kind: str
    source_path: Path
    checklist_path: Path
    context_paths: tuple[Path, ...] = ()

    @property
    def name(self) -> str:
        return self.source_path.name if self.source_path.is_dir() else self.source_path.stem

    @property
    def is_openspec(self) -> bool:
        return self.kind == "openspec"


class FenceTracker:
    def __init__(self) -> None:
        self.open_marker = ""

    def skip(self, line: str) -> bool:
        if not self.open_marker:
            match = FENCE_OPEN_RE.match(line)
            if not match:
                return False
            self.open_marker = match.group(1)
            return True

        match = FENCE_CLOSE_RE.match(line)
        if match and len(match.group(1)) >= len(self.open_marker) and match.group(1)[0] == self.open_marker[0]:
            self.open_marker = ""
        return True


def parse_task_number(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return 0


def parse_plan(content: str, *, plan_format: str = "gigaflex") -> Plan:
    if plan_format not in {"gigaflex", "openspec"}:
        raise ValueError(f"unsupported plan format: {plan_format}")
    plan = Plan()
    current: Optional[Task] = None
    current_lines: list[str] = []
    fence = FenceTracker()

    def finish_current_task(end_line: int) -> None:
        nonlocal current, current_lines
        if current is None:
            return
        current.section = "\n".join(current_lines).rstrip()
        current.end_line = end_line
        if plan_format == "openspec" and not current.checkboxes:
            current.checkboxes.append(
                Checkbox(
                    text=f"{current.number}. {current.title}",
                    checked=False,
                    implicit=True,
                )
            )
        plan.tasks.append(current)

    lines = content.splitlines()
    for line_number, line in enumerate(lines, start=1):
        if fence.skip(line):
            if current is not None:
                current_lines.append(line)
            continue

        if not plan.title:
            title_match = TITLE_RE.match(line)
            if title_match:
                plan.title = title_match.group(1).strip()
                continue

        task_match = (
            OPENSPEC_TASK_HEADER_RE.match(line) or TASK_HEADER_RE.match(line)
            if plan_format == "openspec"
            else TASK_HEADER_RE.match(line)
        )
        if task_match:
            if current is not None:
                finish_current_task(line_number - 1)
            current = Task(
                number=parse_task_number(task_match.group(1)),
                title=task_match.group(2).strip(),
                start_line=line_number,
            )
            current_lines = [line]
            continue

        is_h2 = line.startswith("##") and not line.startswith("###")
        is_h1_after_title = line.startswith("#") and plan.title and not line.startswith("##")
        if current is not None and (is_h2 or is_h1_after_title):
            finish_current_task(line_number - 1)
            current = None
            current_lines = []
            continue

        if current is not None:
            current_lines.append(line)
            checkbox_match = CHECKBOX_RE.match(line)
            if checkbox_match:
                current.checkboxes.append(
                    Checkbox(
                        text=checkbox_match.group(2).strip(),
                        checked=checkbox_match.group(1).lower() == "x",
                    )
                )

    if current is not None:
        finish_current_task(len(lines))
    return plan


def task_plan_update_allowed(
    before: str,
    after: str,
    selected_task: Task,
    *,
    plan_format: str = "gigaflex",
) -> bool:
    """Allow only forward checkbox updates or the selected prose-task marker.

    All requirements, examples, headings, and other tasks remain unchanged.
    Line-ending style and a final newline do not affect this comparison.
    """
    matches = parse_plan(before, plan_format=plan_format).tasks_matching(
        selected_task.number, selected_task.title,
    )
    if len(matches) != 1:
        return False
    task = matches[0]
    original = before.splitlines()
    updated = after.splitlines()
    if task.has_implicit_tracking:
        marked = original.copy()
        marked.insert(task.start_line, f"- [x] {task.number}. {task.title}")
        return updated == original or updated == marked
    if len(original) != len(updated):
        return False
    fence = FenceTracker()
    for index, (old_line, new_line) in enumerate(zip(original, updated)):
        in_selected = task.start_line - 1 <= index < task.end_line
        in_fence = fence.skip(old_line) if in_selected else False
        if old_line == new_line:
            continue
        checkbox = CHECKBOX_RE.match(old_line) if in_selected and not in_fence else None
        if checkbox is None or not Checkbox(checkbox.group(2), False).actionable:
            return False
        position = checkbox.start(1)
        if not any(
            new_line == old_line[:position] + state + old_line[position + 1:]
            for state in ("x", "X")
        ):
            return False
    return True


def parse_plan_file(path: Path, *, plan_format: str = "gigaflex") -> Plan:
    return parse_plan(path.read_text(encoding="utf-8"), plan_format=plan_format)


def resolve_markdown_plan(path: Path) -> PlanSource:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"plan file not found: {resolved}")
    return PlanSource(
        kind="gigaflex",
        source_path=resolved,
        checklist_path=resolved,
    )


def resolve_openspec_change(path: Path) -> PlanSource:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"OpenSpec change directory not found: {resolved}")
    if resolved.parent.name == "archive":
        raise ValueError(f"OpenSpec change is archived and cannot be executed: {resolved}")

    checklist = resolved / "tasks.md"
    if not checklist.is_file():
        raise ValueError(f"OpenSpec change has no tasks.md: {resolved}")

    context_paths: list[Path] = []
    for artifact in (resolved / "proposal.md", resolved / "design.md"):
        if artifact.is_file():
            context_paths.append(artifact)
    specs_dir = resolved / "specs"
    if specs_dir.is_dir():
        context_paths.extend(sorted(path for path in specs_dir.rglob("*.md") if path.is_file()))

    return PlanSource(
        kind="openspec",
        source_path=resolved,
        checklist_path=checklist,
        context_paths=tuple(context_paths),
    )


def file_has_uncompleted_checkbox(path: Path) -> bool:
    fence = FenceTracker()
    for line in path.read_text(encoding="utf-8").splitlines():
        if fence.skip(line):
            continue
        match = CHECKBOX_RE.match(line)
        if not match:
            continue
        checkbox = Checkbox(text=match.group(2).strip(), checked=match.group(1).lower() == "x")
        if not checkbox.checked and checkbox.actionable:
            return True
    return False
