"""Diagnostic scenarios for the 2026-09-11 audit at revision 6af4eb6.

Uses temporary Git repositories and scripted executors; makes no model calls.
The JSON reports observed behavior. Exit zero means the diagnostics ran,
not that the runner passed the proposed acceptance criteria.
"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import json
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))
from gigaflex.executor import ExecResult
from gigaflex.git import GitService, TaskWorktreeManager, ReviewWorktreeManager
from gigaflex.progress import ProgressLog
from gigaflex.runner import Runner, RunOptions
from gigaflex.checkpoint import RunCheckpoint
from gigaflex.prompts import DEFAULT_PROMPTS
from gigaflex.signals import FINALIZE_DONE

FINDING = '''<FINDING>
severity: major
category: correctness
file: result.txt
line: 1
evidence: Result contains the old value.
impact: The output is incorrect.
suggested_fix: Replace the old value with the expected value.
</FINDING>'''

@contextmanager
def repository(plan_text):
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='gigaflex-audit-') as tmp:
        root = Path(tmp)
        repo = root / 'repo'
        repo.mkdir()
        os.chdir(repo)
        git = GitService(repo)
        git.run('init', '-q')
        git.run('config', 'user.email', 'audit@example.invalid')
        git.run('config', 'user.name', 'Audit')
        (repo / '.gitignore').write_text('/.gigaflex/\n')
        (repo / 'plan.md').write_text(plan_text)
        (repo / 'result.txt').write_text('old\n')
        git.run('add', '.')
        git.run('commit', '-qm', 'initial')
        try:
            yield repo, git
        finally:
            os.chdir(previous)

def task_case(mode):
    plan_text = '# Plan\n### Task 1: produce result\n- [ ] implement output\n- [ ] validate output\n'
    with repository(plan_text) as (repo, git):
        calls, paths = [], []
        class Agent:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(prompt)
                paths.append(cwd)
                plan = cwd / 'plan.md'
                text = plan.read_text().replace('- [ ] implement', '- [x] implement')
                if mode == 'deleted_requirement':
                    text = text.replace('- [ ] validate output\n', '')
                else:
                    text = text.replace('- [ ] validate', '- [x] validate')
                plan.write_text(text)
                task_git = GitService(cwd)
                task_git.run('add', 'plan.md')
                task_git.run('commit', '-qm', 'complete checklist')
                if mode == 'dirty_completion':
                    (cwd / 'valuable-result.txt').write_text('completed deliverable, not staged\n')
                return ExecResult(output='done\n', returncode=0)
        progress = repo / '.gigaflex/progress/progress-audit.txt'
        runner = Runner(
            RunOptions(plan_file=repo/'plan.md', progress_file=progress, tasks_only=True,
                       finalize_enabled=False, delay_seconds=0, task_completion_retries=1),
            Agent(), ProgressLog(progress), task_worktrees=TaskWorktreeManager(git))
        error = None
        try:
            runner.run()
        except RuntimeError as exc:
            error = str(exc)
        return dict(case=mode, accepted=error is None, error=error, agent_calls=len(calls),
                    worktree_exists=paths[0].exists(),
                    deliverable_in_main=(repo/'valuable-result.txt').exists(),
                    main_plan=(repo/'plan.md').read_text())

def review_case(mode):
    with repository('# Plan\n### Task 1: produce result\n- [x] done\n') as (repo, git):
        review_heads, synthesis_calls = [], []
        class Reviewer:
            def run_batch(self, prompts, *, workdirs=None):
                review_heads.append(git.head_commit())
                needs_finding = mode == 'confirmed_stall' or (mode == 'dirty_synthesis' and len(review_heads) == 1)
                return {name: ExecResult(output=(FINDING if needs_finding and name == 'quality' else 'NO FINDINGS'), returncode=0)
                    for name in prompts}
        class Synthesis:
            def run(self, prompt, *, retry_guard=None):
                synthesis_calls.append(prompt)
                if mode == 'dirty_synthesis':
                    (repo/'result.txt').write_text('fixed but uncommitted\n')
                decision = 'confirmed' if mode == 'confirmed_stall' else 'fixed'
                reason = ('The value is still wrong.' if mode == 'confirmed_stall'
                          else 'Corrected the value in result.txt.')
                ledger = (
                    '<SYNTHESIS_DECISION>\n'
                    'finding_id: F001\n'
                    f'decision: {decision}\n'
                    f'reason: {reason}\n'
                    '</SYNTHESIS_DECISION>'
                )
                return ExecResult(output=ledger, returncode=0)
        class Finalizer:
            def run(self, prompt, *, retry_guard=None):
                if mode == 'finalize_mutation':
                    (repo/'result.txt').write_text('new behavior after review\n')
                    git.run('add', 'result.txt')
                    git.run('commit', '-qm', 'finalize changes behavior')
                return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE, returncode=0)
        progress = repo/'.gigaflex/progress/progress-audit.txt'
        runner = Runner(
            RunOptions(plan_file=repo/'plan.md', progress_file=progress,
                       default_branch=git.head_commit(), allow_dirty=False, delay_seconds=0,
                       review_iterations=2),
            Reviewer(), ProgressLog(progress), review_agent_executor=Reviewer(),
            synthesis_executor=Synthesis(), finalize_executor=Finalizer(),
            review_worktrees=ReviewWorktreeManager(git))
        error = None
        try:
            runner.run()
        except RuntimeError as exc:
            error = str(exc)
        return dict(case=mode, accepted=error is None, error=error,
                    review_passes=len(review_heads),
                    synthesis_calls=len(synthesis_calls),
                    final_head_was_reviewed=git.head_commit() in review_heads,
                    git_status=git.run('status','--short').stdout)

def changed_review_policy():
    with repository('# Plan\n### Task 1: produce result\n- [x] done\n') as (repo, git):
        progress = repo/'.gigaflex/progress/progress-audit.txt'
        checkpoint = RunCheckpoint(repo/'.gigaflex/checkpoint.json', git,
                                   identity='plan:demo', base_commit=git.head_commit())
        calls = []
        class Reviewer:
            def run_batch(self, prompts, *, workdirs=None):
                calls.append(prompts)
                return {name: ExecResult(output='NO FINDINGS', returncode=0) for name in prompts}
        options = RunOptions(plan_file=repo/'plan.md', progress_file=progress,
                             default_branch=git.head_commit(), finalize_enabled=False)
        Runner(options, Reviewer(), ProgressLog(progress), checkpoint=checkpoint,
               review_worktrees=ReviewWorktreeManager(git)).run()
        first_calls = len(calls)
        changed = replace(options, prompts=replace(DEFAULT_PROMPTS,
                          review_agent=DEFAULT_PROMPTS.review_agent + '\nCheck a newly required invariant.\n'))
        Runner(changed, Reviewer(), ProgressLog(progress), checkpoint=checkpoint,
               review_worktrees=ReviewWorktreeManager(git)).run()
        return dict(case='changed_review_policy', first_review_calls=first_calls,
                    new_policy_review_calls=len(calls)-first_calls)

if __name__ == '__main__':
    cases = [task_case('dirty_completion'), task_case('deleted_requirement'),
             review_case('finalize_mutation'), review_case('dirty_synthesis'),
             review_case('confirmed_stall'), changed_review_policy()]
    print(json.dumps(cases, indent=2, ensure_ascii=False))
