from pathlib import Path
import json
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.archive import archive_plan
from gigaflex.git import GitError, TaskWorktree, move_plan_to_completed
from gigaflex.validation import repository_state
from test_task_recovery import TaskRepositoryCase


class ArchiveTest(TaskRepositoryCase):
    def hook(self, name, body):
        path = self.repo / '.git/hooks' / name
        path.write_text(f'#!{sys.executable}\n' + body)
        path.chmod(0o755)

    def setUp(self):
        super().setUp()
        self.plan.write_text(self.plan.read_text().replace('[ ]', '[x]'))
        self.git.run('add', 'plan.md')
        self.git.run('commit', '-qm', 'completed tasks')
        self.completed_head = self.git.head_commit()
        self.contents = self.plan.read_bytes()

    def test_archive_is_plan_only_and_preserves_dirty_input_and_staging(self):
        (self.repo / 'tracked.txt').write_text('staged\n')
        self.git.run('add', 'tracked.txt')
        (self.repo / 'tracked.txt').write_text('unstaged\n')
        verified = repository_state(self.git)
        target = archive_plan(self.manager, self.plan, 'archive plan', expected=verified)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.contents, target.read_bytes())
        self.assertEqual({Path('plan.md'), Path('completed/plan.md')},
                         self.git.changed_paths_between(self.completed_head, 'HEAD'))
        self.assertEqual('staged\n', self.git.run('show', ':tracked.txt').stdout)
        self.assertEqual('unstaged\n', (self.repo / 'tracked.txt').read_text())
        self.assertFalse((self.repo / '.git/gigaflex/recovery').exists())

    def test_archive_rejects_incomplete_plan(self):
        self.plan.write_text(self.plan.read_text().replace('[x]', '[ ]'))
        with self.assertRaisesRegex(GitError, 'incomplete tasks'):
            archive_plan(self.manager, self.plan, 'archive plan')
        self.assertTrue(self.plan.exists())
        self.assertEqual(self.completed_head, self.git.head_commit())

    def test_extra_commit_from_hook_is_not_promoted(self):
        self.hook('post-commit', '''from pathlib import Path
import subprocess
Path('injected.txt').write_text('unexpected change\\n')
subprocess.run(['git', 'add', 'injected.txt'], check=True)
subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'hook output'], check=True)
''')
        with self.assertRaisesRegex(GitError, 'outside its bookkeeping boundary'):
            archive_plan(self.manager, self.plan, 'archive plan')
        self.assertTrue(self.plan.exists())
        self.assertFalse((self.repo / 'injected.txt').exists())
        self.assertEqual(self.completed_head, self.git.head_commit())

    def test_archive_resumes_staged_rename_after_commit_hook_failure(self):
        self.hook('pre-commit', 'raise SystemExit(1)\n')
        with self.assertRaises(GitError) as raised:
            archive_plan(self.manager, self.plan, 'archive plan')
        recovery = raised.exception.task_recovery
        self.hook('pre-commit', 'raise SystemExit(0)\n')
        target = archive_plan(self.manager, self.plan, 'archive plan', recovery=recovery)
        self.assertEqual(self.contents, target.read_bytes())
        self.assertFalse(self.plan.exists())
        self.assertFalse(self.git.is_dirty())

    def test_resumed_commit_hook_cannot_replace_verified_plan(self):
        self.hook('pre-commit', 'raise SystemExit(1)\n')
        with self.assertRaises(GitError) as raised:
            archive_plan(self.manager, self.plan, 'archive plan')
        recovery = raised.exception.task_recovery
        self.hook('pre-commit', 'raise SystemExit(0)\n')
        self.hook('post-commit', '''from pathlib import Path
import subprocess
Path('completed/plan.md').write_text('# Changed plan\\n### Task 1: Build\\n- [ ] Pending\\n')
subprocess.run(['git', 'add', 'completed/plan.md'], check=True)
subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'commit', '--amend', '--no-edit'], check=True)
''')
        with self.assertRaisesRegex(GitError, 'preserve the verified plan') as rejected:
            archive_plan(self.manager, self.plan, 'archive plan', recovery=recovery)
        self.assertEqual(self.completed_head, self.git.head_commit())
        self.assertEqual(self.contents, self.plan.read_bytes())
        restored, _, _ = self.recover(Path(rejected.exception.task_recovery['directory']))
        self.assertIn('[ ] Pending', (restored / 'completed/plan.md').read_text())

    def test_archive_resumes_if_move_fails_before_rename(self):
        self.assert_move_failure_resumes(after_move=False)

    def test_archive_resumes_if_move_fails_after_rename(self):
        self.assert_move_failure_resumes(after_move=True)

    def assert_move_failure_resumes(self, *, after_move):
        def fail(plan, *, target):
            if after_move:
                move_plan_to_completed(plan, target=target)
            raise OSError('temporary move failure')
        with patch('gigaflex.archive.move_plan_to_completed', side_effect=fail):
            with self.assertRaises(GitError) as raised:
                archive_plan(self.manager, self.plan, 'archive plan')
        recovery = raised.exception.task_recovery
        self.assertEqual('plan.md', recovery['phase_context']['source'])
        self.assertEqual('completed/plan.md', recovery['phase_context']['target'])
        target = archive_plan(self.manager, self.plan, 'archive plan', recovery=recovery)
        self.assertEqual(self.contents, target.read_bytes())

    def test_legacy_early_move_packet_without_context_can_resume(self):
        with patch('gigaflex.archive.move_plan_to_completed', side_effect=OSError('move failed')):
            with self.assertRaises(GitError) as raised:
                archive_plan(self.manager, self.plan, 'archive plan')
        recovery = raised.exception.task_recovery
        manifest_path = Path(recovery['directory']) / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['phase_context'] = {}
        manifest_path.write_text(json.dumps(manifest))
        recovery['phase_context'] = {}
        target = archive_plan(self.manager, self.plan, 'archive plan', recovery=recovery)
        self.assertEqual(self.contents, target.read_bytes())

    def test_archive_can_resume_after_failed_promotion(self):
        with patch.object(TaskWorktree, 'promote', side_effect=GitError('promotion unavailable')):
            with self.assertRaises(GitError) as raised:
                archive_plan(self.manager, self.plan, 'archive plan')
        recovery = raised.exception.task_recovery
        self.assertEqual('archive', recovery['phase'])
        target = archive_plan(self.manager, self.plan, 'archive plan', recovery=recovery)
        self.assertEqual(self.contents, target.read_bytes())
        self.assertFalse(self.plan.exists())
        self.assertEqual({Path('plan.md'), Path('completed/plan.md')},
                         self.git.changed_paths_between(self.completed_head, 'HEAD'))

    def test_changes_after_verification_prevent_archival(self):
        expected = repository_state(self.git)
        (self.repo / 'tracked.txt').write_text('changed later\n')
        with self.assertRaisesRegex(GitError, 'changed after verification'):
            archive_plan(self.manager, self.plan, 'archive plan', expected=expected)
        self.assertTrue(self.plan.exists())
        self.assertEqual(self.completed_head, self.git.head_commit())
        self.assertFalse((self.repo / '.git/gigaflex/recovery').exists())
