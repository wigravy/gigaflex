from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.checkpoint import RunCheckpoint
from gigaflex.dashboard import ProgressDashboard
from gigaflex.executor import ExecResult
from gigaflex.git import GitError, GitService, ReviewWorktreeManager
from gigaflex.runner import RunOptions, Runner
from gigaflex.progress import ProgressLog
from gigaflex.signals import FINALIZE_DONE
from gigaflex.validation import ValidationCommand
from test_task_recovery import TaskRepositoryCase

FINDING = '''<FINDING>
severity: major
category: correctness
file: tracked.txt
line: 1
evidence: The delivered value is original instead of fixed.
impact: The result fails the required contract.
suggested_fix: Change the value to fixed.
</FINDING>'''


def decision(kind='fixed'):
    return ExecResult(output=f'''<SYNTHESIS_DECISION>
finding_id: F001
decision: {kind}
reason: Verified the value in tracked.txt against the requested contract.
</SYNTHESIS_DECISION>''', returncode=0)


def commit(root, text='fixed\n'):
    (root / 'tracked.txt').write_text(text)
    git = GitService(root)
    git.run('add', 'tracked.txt')
    git.run('commit', '-qm', 'fix value')


class Callback:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def run(self, prompt, *, cwd=None, retry_guard=None):
        self.calls.append((prompt, cwd))
        return self.callback(prompt, cwd, len(self.calls))


class PhaseVerificationTest(TaskRepositoryCase):
    def make_runner(self, synthesis=None, reviewer=None, finalizer=None, retries=1, checks=()):
        progress = self.repo / '.gigaflex/progress/phases.txt'
        clean = Callback(lambda *args: ExecResult(output='NO FINDINGS', returncode=0))
        finalizer = finalizer or Callback(lambda *args: ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE))
        return Runner(
            RunOptions(self.plan, progress, review_only=True, parallel_review=False,
                       default_branch=self.initial_head, task_completion_retries=retries,
                       review_iterations=2, delay_seconds=0, allow_dirty=True,
                       validation_commands=checks),
            clean, ProgressLog(progress), synthesis_executor=synthesis or clean,
            review_agent_executor=reviewer or clean, finalize_executor=finalizer,
            task_worktrees=self.manager,
            review_worktrees=ReviewWorktreeManager(self.git, temp_parent=self.root),
        )

    def finding_then_clean(self):
        return Callback(lambda p, cwd, n: ExecResult(output=FINDING if n == 1 else 'NO FINDINGS'))

    def test_dirty_synthesis_is_repaired_in_same_workspace_before_promotion(self):
        def synth(prompt, cwd, n):
            self.assertEqual(self.initial_head, self.git.head_commit())
            if n == 1:
                (cwd / 'tracked.txt').write_text('fixed\n')
            else:
                self.assertIn('uncommitted phase output', prompt)
                commit(cwd)
            return decision()
        synthesis = Callback(synth)
        reviewer = self.finding_then_clean()
        runner = self.make_runner(synthesis, reviewer)
        runner.run()
        self.assertEqual(2, len(synthesis.calls))
        self.assertEqual(synthesis.calls[0][1], synthesis.calls[1][1])
        self.assertEqual(2, len(reviewer.calls))
        self.assertEqual('fixed\n', (self.repo / 'tracked.txt').read_text())
        self.assertFalse(self.git.is_dirty())
        self.assertEqual(self.git.head_commit(), runner.verified_state.head)
        self.assertFalse((self.repo / '.git/gigaflex/recovery').exists())

    def test_exhausted_synthesis_keeps_dirty_result_and_main_unchanged(self):
        def synth(prompt, cwd, n):
            (cwd / 'valuable.txt').write_text('keep me\n')
            return decision()
        synthesis = Callback(synth)
        runner = self.make_runner(synthesis, self.finding_then_clean(), retries=0)
        with self.assertRaises(GitError) as raised:
            runner.run()
        recovery = raised.exception.task_recovery
        self.assertEqual('synthesis', recovery['phase'])
        self.assertEqual(self.initial_head, self.git.head_commit())
        restored, _, manifest = self.recover(Path(recovery['directory']))
        self.assertEqual('keep me\n', (restored / 'valuable.txt').read_text())
        self.assertEqual({'review': FINDING}, manifest['phase_context']['findings'])

    def test_all_rejected_ledger_with_code_changes_requires_fresh_review(self):
        def synth(prompt, cwd, n):
            commit(cwd)
            return decision('rejected')
        reviewer = self.finding_then_clean()
        self.make_runner(Callback(synth), reviewer).run()
        self.assertEqual(2, len(reviewer.calls))

    def test_unchanged_rejection_creates_no_commits_or_recovery(self):
        runner = self.make_runner(Callback(lambda *args: decision('rejected')), self.finding_then_clean())
        runner.run()
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertFalse((self.repo / '.git/gigaflex/recovery').exists())

    def test_synthesis_restores_protected_plan_before_corrective_attempt(self):
        original = self.plan.read_bytes()
        def synth(prompt, cwd, n):
            if n == 1:
                (cwd / 'plan.md').write_text('# Changed requirements\n')
                git = GitService(cwd)
                git.run('add', 'plan.md')
                git.run('commit', '-qm', 'rewrite plan')
            else:
                self.assertEqual(original, (cwd / 'plan.md').read_bytes())
                git = GitService(cwd)
                git.run('add', 'plan.md')
                git.run('commit', '-qm', 'restore plan')
                commit(cwd)
            return decision()
        self.make_runner(Callback(synth), self.finding_then_clean()).run()
        self.assertEqual(original, self.plan.read_bytes())

    def test_reconciliation_runs_in_the_same_candidate(self):
        def synth(prompt, cwd, n):
            if n == 1:
                commit(cwd)
                return ExecResult(output='I repaired the value.')
            return decision()
        synthesis = Callback(synth)
        self.make_runner(synthesis, self.finding_then_clean()).run()
        self.assertEqual(2, len(synthesis.calls))
        self.assertEqual(synthesis.calls[0][1], synthesis.calls[1][1])

    def test_finalize_commit_goes_through_review_and_another_finalize(self):
        events = []
        def finalize(prompt, cwd, n):
            events.append('finalize')
            self.assertIn('without changing HEAD', prompt)
            if n == 1:
                commit(cwd)
                self.assertEqual(self.initial_head, self.git.head_commit())
            return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE)
        def review(prompt, cwd, n):
            events.append('review')
            if n == 2:
                self.assertEqual('fixed\n', (cwd / 'tracked.txt').read_text())
            return ExecResult(output='NO FINDINGS')
        runner = self.make_runner(reviewer=Callback(review), finalizer=Callback(finalize))
        runner.run()
        self.assertEqual(['review', 'finalize', 'review', 'finalize'], events)
        self.assertEqual(self.git.head_commit(), runner.verified_state.head)

    def test_dirty_finalize_is_repaired_then_reviewed(self):
        def finalize(prompt, cwd, n):
            if n == 1:
                (cwd / 'tracked.txt').write_text('fixed\n')
            return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE)
        def repair(prompt, cwd, n):
            self.assertIn('repair the final verification candidate', prompt)
            commit(cwd)
            return ExecResult(output='repaired')
        finalizer, synthesis = Callback(finalize), Callback(repair)
        reviewer = Callback(lambda *args: ExecResult(output='NO FINDINGS'))
        self.make_runner(synthesis, reviewer, finalizer).run()
        self.assertEqual(2, len(finalizer.calls))
        self.assertEqual(2, len(reviewer.calls))
        self.assertEqual(finalizer.calls[0][1], synthesis.calls[0][1])

    def test_finalize_edits_with_no_budget_are_retained_without_promotion(self):
        def finalize(prompt, cwd, n):
            commit(cwd)
            return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE)
        with self.assertRaises(GitError) as raised:
            self.make_runner(finalizer=Callback(finalize), retries=0).run()
        self.assertEqual('finalize', raised.exception.task_recovery['phase'])
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_changes_to_already_dirty_input_after_review_are_detected(self):
        target = self.repo / 'tracked.txt'
        target.write_text('user input\n')
        runner = self.make_runner()
        runner.run_review()
        target.write_text('changed after review\n')
        with self.assertRaisesRegex(RuntimeError, 'state after review changed'):
            runner.run_finalize()
        self.assertEqual([], runner.finalize_executor.calls)

    def test_staging_only_change_after_review_is_detected(self):
        (self.repo / 'tracked.txt').write_text('input\n')
        runner = self.make_runner()
        runner.run_review()
        self.git.run('add', 'tracked.txt')
        with self.assertRaisesRegex(RuntimeError, 'staged state'):
            runner.run_finalize()

    def test_review_cannot_approve_its_own_modified_snapshot(self):
        def review(prompt, cwd, n):
            commit(cwd)
            return ExecResult(output='NO FINDINGS')
        with self.assertRaisesRegex(RuntimeError, 'read-only review modified'):
            self.make_runner(reviewer=Callback(review)).run()
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_runner_checks_override_clean_agent_report_and_bind_actual_result(self):
        command = ValidationCommand('value', (sys.executable, '-c',
            'from pathlib import Path; assert Path("tracked.txt").read_text() == "fixed\\n"'))
        def repair(prompt, cwd, n):
            self.assertIn('Runner-owned validation failed', prompt)
            commit(cwd)
            return decision()
        reviewer = Callback(lambda *args: ExecResult(output='NO FINDINGS'))
        runner = self.make_runner(Callback(repair), reviewer, checks=(command,))
        dashboard = ProgressDashboard(self.repo / '.gigaflex/status.json', self.repo / '.gigaflex/status.html',
                                      name='checks', plan_file=self.plan)
        runner.dashboard = dashboard
        runner.run()
        self.assertEqual(2, len(reviewer.calls))
        self.assertEqual('passed', dashboard.state['validation']['status'])
        self.assertEqual(runner.verified_state.head, dashboard.state['verification']['repository']['head'])
        self.assertIn('Runner checks', dashboard.html_path.read_text())
        self.assertEqual('fixed\n', (self.repo / 'tracked.txt').read_text())

    def test_validation_that_modifies_deliverables_cannot_pass(self):
        command = ValidationCommand('mutator', (sys.executable, '-c',
            'from pathlib import Path; Path("tracked.txt").write_text("mutation")'))
        runner = self.make_runner(checks=(command,))
        errors = runner._run_checks('review')
        self.assertIn('modified repository', errors[0])
        self.assertEqual('original\n', (self.repo / 'tracked.txt').read_text())

    def test_synthesis_preserves_unrelated_staged_and_unstaged_input(self):
        (self.repo / 'deleted.txt').write_text('user staged\n')
        self.git.run('add', 'deleted.txt')
        (self.repo / 'deleted.txt').write_text('user unstaged\n')
        (self.repo / 'tracked.txt').write_text('dirty touched input\n')
        def synth(prompt, cwd, n):
            self.assertEqual('dirty touched input\n', (cwd / 'tracked.txt').read_text())
            commit(cwd)
            return decision()
        self.make_runner(Callback(synth), self.finding_then_clean()).run()
        self.assertEqual('user staged\n', self.git.run('show', ':deleted.txt').stdout)
        self.assertEqual('user unstaged\n', (self.repo / 'deleted.txt').read_text())
        self.assertEqual('fixed\n', self.git.run('show', 'HEAD:tracked.txt').stdout)

    def test_synthesis_resume_restores_saved_candidate_and_repeats_review(self):
        def fail(prompt, cwd, n):
            (cwd / 'valuable.txt').write_text('saved result\n')
            return decision()
        first = self.make_runner(Callback(fail), self.finding_then_clean(), retries=0)
        with self.assertRaises(GitError) as raised:
            first.run()
        packet = raised.exception.task_recovery
        checkpoint = RunCheckpoint(self.repo / '.gigaflex/checkpoint.json', self.git,
                                   identity='phases', base_commit=self.initial_head)
        checkpoint.mark_blocked('unfinished synthesis', 'review', 'gigaflex --resume', packet)
        def resume(prompt, cwd, n):
            self.assertEqual('saved result\n', (cwd / 'valuable.txt').read_text())
            git = GitService(cwd)
            git.run('add', 'valuable.txt')
            git.run('commit', '-qm', 'retain result')
            return decision()
        runner = self.make_runner(Callback(resume))
        runner.checkpoint = checkpoint
        runner.options.resume = True
        runner.run()
        self.assertEqual('saved result\n', (self.repo / 'valuable.txt').read_text())
        self.assertFalse(checkpoint.blocked)
        self.assertEqual(1, len(runner.review_agent_executor.calls))

    def test_task_phase_check_can_repair_after_checklist_completion(self):
        def task(prompt, cwd, n):
            if n == 1:
                (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                git = GitService(cwd)
                git.run('add', 'plan.md')
                git.run('commit', '-qm', 'checklist')
            else:
                self.assertIn('value:', prompt)
                commit(cwd)
            return ExecResult(output='done')
        executor = Callback(task)
        runner = self.runner(executor)
        runner.options.validation_commands = (ValidationCommand('value', (sys.executable, '-c',
            'from pathlib import Path; assert Path("tracked.txt").read_text() == "fixed\\n"'), phases=('task',)),)
        runner.run_tasks()
        self.assertEqual(2, len(executor.calls))
        self.assertEqual('fixed\n', (self.repo / 'tracked.txt').read_text())
