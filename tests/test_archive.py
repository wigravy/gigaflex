from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.archive import archive_plan
from gigaflex.git import GitError, GitService, TaskWorktree
from gigaflex.validation import repository_state
from test_task_recovery import TaskRepositoryCase


class ArchiveTest(TaskRepositoryCase):
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
        original = GitService.commit_paths
        def injected(git, paths, message):
            result = original(git, paths, message)
            (git.cwd / 'injected.txt').write_text('unexpected change\n')
            git.run('add', 'injected.txt')
            git.run('commit', '-qm', 'unverified hook output')
            return result
        with patch.object(GitService, 'commit_paths', injected):
            with self.assertRaisesRegex(GitError, 'outside its bookkeeping boundary'):
                archive_plan(self.manager, self.plan, 'archive plan')
        self.assertTrue(self.plan.exists())
        self.assertFalse((self.repo / 'injected.txt').exists())
        self.assertEqual(self.completed_head, self.git.head_commit())

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
