import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.validation import ValidationCommand, parse_validation_commands, run_validation, repository_state
from test_task_recovery import TaskRepositoryCase


class ValidationTest(unittest.TestCase):
    def test_structured_commands_have_explicit_defaults(self):
        command, = parse_validation_commands('[{"name":"tests","argv":["python3","-m","unittest"]}]')
        self.assertEqual(('python3', '-m', 'unittest'), command.argv)
        self.assertEqual(('review', 'finalize'), command.phases)
        self.assertEqual('.', command.cwd)
        self.assertEqual(300, command.timeout)

    def test_rejects_ambiguous_or_unsafe_configuration(self):
        values = [None, {}, ['python3'], [{'argv': 'python3 -m unittest'}],
                  [{'argv': []}], [{'argv': ['']}], [{'argv': ['x\0']}]]
        for key, value in [('cwd', '../elsewhere'), ('cwd', '/tmp'), ('cwd', '\0'),
                           ('timeout', 0), ('timeout', True), ('timeout', float('nan')),
                           ('timeout', float('inf')), ('phases', ['unknown']), ('name', '')]:
            values.append([{'argv': ['true'], key: value}])
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_validation_commands(json.dumps(value))

    def test_argv_is_executed_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            literal = '$(touch unexpected); echo unsafe'
            result = run_validation(ValidationCommand('literal', (sys.executable, '-c',
                                    'import sys; print(sys.argv[1])', literal)), root)
            self.assertEqual('passed', result['status'])
            self.assertIn(literal, result['output'])
            self.assertFalse((root / 'unexpected').exists())

    def test_exit_failure_timeout_and_missing_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for argv, timeout, status in [((sys.executable, '-c', 'raise SystemExit(7)'), 3, 'failed'),
                                          ((sys.executable, '-c', 'import time; time.sleep(20)'), .05, 'timed_out'),
                                          (('no-such-gigaflex-test-executable',), 3, 'failed')]:
                result = run_validation(ValidationCommand('check', argv, timeout=timeout), root)
                self.assertEqual(status, result['status'])
                self.assertLess(result['duration_seconds'], 3)

    def test_cwd_cannot_escape_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / 'outside').symlink_to(Path(tmp), target_is_directory=True)
            result = run_validation(ValidationCommand('check', ('true',), cwd='outside'), root)
            self.assertEqual('failed', result['status'])
            self.assertIn('outside', result['output'])


class RepositoryStateTest(TaskRepositoryCase):
    def test_same_dirty_path_content_and_index_are_distinct_states(self):
        target = self.repo / 'tracked.txt'
        target.write_text('dirty one\n')
        before = repository_state(self.git)
        target.write_text('dirty two\n')
        changed = repository_state(self.git)
        self.assertEqual(before.head, changed.head)
        self.assertNotEqual(before.tree, changed.tree)
        self.git.run('add', 'tracked.txt')
        staged = repository_state(self.git)
        self.assertEqual(changed.tree, staged.tree)
        self.assertNotEqual(changed.index, staged.index)
