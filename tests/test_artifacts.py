from pathlib import Path
import os
from unittest.mock import patch

from test_task_recovery import TaskRepositoryCase
from gigaflex.artifacts import ArtifactSnapshot
from gigaflex.checkpoint import ResumeError, RunCheckpoint
from gigaflex.executor import ExecResult
from gigaflex.git import GitError, GitService, TaskWorktreeManager, ReviewWorktreeManager
from gigaflex.validation import repository_state


class TaskArtifactTest(TaskRepositoryCase):
    def setUp(self):
        super().setUp()
        (self.repo / '.gitignore').write_text(
            '/.gigaflex/\nbuild/\ngraphify-out/\ndomain-out/\ndomains.json\n.env\n'
        )
        self.git.run('add', '.gitignore')
        self.git.run('commit', '-qm', 'ignore skill artifacts')
        self.initial_head = self.git.head_commit()
        self.manager.artifact_paths = (Path('graphify-out'), Path('domain-out'), Path('domains.json'))

    def commit_task(self, workspace):
        task_git = GitService(workspace.path)
        (workspace.path / 'tracked.txt').write_text('task result\n')
        task_git.run('add', 'tracked.txt')
        task_git.run('commit', '-qm', 'complete task')
        return task_git.head_commit()

    def test_runner_passes_ignored_skill_outputs_to_the_next_task_without_committing_them(self):
        self.plan.write_text(
            '# Plan\n### Task 1: Graph\n- [ ] Build graph\n'
            '### Task 2: Domains\n- [ ] Use graph\n'
        )
        self.git.run('add', 'plan.md')
        self.git.run('commit', '-qm', 'two tasks')
        calls = []
        testcase = self

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                if len(calls) == 1:
                    (cwd / 'graphify-out').mkdir()
                    (cwd / 'graphify-out/graph data.json').write_bytes(b'graph\x00data')
                    (cwd / 'domains.json').write_text('["example"]')
                    (cwd / '.env').write_text('unselected')
                    (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]', 1))
                else:
                    testcase.assertEqual(b'graph\x00data', (cwd / 'graphify-out/graph data.json').read_bytes())
                    testcase.assertEqual('["example"]', (cwd / 'domains.json').read_text())
                    testcase.assertFalse((cwd / '.env').exists())
                    (cwd / 'domain-out').mkdir()
                    (cwd / 'domain-out/result.json').write_text('complete')
                    (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                git = GitService(cwd)
                git.run('add', '.')
                git.run('commit', '-qm', 'complete phase')
                return ExecResult(output='done', returncode=0)

        self.runner(Executor()).run_tasks()
        self.assertEqual(2, len(calls))
        self.assertNotEqual(calls[0], calls[1])
        self.assertTrue(all(not path.exists() for path in calls))
        self.assertEqual('complete', (self.repo / 'domain-out/result.json').read_text())
        self.assertFalse((self.repo / '.env').exists())
        self.assertEqual('', self.git.run('status', '--short').stdout)
        for ref in ('HEAD', 'HEAD~1'):
            tracked = self.git.run('ls-tree', '-r', '--name-only', ref).stdout
            self.assertNotIn('graphify-out', tracked)
            self.assertNotIn('domains.json', tracked)
            self.assertNotIn('domain-out', tracked)

    def test_copies_inputs_and_promotes_edits_deletions_modes_and_symlinks(self):
        (self.repo / 'graphify-out').mkdir()
        (self.repo / 'graphify-out/obsolete').write_text('old')
        (self.repo / 'domains.json').write_text('before')
        with self.manager.create('task') as workspace:
            self.assertEqual('before', (workspace.path / 'domains.json').read_text())
            self.assertNotIn('domains.json', GitService(workspace.path).run('ls-files').stdout)
            (workspace.path / 'domains.json').write_text('after')
            (workspace.path / 'graphify-out/obsolete').unlink()
            script = workspace.path / 'graphify-out/run'
            script.write_text('run')
            script.chmod(0o755)
            (workspace.path / 'graphify-out/link').symlink_to('run')
            workspace.promote(self.commit_task(workspace))
        self.assertFalse((self.repo / 'graphify-out/obsolete').exists())
        self.assertEqual('after', (self.repo / 'domains.json').read_text())
        self.assertEqual(0o755, (self.repo / 'graphify-out/run').stat().st_mode & 0o777)
        self.assertEqual('run', os.readlink(self.repo / 'graphify-out/link'))
        self.assertEqual('', self.git.run('status', '--short').stdout)

    def test_rejects_concurrent_main_artifact_changes(self):
        (self.repo / 'domains.json').write_text('before')
        with self.manager.create('task') as workspace:
            (workspace.path / 'domains.json').write_text('task')
            head = self.commit_task(workspace)
            (self.repo / 'domains.json').write_text('concurrent')
            with self.assertRaisesRegex(GitError, 'main task artifacts changed'):
                workspace.promote(head)
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertEqual('concurrent', (self.repo / 'domains.json').read_text())

    def test_failed_artifact_install_rolls_back_files_head_and_index(self):
        (self.repo / 'domains.json').write_text('before')
        (self.repo / 'deleted.txt').write_text('user staged change')
        self.git.run('add', 'deleted.txt')
        index = self.git.run('write-tree').stdout
        status = self.git.run('status', '--short').stdout
        with self.manager.create('task') as workspace:
            (workspace.path / 'domains.json').write_text('after')
            (workspace.path / 'graphify-out').mkdir()
            (workspace.path / 'graphify-out/new').write_text('new')
            head = self.commit_task(workspace)
            install = ArtifactSnapshot.install
            failed = False

            def fail_after_install(snapshot, root, paths):
                nonlocal failed
                install(snapshot, root, paths)
                if root == workspace.repo_root and not failed:
                    failed = True
                    raise OSError('injected artifact copy failure')

            with patch.object(ArtifactSnapshot, 'install', fail_after_install):
                with self.assertRaisesRegex(OSError, 'injected artifact copy failure'):
                    workspace.promote(head)
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertEqual('before', (self.repo / 'domains.json').read_text())
        self.assertFalse((self.repo / 'graphify-out/new').exists())
        self.assertEqual('original\n', (self.repo / 'tracked.txt').read_text())
        self.assertEqual(index, self.git.run('write-tree').stdout)
        self.assertEqual(status, self.git.run('status', '--short').stdout)

    def test_artifacts_survive_failed_task_recovery_and_resume(self):
        (self.repo / 'domains.json').write_text('before')
        (self.repo / 'graphify-out').mkdir()
        (self.repo / 'graphify-out/link').symlink_to('../domains.json')
        with self.assertRaises(GitError) as caught:
            with self.manager.create('task') as workspace:
                (workspace.path / 'domains.json').write_text('partial')
                (workspace.path / 'domain-out').mkdir()
                (workspace.path / 'domain-out/result').write_text('saved')
                self.commit_task(workspace)
                raise RuntimeError('task interrupted')
        self.assertFalse(workspace.path.exists())
        self.assertEqual('before', (self.repo / 'domains.json').read_text())
        with self.manager.resume('task', caught.exception.task_recovery) as restored:
            self.assertEqual('partial', (restored.path / 'domains.json').read_text())
            self.assertEqual('saved', (restored.path / 'domain-out/result').read_text())
            self.assertEqual('../domains.json', os.readlink(restored.path / 'graphify-out/link'))
            self.assertEqual('', GitService(restored.path).run('status', '--short').stdout)
            restored.promote(GitService(restored.path).head_commit())
        self.assertEqual('partial', (self.repo / 'domains.json').read_text())
        self.assertEqual('saved', (self.repo / 'domain-out/result').read_text())

    def test_review_copies_are_independent_and_artifact_changes_invalidate_verification(self):
        (self.repo / 'domains.json').write_text('original')
        manager = ReviewWorktreeManager(self.git, temp_parent=self.root, artifact_paths=self.manager.artifact_paths)
        with manager.create(['first', 'second']) as worktrees:
            first, second = worktrees.paths.values()
            git = GitService(first)
            before = repository_state(git, artifact_paths=self.manager.artifact_paths)
            self.assertEqual('original', (first / 'domains.json').read_text())
            (first / 'domains.json').write_text('review edit')
            self.assertEqual('original', (second / 'domains.json').read_text())
            self.assertNotEqual(before, repository_state(git, artifact_paths=self.manager.artifact_paths))
        self.assertEqual('original', (self.repo / 'domains.json').read_text())

    def test_checkpoint_cannot_reuse_checks_after_artifact_edits_or_deletion(self):
        artifact = self.repo / 'domains.json'
        artifact.write_text('before')
        checkpoint = RunCheckpoint(
            self.repo / '.gigaflex/checkpoint.json', self.git,
            identity='plan:test', base_commit=self.initial_head, artifact_paths=self.manager.artifact_paths,
        )
        checkpoint.mark_completed('review', checkpoint.current_state())
        self.assertTrue(checkpoint.can_reuse('review', checkpoint.current_state()))
        artifact.write_text('after')
        self.assertFalse(checkpoint.can_reuse('review', checkpoint.current_state()))
        artifact.unlink()
        self.assertFalse(checkpoint.can_reuse('review', checkpoint.current_state()))

    def test_artifact_file_directory_transitions(self):
        folder = self.repo / 'graphify-out'
        folder.mkdir()
        (folder / 'old-file').write_text('file')
        (folder / 'old-dir').mkdir()
        (folder / 'old-dir/child').write_text('child')
        with self.manager.create('task') as workspace:
            task_folder = workspace.path / 'graphify-out'
            (task_folder / 'old-file').unlink()
            (task_folder / 'old-file').mkdir()
            (task_folder / 'old-file/child').write_text('new child')
            (task_folder / 'old-dir/child').unlink()
            (task_folder / 'old-dir').rmdir()
            (task_folder / 'old-dir').write_text('new file')
            workspace.promote(self.commit_task(workspace))
        self.assertEqual('new child', (folder / 'old-file/child').read_text())
        self.assertEqual('new file', (folder / 'old-dir').read_text())

    def test_artifact_selection_is_literal_and_excludes_orchestration_files(self):
        folder = self.repo / 'graphify-out/[literal]'
        folder.mkdir(parents=True)
        (folder / 'data').write_text('data')
        (folder / 'progress').write_text('runtime')
        (self.repo / 'graphify-out/l').write_text('unselected')
        self.manager.artifact_paths = (Path('graphify-out/[literal]'),)
        self.manager.ignored_paths = (Path('graphify-out/[literal]/progress'),)
        with self.manager.create('task') as workspace:
            self.assertEqual('data', (workspace.path / 'graphify-out/[literal]/data').read_text())
            self.assertFalse((workspace.path / 'graphify-out/[literal]/progress').exists())
            self.assertFalse((workspace.path / 'graphify-out/l').exists())
            workspace.promote(self.commit_task(workspace))

    def test_resume_rejects_changed_artifact_selection(self):
        with self.assertRaises(GitError) as caught:
            with self.manager.create('task'):
                raise RuntimeError('interrupted')
        self.manager.artifact_paths = ()
        with self.assertRaisesRegex(ResumeError, 'task_artifact_paths changed'):
            with self.manager.resume('task', caught.exception.task_recovery):
                self.fail('must not resume with different artifact inputs')

    def test_ignored_files_remain_excluded_by_default(self):
        self.manager.artifact_paths = ()
        (self.repo / 'domains.json').write_text('main only')
        with self.manager.create('task') as workspace:
            self.assertFalse((workspace.path / 'domains.json').exists())
            (workspace.path / 'domains.json').write_text('task only')
            workspace.promote(self.commit_task(workspace))
        self.assertEqual('main only', (self.repo / 'domains.json').read_text())

    def test_artifact_only_repair_does_not_require_an_empty_commit(self):
        with self.manager.create('repair') as workspace:
            (workspace.path / 'domains.json').write_text('repaired')
            self.assertEqual([], workspace.promote(GitService(workspace.path).head_commit()))
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertEqual('repaired', (self.repo / 'domains.json').read_text())
        self.assertEqual('', self.git.run('status', '--short').stdout)

    def test_a_committed_ignored_file_uses_the_normal_promotion_path(self):
        self.manager.artifact_paths = ()
        with self.manager.create('task') as workspace:
            (workspace.path / 'domains.json').write_text('force added')
            task_git = GitService(workspace.path)
            task_git.run('add', '-f', 'domains.json')
            task_git.run('commit', '-qm', 'tracked ignored file')
            workspace.promote(task_git.head_commit())
        self.assertEqual('force added', (self.repo / 'domains.json').read_text())
        self.assertEqual('force added', self.git.run('show', 'HEAD:domains.json').stdout)

    def test_artifact_paths_cannot_escape_the_repository_or_select_git_metadata(self):
        for value in ('.', '..', '../outside', '/tmp/outside', '.git', 'nested/.git/index'):
            with self.subTest(path=value), self.assertRaises(ValueError):
                TaskWorktreeManager(self.git, artifact_paths=(Path(value),))
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'result').write_text('outside')
        (self.repo / 'graphify-out').symlink_to(outside, target_is_directory=True)
        self.manager.artifact_paths = (Path('graphify-out/result'),)
        with self.assertRaisesRegex(ValueError, 'parent is a symlink'):
            with self.manager.create('task'):
                self.fail('must not follow an artifact parent symlink')
        self.assertEqual('outside', (outside / 'result').read_text())
