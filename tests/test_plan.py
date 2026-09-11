from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.plan import (
    file_has_uncompleted_checkbox,
    parse_plan,
    resolve_openspec_change,
    task_plan_update_allowed,
)


class PlanParserTest(unittest.TestCase):
    def test_task_updates_protect_everything_except_selected_tracking(self) -> None:
        original = (
            "# Plan\n## Context\nKeep the contract.\n"
            "### Task 1: Previous\n- [x] Finished\nPreserve earlier prose.\n"
            "### Task 2: Selected\n- [x] Already done\n- [ ] Implement\n- [ ] Validate\n"
            "```md\n- [ ] Example\n```\n"
            "- [ ] Explain the [ ] format\n"
            "### Task 3: Later\n- [ ] Follow up\nPreserve later prose.\n"
            "## Validation\npython3 verify.py\n"
        )
        selected = parse_plan(original).first_uncompleted_task()
        completed = original.replace('- [ ] Implement', '- [x] Implement').replace(
            '- [ ] Validate', '- [X] Validate',
        )
        self.assertTrue(task_plan_update_allowed(original, completed, selected))
        changes = (
            ('- [X] Validate\n', ''),
            ('- [x] Implement', '- [x] Implement something easier'),
            ('- [x] Already done', '- [ ] Already done'),
            ('Keep the contract.', 'Changed contract.'),
            ('Preserve earlier prose.', 'Changed earlier prose.'),
            ('Preserve later prose.', 'Changed later prose.'),
            ('- [ ] Follow up', '- [x] Follow up'),
            ('- [ ] Example', '- [x] Example'),
            ('- [ ] Explain the [ ] format', '- [x] Explain the [ ] format'),
            ('python3 verify.py', 'true'),
            ('### Task 2: Selected', '### Task 2: Renamed'),
        )
        for old, new in changes:
            with self.subTest(change=(old, new)):
                self.assertFalse(task_plan_update_allowed(original, completed.replace(old, new), selected))

    def test_prose_tracking_allows_only_the_exact_marker_after_its_heading(self) -> None:
        original = '## Задача 1: Анализ\n\nПроверить источники.\n## Задача 2: Отчёт\nПодготовить отчёт.\n'
        selected = parse_plan(original, plan_format='openspec').first_uncompleted_task()
        marker = '- [x] 1. Анализ\n'
        valid = original.replace('## Задача 1: Анализ\n', '## Задача 1: Анализ\n' + marker)
        self.assertTrue(task_plan_update_allowed(original, valid, selected, plan_format='openspec'))
        for invalid in (
            valid.replace('Проверить источники.', 'Пропустить источники.'),
            valid.replace(marker, '- [x] Любая отметка\n'),
            valid.replace(marker, marker + marker),
            original + marker,
        ):
            with self.subTest(plan=invalid):
                self.assertFalse(task_plan_update_allowed(original, invalid, selected, plan_format='openspec'))

    def test_tracking_boundary_uses_source_lines_when_title_is_inside_task(self) -> None:
        original = '### Task 1: Build\n# Plan\n- [ ] Implement\n- [ ] Validate\n'
        selected = parse_plan(original).first_uncompleted_task()
        self.assertTrue(task_plan_update_allowed(original, original.replace('[ ]', '[x]'), selected))
        self.assertFalse(task_plan_update_allowed(original, original.replace('# Plan', '# Changed'), selected))

    def test_parses_tasks_and_ignores_fenced_checkboxes(self) -> None:
        plan = parse_plan(
            """# Plan: Demo

```md
- [ ] ignored
```

### Task 1: Build
- [x] done
- [ ] todo

## Notes
- [ ] not task

### Iteration 2: Verify
- [X] checked
"""
        )

        self.assertEqual("Plan: Demo", plan.title)
        self.assertEqual(2, len(plan.tasks))
        self.assertEqual(1, plan.first_uncompleted_task_index())
        self.assertTrue(plan.has_uncompleted_tasks())

    def test_file_has_uncompleted_checkbox_ignores_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.md"
            path.write_text("- [ ] literal format [ ] example\n- [x] done\n", encoding="utf-8")
            self.assertFalse(file_has_uncompleted_checkbox(path))

    def test_parses_fully_localized_russian_plan(self) -> None:
        plan = parse_plan(
            """# План: Демо

## Обзор
Добавить новую возможность.

## Контекст
Сначала изучить существующую реализацию.

### Задача 1: Реализация
- [x] Готовый шаг
- [ ] Добавить код

### Итерация №2: Проверка
- [ ] Запустить тесты

## Проверка
- выполнить полный набор тестов
"""
        )

        self.assertEqual("План: Демо", plan.title)
        self.assertEqual([1, 2], [task.number for task in plan.tasks])
        self.assertEqual(["Реализация", "Проверка"], [task.title for task in plan.tasks])
        self.assertEqual(1, plan.first_uncompleted_task_index())

    def test_russian_task_header_is_case_insensitive(self) -> None:
        plan = parse_plan("### задача 1: Сделать\n- [ ] Шаг\n")

        self.assertEqual(1, len(plan.tasks))
        self.assertEqual(1, plan.tasks[0].number)

    def test_preserves_complete_task_section_text(self) -> None:
        plan = parse_plan(
            """# Plan: Demo

### Task 7: Build parser
Explain the approach.
- [ ] Implement it
```text
example
```

### Task 8: Follow-up
- [ ] Later work
"""
        )

        self.assertEqual(
            """### Task 7: Build parser
Explain the approach.
- [ ] Implement it
```text
example
```""",
            plan.tasks[0].section,
        )
        self.assertEqual(plan.tasks[0], plan.first_uncompleted_task())
        self.assertEqual([plan.tasks[0]], plan.tasks_matching(7, "Build parser"))

    def test_parses_superpowers_plan_with_h2_tasks_and_step_checkboxes(self) -> None:
        plan = parse_plan(
            """# Demo Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Add the demo feature.

## Task 1: Build the feature

**Files:**
- Modify: `src/demo.py`
- Test: `tests/test_demo.py`

- [x] **Step 1: Write the failing test**
- [ ] **Step 2: Write minimal implementation**

```markdown
## Task 99: This is example content
- [ ] Ignore this checkbox
```

## Task 2: Verify the feature

- [ ] **Step 1: Run the focused tests**
"""
        )

        self.assertEqual("Demo Feature Implementation Plan", plan.title)
        self.assertEqual([1, 2], [task.number for task in plan.tasks])
        self.assertEqual(
            ["Build the feature", "Verify the feature"],
            [task.title for task in plan.tasks],
        )
        self.assertEqual(2, len(plan.tasks[0].checkboxes))
        self.assertIn("**Files:**", plan.tasks[0].section)
        self.assertIn("## Task 99: This is example content", plan.tasks[0].section)
        self.assertEqual(plan.tasks[0], plan.first_uncompleted_task())

    def test_parses_openspec_numbered_task_groups_only_in_openspec_mode(self) -> None:
        content = """## 1. Setup
- [x] 1.1 Create module
- [ ] 1.2 Add configuration

```markdown
## 99. Example only
- [ ] 99.1 Ignore this
```

## 2. Verification
- [ ] 2.1 Run tests
"""

        plan = parse_plan(content, plan_format="openspec")

        self.assertEqual([1, 2], [task.number for task in plan.tasks])
        self.assertEqual(["Setup", "Verification"], [task.title for task in plan.tasks])
        self.assertEqual(2, len(plan.tasks[0].checkboxes))
        self.assertEqual(plan.tasks[0], plan.first_uncompleted_task())
        self.assertEqual([], parse_plan(content).tasks)

    def test_parses_localized_openspec_prose_tasks_as_pending_work(self) -> None:
        content = """# Задачи: изменение проверки

## Задача 1: Добавить метод

**Файл:** `Adapter.java`

Добавить новый метод интерфейса.

## Задача 2: Написать тесты

Создать unit-тесты.
"""

        plan = parse_plan(content, plan_format="openspec")

        self.assertEqual([1, 2], [task.number for task in plan.tasks])
        self.assertEqual(["Добавить метод", "Написать тесты"], [task.title for task in plan.tasks])
        self.assertTrue(plan.tasks[0].has_implicit_tracking)
        self.assertTrue(plan.tasks[1].has_implicit_tracking)
        self.assertEqual(plan.tasks[0], plan.first_uncompleted_task())
        self.assertIn("**Файл:** `Adapter.java`", plan.tasks[0].section)

    def test_explicit_completion_marker_completes_openspec_prose_task(self) -> None:
        plan = parse_plan(
            "## Задача 1: Добавить метод\n- [x] 1. Добавить метод\n",
            plan_format="openspec",
        )

        self.assertFalse(plan.tasks[0].has_implicit_tracking)
        self.assertTrue(plan.tasks[0].complete)
        self.assertIsNone(plan.first_uncompleted_task())

    def test_resolves_openspec_change_and_collects_context_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            change = Path(tmp) / "openspec/changes/add-search"
            specs = change / "specs/search"
            specs.mkdir(parents=True)
            (change / "tasks.md").write_text("## 1. Build\n- [ ] 1.1 Implement\n", encoding="utf-8")
            (change / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
            (change / "design.md").write_text("# Design\n", encoding="utf-8")
            (specs / "spec.md").write_text("## ADDED Requirements\n", encoding="utf-8")

            source = resolve_openspec_change(change)

            self.assertTrue(source.is_openspec)
            self.assertEqual(change.resolve(), source.source_path)
            self.assertEqual((change / "tasks.md").resolve(), source.checklist_path)
            self.assertEqual(
                (
                    (change / "proposal.md").resolve(),
                    (change / "design.md").resolve(),
                    (specs / "spec.md").resolve(),
                ),
                source.context_paths,
            )

    def test_rejects_openspec_change_without_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            change = Path(tmp) / "openspec/changes/incomplete"
            change.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "has no tasks.md"):
                resolve_openspec_change(change)


if __name__ == "__main__":
    unittest.main()
