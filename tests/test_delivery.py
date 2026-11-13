import contextlib
from email.utils import formatdate
import http.server
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from soso.cli import main
from soso.delivery import configure_models, load_model_pool, ollama_models, start_goal
from soso.engine import DEFAULTS, evaluate
from soso.models import ModelProtocolError, Budget, BudgetExceeded, ModelUnavailable, Router, cli_unavailable, retry_timestamp
from soso.process import execute, git


EVALUATOR = '''import json
from pathlib import Path
p=Path('stage.txt')
stage=int(p.read_text()) if p.exists() else 0
print(json.dumps({'score':100 if stage>=2 else 0,'complete':stage>=2,'checks':{'playable':stage>=2}}))
'''


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'project'
        self.repo.mkdir()
        self.config = {**DEFAULTS, 'models': {'test': {'kind': 'command', 'command': ['false']}},
                       'routes': {r: ['test'] for r in ('worker', 'planner', 'reviewer')},
                       'rounds': 1, 'patience': 1, 'parallel': 1, 'max_steps': 5,
                       'max_calls': 30, 'max_seconds': 15, 'protected': list(DEFAULTS['protected'])}
        self.final_approve = True
        self.prompts = []

    def tearDown(self):
        self.tmp.cleanup()

    def answer(self, router, role, prompt, cwd, attempt=0, task=None):
        router.budget.claim()
        self.prompts.append((role, prompt))
        if role == 'planner':
            if 'Create a small acceptance contract' in prompt:
                return {'milestones': ['scaffold', 'finish game'], 'assumptions': [], 'evaluator': EVALUATOR}
            return {'experiments': [{'task': 'advance one stage'}]}
        if role == 'reviewer':
            return {'approve': self.final_approve if prompt.startswith('Perform independent final acceptance review.') else True,
                    'useful': True, 'complete': self.final_approve}
        last = json.loads(prompt.split('LAST RESULT:\n')[1])
        if 'files' in last:
            stage = Path(cwd) / 'stage.txt'
            return {'tool': 'write', 'path': 'stage.txt', 'content': str(int(stage.read_text()) + 1 if stage.exists() else 1)}
        return {'tool': 'done', 'summary': 'advanced'}

    def start(self, intent='make a game', **kwargs):
        def configured(c, *a):
            return {**c, **self.config, **{k: c[k] for k in ('evaluate', 'protected', 'delivery') if k in c}}
        with patch('soso.delivery.configure_models', side_effect=configured), \
             patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)):
            return start_goal(self.repo, intent, once=True, **kwargs)


    def test_intermediate_accepted_carries_to_new_epoch_and_completion(self):
        first = self.start()
        self.assertEqual(first['status'], 'incomplete')
        root = Path(first['worktree'])
        self.assertEqual((root / 'stage.txt').read_text(), '1')
        self.assertEqual(first['epochs'][0]['best'], 0)
        self.assertFalse((self.repo / 'INTENT.md').exists())
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'complete')
        self.assertEqual((root / 'stage.txt').read_text(), '2')
        self.assertEqual(len(second['epochs']), 2)
        self.assertNotEqual(second['epochs'][0]['head'], second['head'])
        plan = [p for r, p in self.prompts if r == 'planner' and 'RECENT FEEDBACK' in p][-1]
        self.assertIn(first['epochs'][0]['head'], plan)
        self.assertTrue((root / '.soso-delivery/contract.json').exists())

    def test_increment_review_accepts_partial_progress_without_final_completion(self):
        original = self.answer
        increment_reviews = []
        self.final_approve = False
        def answer(router, role, prompt, cwd, **kwargs):
            reply = original(router, role, prompt, cwd, **kwargs)
            if role == 'planner' and 'Create a small acceptance contract' in prompt:
                reply['evaluator'] = EVALUATOR.replace('100 if stage>=2 else 0',
                                                       '100 if stage>=2 else 75 if stage==1 else 0')
            if role == 'reviewer' and 'Independently review this increment.' in prompt:
                # A reviewer must assess useful progress separately from the completion gate.
                self.assertIn('This is NOT final acceptance', prompt)
                self.assertIn('Approve a useful nonregressing partial increment', prompt)
                self.assertIn('distinguish new defects introduced by this change', prompt)
                self.assertIn('Missing future features or tests alone are not grounds', prompt)
                self.assertIn('Reject new regressions, gaming, scope violations and useless changes', prompt)
                increment_reviews.append(json.loads(prompt.split('\nEVALUATION:\n')[1]))
                return {'approve': True, 'useful': True, 'reason': 'Useful progress; tests remain for later.'}
            return reply
        self.answer = answer
        first = self.start()
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['epochs'][0]['best'], 75)
        self.assertEqual((Path(first['worktree']) / 'stage.txt').read_text(), '1')
        self.assertFalse(increment_reviews[0]['complete'])
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'incomplete')
        report = json.loads(Path(second['epochs'][-1]['report']).read_text())
        self.assertFalse(report['completion_review']['approve'])
        self.assertEqual((Path(second['worktree']) / 'stage.txt').read_text(), '2')

    def use_evaluator(self, source):
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            reply = original(router, role, prompt, cwd, **kwargs)
            if role == 'planner' and 'Create a small acceptance contract' in prompt:
                reply['evaluator'] = source
            return reply
        self.answer = answer

    def test_config_set_during_an_epoch_baseline_stops_the_goal_and_blocks_new_runs(self):
        # Accepted code the next epoch's baseline runs sets a filter; nothing is recorded yet to restore it from,
        # and the first candidate would record it as the repository's own.
        self.use_evaluator(EVALUATOR.replace("print(", "import os, subprocess\n"
                                             "if stage == 1 and '-c0-' not in os.getcwd():\n"
                                             "    subprocess.run(['git', 'config', 'filter.h.clean', 'cat'])\n"
                                             "print(", 1))
        first = self.start()
        self.assertEqual((Path(first['worktree']) / 'stage.txt').read_text(), '1')
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                self.assertRaisesRegex(ValueError, 'baseline evaluation changed repository configuration'):
            self.start(resume_goal=first['goal_id'])
        common = Path(git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        self.assertTrue((common / 'soso/unrestored.json').exists())

    def resume(self, result):
        # A clock that only moves forward, past any retry time a previous call saved.
        self.clock = max(getattr(self, 'clock', 0), time.time()) + 100
        with patch('soso.delivery.time.time', return_value=self.clock):
            return self.start(resume_goal=result['goal_id'])

    def set_state(self, result, **values):
        path = Path(result['report'])
        path.write_text(json.dumps({**json.loads(path.read_text()), **values}))

    def reject_increments(self):
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                router.budget.claim()
                return {'approve': False, 'useful': False, 'reason': 'not useful'}
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer

    def reject_contracts(self):
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this proposed acceptance contract'):
                router.budget.claim()
                return {'approve': False, 'reason': 'it checks file presence only'}
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer

    def test_goal_blocks_after_six_epochs_without_an_accepted_change(self):
        self.reject_increments()
        result = self.start()
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 1))
        self.set_state(result, stalled=4)  # as after four more such epochs, each a --once resume
        result = self.resume(result)
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 5))
        result = self.resume(result)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'stalled: no accepted change in 6 consecutive epochs (last: plateau); '
                                           'resume to try again')
        self.assertIsNone(result['next_retry_at'])
        self.assertEqual(result.get('stalled'), 0)
        resumed = self.resume(result)  # a resume gets six more
        self.assertEqual((resumed['status'], resumed.get('stalled')), ('incomplete', 1))

    def test_epoch_that_accepts_a_change_resets_the_stall_count_even_when_it_waits(self):
        accept = self.answer
        self.reject_increments()
        first = self.start()
        self.set_state(first, stalled=5)
        plans = []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'planner' and prompt.startswith('Plan one small useful next milestone step.'):
                plans.append(prompt)
                if len(plans) == 2:
                    raise ModelUnavailable(time.time() + 3600)
            return accept(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        self.config['rounds'] = 2
        result = self.resume(first)
        self.assertEqual(result['status'], 'waiting')
        self.assertNotEqual(result['head'], first['head'])
        self.assertEqual(result.get('stalled'), 0)

    def test_killed_epoch_that_accepted_a_change_resets_the_stall_count(self):
        first = self.start()  # accepts stage 1
        root = Path(first['worktree'])
        killed = first['epochs'][-1]['report']
        self.set_state(first, active_epoch=killed, epochs=first['epochs'][:-1], head=git(root, 'rev-parse', 'HEAD~1'),
                       status='running', stalled=5)  # killed after the merge, before the goal recorded it
        self.reject_increments()
        result = self.resume(first)
        self.assertEqual(result['head'], first['head'])
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 1))

    def test_epochs_that_accept_and_then_wait_keep_the_provider_time(self):
        # Each epoch accepts a change, then a provider asks for a short wait: that wait is a first failure again.
        self.use_evaluator(EVALUATOR.replace('stage>=2', 'stage>=9'))
        accept = self.answer
        plans = []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'planner' and prompt.startswith('Plan one small useful next milestone step.'):
                plans.append(prompt)
                if len(plans) % 2 == 0:
                    raise ModelUnavailable(time.time() + 5)
            return accept(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        self.config['rounds'] = 2
        first = self.start()
        self.assertEqual((first['status'], first['failures']), ('waiting', 1))
        second = self.resume(first)
        self.assertEqual(second['status'], 'waiting')
        self.assertNotEqual(second['head'], first['head'])
        self.assertEqual(second['failures'], 1)
        self.assertEqual(second['next_retry_at'], self.clock + 5)

    def test_setup_that_never_freezes_a_contract_blocks_after_six_attempts(self):
        self.reject_contracts()
        result = self.start()
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 1))
        self.set_state(result, stalled=4)
        result = self.resume(result)
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 5))
        result = self.resume(result)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'acceptance setup: stalled: no approved acceptance contract in 6 consecutive '
                         'attempts; resume to try again: REPAIR ACCEPTANCE CONTRACT AFTER INDEPENDENT REVIEW: '
                         'it checks file presence only')
        self.assertIn('it checks file presence only', result['setup_repair_reason'])  # kept for the next attempt
        self.assertIsNone(result['next_retry_at'])
        self.assertEqual(result.get('stalled'), 0)
        resumed = self.resume(result)
        self.assertEqual((resumed['status'], resumed.get('stalled')), ('incomplete', 1))

    def test_setup_that_keeps_running_out_of_budget_blocks_after_six_attempts(self):
        def exhausted(router, role, prompt, cwd, **kwargs):
            raise BudgetExceeded('model call budget exhausted')
        self.answer = exhausted
        result = self.start()
        for _ in range(5):
            self.assertEqual(result['status'], 'incomplete')
            result = self.resume(result)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(result['reason'].startswith('acceptance setup: stalled: '))
        self.assertTrue(result['reason'].endswith('resume to try again: model call budget exhausted'))

    def test_frozen_contract_resets_the_stall_count(self):
        self.reject_contracts()
        first = self.start()
        self.assertEqual((first['status'], first.get('stalled')), ('incomplete', 1))
        self.set_state(first, stalled=5)
        self.answer = DeliveryTests.answer.__get__(self)
        self.reject_increments()
        result = self.resume(first)
        self.assertIn('policy', result)
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 1))  # the stalled epoch, not 6

    def test_recovered_contract_resets_the_stall_count(self):
        def interrupt(root, *args):
            if 'Freeze goal acceptance contract' in args:
                raise KeyboardInterrupt
            return git(root, *args)
        with patch('soso.delivery.git', side_effect=interrupt):
            first = self.start()
        self.assertNotIn('policy', first)
        self.set_state(first, stalled=5)
        self.reject_increments()
        result = self.resume(first)
        self.assertIn('policy', result)
        self.assertEqual((result['status'], result.get('stalled')), ('incomplete', 1))

    def test_lower_baseline_for_the_same_commit_is_recorded_as_drift(self):
        # The evaluator reads state outside the goal worktree, so the accepted commit scores lower in the next epoch.
        drift = Path(self.tmp.name) / 'drift'
        self.use_evaluator(EVALUATOR.replace("print(json.dumps({'score':100 if stage>=2 else 0,",
                                             f"print(json.dumps({{'score':(100 if stage>=2 else 50*stage)-10*Path({str(drift)!r}).exists(),"))
        first = self.start()
        self.assertEqual(first['epochs'][-1]['best'], 50)
        drift.write_text('')
        second = self.resume(first)
        report = json.loads(Path(second['epochs'][-1]['report']).read_text())
        self.assertEqual(report.get('baseline_drift'), {'previous_best': 50, 'baseline': 40})
        events = [json.loads(line) for line in (Path(second['epochs'][-1]['report']).parent / 'events.jsonl').read_text().splitlines()]
        self.assertEqual([(e['previous_best'], e['baseline']) for e in events if e['event'] == 'baseline_drift'], [(50, 40)])
        self.assertEqual(second['status'], 'complete')  # evidence only: acceptance is unchanged

    def test_epoch_without_a_best_score_gives_no_drift_evidence(self):
        first = self.start()
        state = json.loads(Path(first['report']).read_text())
        state['epochs'][-1]['best'] = None  # e.g. an epoch killed before its baseline
        self.set_state(first, epochs=state['epochs'])
        second = self.resume(first)
        self.assertEqual(second['status'], 'complete')
        self.assertNotIn('baseline_drift', json.loads(Path(second['epochs'][-1]['report']).read_text()))

    def test_baseline_ignores_files_left_by_earlier_evaluator_runs(self):
        # The goal worktree is long-lived; the contract-time evaluator run leaves an ignored marker there.
        # A slow machine must not run out of the 15 s epoch budget before the candidate is accepted.
        self.config['max_seconds'] = 600
        self.repo.joinpath('.gitignore').write_text('cache/\n')
        self.use_evaluator('''import json
from pathlib import Path
p=Path('stage.txt')
stage=int(p.read_text()) if p.exists() else 0
marker=Path('cache/marker')
stale=marker.exists()
marker.parent.mkdir(exist_ok=True)
marker.write_text('1')
print(json.dumps({'score':min(100,10*stage+(50 if stale else 0)),'complete':stage>=2}))
''')
        first = self.start()
        report = json.loads(Path(first['epochs'][0]['report']).read_text())
        self.assertEqual(report['status'], 'round_limit', report.get('reason'))
        self.assertEqual(report['baseline']['score'], 0)
        self.assertEqual(first['epochs'][0]['best'], 10)
        self.assertEqual((Path(first['worktree']) / 'stage.txt').read_text(), '1')

    def test_contract_repair_scores_the_baseline_without_earlier_leftovers(self):
        # Each epoch checks the goal worktree out again, but contract attempts score it as it stands:
        # the second attempt must not see the marker the first attempt's evaluator left.
        self.config['max_seconds'] = 600
        self.repo.joinpath('.gitignore').write_text('cache/\n')
        self.use_evaluator('''import json
from pathlib import Path
marker=Path('cache/marker')
stale=marker.exists()
marker.parent.mkdir(exist_ok=True)
marker.write_text('1')
print(json.dumps({'score':50 if stale else 0,'complete':False}))
''')
        original, reviews = self.answer, []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and 'proposed acceptance contract' in prompt and not reviews:
                reviews.append(prompt)
                router.budget.claim()
                return {'approve': False, 'reason': 'repair the coverage'}
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        first = self.start()
        lines = (Path(first['report']).parent / 'contract-reviews.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(line)['baseline']['score'] for line in lines], [0, 0])

    def test_candidate_ignored_shadow_files_do_not_raise_accepted_score(self):
        self.config['max_seconds'] = 600  # as above: the result must not depend on machine speed
        self.repo.joinpath('.gitignore').write_text('cache/\n')
        self.use_evaluator('''import json
from pathlib import Path
p=Path('stage.txt')
stage=(int(p.read_text()) if p.exists() else 0)+Path('cache/bonus').exists()
print(json.dumps({'score':100 if stage>=2 else 0,'complete':stage>=2}))
''')
        def work(router, task, path, config, log, index):
            (Path(path) / 'cache').mkdir()
            (Path(path) / 'cache/bonus').write_text('shadow')
            (Path(path) / 'stage.txt').write_text('1')
            return {'finished': True}
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
        report = json.loads(Path(first['epochs'][0]['report']).read_text())
        candidate = report['rounds'][0]['experiments'][0]
        self.assertEqual(candidate['evaluation']['score'], 0)
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['epochs'][0]['best'], 0)

    def test_configured_python_m_evaluator_and_its_packages_are_protected(self):
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Tester')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'bench').mkdir()
        (self.repo / 'bench/__init__.py').write_text('')
        (self.repo / 'bench/score.py').write_text(EVALUATOR)
        (self.repo / 'soso.json').write_text(json.dumps({**self.config, 'evaluate': [sys.executable, '-B', '-m', 'bench.score']}))
        git(self.repo, 'add', '--all')
        git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'configured policy')
        state = self.start()
        config = json.loads((Path(state['report']).parent / 'config.json').read_text())
        self.assertIn('bench/score.py', config['protected'])
        self.assertIn('bench/__init__.py', config['protected'])

    def resume_paused_worker(self, error, status):
        calls = []
        first = None
        def work(router, task, path, config, log, index):
            calls.append(task)
            file = Path(path) / 'stage.txt'
            if len(calls) == 1:
                file.write_text('1')
                raise error
            self.assertEqual(file.read_text(), '1')
            self.assertEqual(git(Path(first['worktree']), 'rev-parse', 'HEAD'), first['head'])
            self.assertFalse((Path(first['worktree']) / 'stage.txt').exists())
            file.write_text('2')
            return {'finished': True, 'summary': 'Finished the retained scaffold.'}
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
            self.assertEqual(first['status'], status)
            draft = first['draft']
            self.assertEqual(draft['phase'], 'worker')
            self.assertEqual(draft['base'], first['head'])
            self.assertNotEqual(draft['commit'], first['head'])
            self.assertEqual(git(self.repo, 'show', draft['commit'] + ':stage.txt'), '1')
            self.assertEqual(git(self.repo, 'rev-parse', draft['ref']), draft['commit'])
            self.assertEqual(json.loads(Path(first['epochs'][0]['report']).read_text())['draft'], draft)
            if isinstance(error, ModelUnavailable):
                # Recover a crash after the epoch checkpoint but before updating goal state.
                state = json.loads(Path(first['report']).read_text())
                state.pop('draft')
                state['active_epoch'] = first['epochs'][0]['report']
                state['epochs'] = []
                Path(first['report']).write_text(json.dumps(state))
                early = self.start(resume_goal=first['goal_id'])
                self.assertEqual(early['draft'], draft)
                self.assertEqual(early['status'], 'waiting')
                self.assertEqual(len(calls), 1)
            later = max(time.time() + 5, getattr(error, 'retry_at', None) or 0) + 1
            with patch('soso.delivery.time.time', return_value=later):
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(resumed['status'], 'complete')
        self.assertIsNone(resumed['draft'])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual((Path(resumed['worktree']) / 'stage.txt').read_text(), '2')
        report = json.loads(Path(resumed['epochs'][-1]['report']).read_text())
        candidate = report['rounds'][0]['experiments'][0]
        self.assertEqual(candidate['evaluation']['score'], 100)
        self.assertTrue(candidate['review']['approve'])
        self.assertEqual(report['rounds'][0]['accepted'], resumed['head'])

    def test_unfinished_worker_survives_budget_epoch(self):
        self.resume_paused_worker(BudgetExceeded('test epoch exhausted'), 'incomplete')

    def test_unfinished_worker_survives_quota_and_crash_recovery(self):
        self.resume_paused_worker(ModelUnavailable(time.time() + 300), 'waiting')

    def test_unfinished_worker_survives_graceful_interrupt(self):
        self.resume_paused_worker(KeyboardInterrupt(), 'interrupted')

    def test_evaluation_budget_retries_retained_commit_without_worker(self):
        def work(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('2')
            return {'finished': True}
        evaluations = []
        def measure(path, config, budget):
            evaluations.append(str(path))
            if len(evaluations) == 1:
                raise BudgetExceeded('evaluation deadline')
            return evaluate(path, config, budget)
        with patch('soso.engine.worker', side_effect=work) as worker, \
                patch('soso.engine.evaluate', side_effect=measure) as evaluate_candidate:
            first = self.start()
            self.assertEqual(first['draft']['phase'], 'evaluate')
            self.assertFalse((Path(first['worktree']) / 'stage.txt').exists())
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(evaluate_candidate.call_count, 2)
        self.assertEqual(resumed['status'], 'complete')
        self.assertIsNone(resumed['draft'])

    def test_review_quota_retries_fresh_evaluation_and_review(self):
        original = self.answer
        reviews = []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                reviews.append(prompt)
                if len(reviews) == 1:
                    raise ModelUnavailable(time.time() + 300)
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        with patch('soso.engine.evaluate', wraps=evaluate) as evaluate_candidate:
            first = self.start()
            self.assertEqual(first['status'], 'waiting')
            self.assertEqual(first['draft']['phase'], 'review')
            with patch('soso.delivery.time.time', return_value=time.time() + 305):
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(evaluate_candidate.call_count, 2)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len([p for r, p in self.prompts if r == 'worker']), 2)
        self.assertIsNone(resumed['draft'])
        self.assertEqual((Path(resumed['worktree']) / 'stage.txt').read_text(), '1')

    def test_review_budget_pause_keeps_the_draft(self):
        original = self.answer
        reviews = []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                reviews.append(prompt)
                if len(reviews) == 1:
                    raise BudgetExceeded('review epoch exhausted')
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        first = self.start()
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['draft']['phase'], 'review')
        report = json.loads(Path(first['epochs'][-1]['report']).read_text())
        self.assertNotIn('dropped_draft', report['rounds'][-1])
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(len(reviews), 2)
        self.assertIsNone(resumed['draft'])
        self.assertEqual(len([p for r, p in self.prompts if r == 'worker']), 2)

    def test_explicit_increment_rejection_clears_pending_draft(self):
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                return {'approve': False, 'useful': False, 'reason': 'Introduces a regression.'}
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        state = self.start()
        self.assertIsNone(state['draft'])
        self.assertFalse((Path(state['worktree']) / 'stage.txt').exists())

    def test_useful_rejection_repairs_existing_files_before_fresh_acceptance(self):
        original = self.answer
        reviews = []
        tasks = []
        first = None
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                reviews.append(prompt)
                if len(reviews) == 1:
                    return {'approve': False, 'useful': True, 'reason': 'Fix the tests to use the actual game API.'}
                self.assertFalse((Path(first['worktree']) / 'stage.txt').exists())
                self.assertEqual(git(Path(first['worktree']), 'rev-parse', 'HEAD'), first['head'])
            return original(router, role, prompt, cwd, **kwargs)
        def work(router, task, path, config, log, index):
            tasks.append(task)
            file = Path(path) / 'stage.txt'
            if len(tasks) == 1:
                self.assertFalse(file.exists())
                file.write_text('1')
            else:
                self.assertEqual(file.read_text(), '1')
                self.assertIn('Fix the tests to use the actual game API.', task)
                self.assertEqual(task.count('REVIEW REPAIR REQUIRED:'), 1)
                file.write_text('2')
            return {'finished': True}
        self.answer = answer
        with patch('soso.engine.worker', side_effect=work), \
                patch('soso.engine.evaluate', wraps=evaluate) as evaluations:
            first = self.start()
            draft = first['draft']
            self.assertEqual(draft['phase'], 'worker')
            self.assertEqual(draft['repairs'], 1)
            self.assertEqual(draft['task'], tasks[0])
            self.assertEqual(draft['base'], first['head'])
            self.assertEqual(draft['policy'], first['policy'])
            self.assertEqual(git(self.repo, 'rev-parse', draft['ref']), draft['commit'])
            report = json.loads(Path(first['epochs'][-1]['report']).read_text())
            self.assertIsNone(report['rounds'][0]['accepted'])
            self.assertEqual(report['best'], 0)
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(evaluations.call_count, 2)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(resumed['status'], 'complete')
        self.assertIsNone(resumed['draft'])
        self.assertEqual(resumed['policy'], first['policy'])
        self.assertEqual((Path(resumed['worktree']) / 'stage.txt').read_text(), '2')

    def test_useful_rejection_repairs_are_bounded_across_epochs(self):
        original = self.answer
        tasks = []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                return {'approve': False, 'useful': True, 'reason': 'Repair the API test assertions.'}
            return original(router, role, prompt, cwd, **kwargs)
        def work(router, task, path, config, log, index):
            tasks.append(task)
            file = Path(path) / 'stage.txt'
            self.assertEqual(file.read_text() if file.exists() else '0', str(len(tasks) - 1))
            self.assertLessEqual(task.count('REVIEW REPAIR REQUIRED:'), 1)
            file.write_text(str(len(tasks)))
            return {'finished': True}
        self.answer = answer
        with patch('soso.engine.worker', side_effect=work):
            state = self.start()
            accepted = state['head']
            for repairs in (1, 2, 3):
                self.assertEqual(state['draft']['repairs'], repairs)
                self.assertEqual(state['head'], accepted)
                self.assertEqual(state['draft']['task'], tasks[0])
                with patch('soso.delivery.time.time', return_value=max(time.time() + 5, state['next_retry_at'] + 1)):
                    state = self.start(resume_goal=state['goal_id'])
        self.assertEqual(len(tasks), 4)
        self.assertIsNone(state['draft'])
        self.assertEqual(state['head'], accepted)
        self.assertFalse((Path(state['worktree']) / 'stage.txt').exists())

    def test_repair_budget_pause_preserves_count_and_original_task(self):
        original = self.answer
        tasks = []
        reviews = []
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                reviews.append(prompt)
                if len(reviews) == 1:
                    return {'approve': False, 'useful': True, 'reason': 'Repair the API tests.'}
            return original(router, role, prompt, cwd, **kwargs)
        def work(router, task, path, config, log, index):
            tasks.append(task)
            file = Path(path) / 'stage.txt'
            self.assertEqual(file.read_text() if file.exists() else '0', str(len(tasks) - 1))
            file.write_text(str(len(tasks)))
            if len(tasks) == 2:
                raise BudgetExceeded('repair epoch exhausted')
            return {'finished': True}
        self.answer = answer
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                paused = self.start(resume_goal=first['goal_id'])
            self.assertEqual(paused['draft']['repairs'], 1)
            self.assertEqual(paused['draft']['task'], tasks[0])
            self.assertEqual(paused['draft']['repair_feedback'], 'Repair the API tests.')
            self.assertEqual(paused['head'], first['head'])
            with patch('soso.delivery.time.time', return_value=max(time.time() + 5, paused['next_retry_at'] + 1)):
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(tasks[1], tasks[2])
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(len(reviews), 2)

    def test_protected_change_during_repair_discards_draft(self):
        original = self.answer
        count = 0
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                return {'approve': False, 'useful': True, 'reason': 'Repair the API tests.'}
            return original(router, role, prompt, cwd, **kwargs)
        def work(router, task, path, config, log, index):
            nonlocal count
            count += 1
            if count == 1:
                (Path(path) / 'stage.txt').write_text('1')
            else:
                (Path(path) / '.soso-delivery/evaluate.py').write_text('print(100)')
            return {'finished': True}
        self.answer = answer
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                rejected = self.start(resume_goal=first['goal_id'])
        self.assertIsNone(rejected['draft'])
        self.assertEqual(rejected['head'], first['head'])
        self.assertEqual(rejected['policy'], first['policy'])

    def reject_poisoned_paused_worker(self, history=False):
        def work(router, task, path, config, log, index):
            if history:
                (Path(path) / 'stage.txt').write_text('1')
                git(path, 'add', '--all')
                git(path, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Unauthorized worker commit')
            else:
                (Path(path) / '.soso-delivery/evaluate.py').write_text('print(100)')
            raise BudgetExceeded('pause after poisoning candidate')
        with patch('soso.engine.worker', side_effect=work), \
                self.assertRaisesRegex(ValueError, 'cannot retain candidate with changed'):
            self.start()
        state_file = next((self.repo / '.git/soso/goals').glob('*/state.json'))
        state = json.loads(state_file.read_text())
        report = json.loads(Path(state['active_epoch']).read_text())
        self.assertNotIn('draft', report)
        self.assertEqual(git(Path(state['worktree']), 'rev-parse', 'HEAD'), state['head'])
        self.assertEqual(git(self.repo, 'for-each-ref', '--format=%(refname)', 'refs/soso/'), '')

    def test_paused_worker_cannot_preserve_changed_protected_policy(self):
        self.reject_poisoned_paused_worker()

    def test_paused_worker_cannot_preserve_changed_git_history(self):
        self.reject_poisoned_paused_worker(history=True)

    def test_retained_candidate_changed_ref_is_rejected_before_worker(self):
        def pause(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('1')
            raise BudgetExceeded('pause')
        with patch('soso.engine.worker', side_effect=pause):
            first = self.start()
        git(self.repo, 'update-ref', first['draft']['ref'], first['head'])
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                patch('soso.engine.worker') as worker, \
                self.assertRaisesRegex(ValueError, 'retained candidate ref or history changed'):
            self.start(resume_goal=first['goal_id'])
        worker.assert_not_called()
        self.assert_dropped_then_planned(first, 'retained candidate ref or history changed')

    def test_retained_candidate_changed_base_is_rejected_before_worker(self):
        def pause(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('1')
            raise BudgetExceeded('pause')
        with patch('soso.engine.worker', side_effect=pause):
            first = self.start()
        state_file = Path(first['report'])
        state = json.loads(state_file.read_text())
        state['draft']['base'] = '0' * 40
        state_file.write_text(json.dumps(state))
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                patch('soso.engine.worker') as worker, \
                self.assertRaisesRegex(ValueError, 'retained candidate base or policy binding changed'):
            self.start(resume_goal=first['goal_id'])
        worker.assert_not_called()
        self.assert_dropped_then_planned(first, 'retained candidate base or policy binding changed')

    def assert_dropped_then_planned(self, first, reason):
        """The failed draft blocks the goal once and is dropped, its ref kept; the next resume plans afresh."""
        state = json.loads(Path(first['report']).read_text())
        self.assertEqual(state['status'], 'blocked')
        self.assertIn(reason, state['reason'])
        self.assertIsNone(state['draft'])
        report = json.loads(Path(state.get('active_epoch') or state['epochs'][-1]['report']).read_text())
        self.assertNotIn('draft', report)
        dropped = report['rounds'][-1]['dropped_draft']
        self.assertIn(reason, dropped['reason'])
        git(self.repo, 'rev-parse', '--verify', dropped['ref'])  # kept for inspection
        plans = len([p for r, p in self.prompts if 'Plan one small useful next milestone step' in p])
        with patch('soso.delivery.time.time', return_value=time.time() + 10):
            resumed = self.start(resume_goal=first['goal_id'])
        planned = [p for r, p in self.prompts if 'Plan one small useful next milestone step' in p]
        self.assertGreater(len(planned), plans)
        self.assertIn(reason, planned[plans])
        return resumed

    def test_draft_whose_evaluation_leaves_a_file_is_dropped_not_retried(self):
        def work(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('2')
            return {'finished': True}
        def leaves_file(path, config, budget):
            result = evaluate(path, config, budget)
            (Path(path) / 'leftover.txt').write_text('left by the evaluation')
            return result
        with patch('soso.engine.worker', side_effect=work) as worker:
            with patch('soso.engine.evaluate', side_effect=leaves_file):
                first = self.start()
            self.assertEqual(first['status'], 'blocked')
            report = json.loads(Path(first['epochs'][-1]['report']).read_text())
            self.assertEqual(report['rounds'][-1]['dropped_draft']['phase'], 'evaluate')
            resumed = self.assert_dropped_then_planned(first, 'evaluation modified source or protected files')
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(resumed['status'], 'complete')

    def test_draft_whose_review_overflows_the_reviewer_context_is_dropped_not_retried(self):
        original = self.answer
        reviews = []
        overflow = 'all routes failed: test: context budget exceeded'
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                reviews.append(prompt)
                if len(reviews) == 1:
                    raise RuntimeError(overflow)
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        with self.assertRaisesRegex(RuntimeError, overflow):
            self.start()
        state_file = next((self.repo / '.git/soso/goals').glob('*/state.json'))
        first = {**json.loads(state_file.read_text()), 'report': str(state_file)}
        report = json.loads(Path(first['active_epoch']).read_text())
        self.assertEqual(report['rounds'][-1]['dropped_draft']['phase'], 'review')
        resumed = self.assert_dropped_then_planned(first, overflow)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(resumed['status'], 'incomplete')

    def test_draft_whose_repair_cannot_be_kept_is_dropped(self):
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                (Path(cwd) / 'review-notes.txt').write_text('left in the goal worktree')
                return {'approve': False, 'useful': True, 'reason': 'Repair the API tests.'}
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        with self.assertRaisesRegex(ValueError, 'review modified accepted source'):
            self.start()
        state = json.loads(next((self.repo / '.git/soso/goals').glob('*/state.json')).read_text())
        self.assertIsNone(state['draft'])
        dropped = json.loads(Path(state['active_epoch']).read_text())['rounds'][-1]['dropped_draft']
        self.assertIn('review modified accepted source', dropped['reason'])
        git(self.repo, 'rev-parse', '--verify', dropped['ref'])

    def api_key_in_intent(self, pool):
        """Retain a draft for an intent holding an api_key_env value, then resume in a fresh process."""
        from soso import models
        gateway = {'kind': 'openai', 'url': 'http://localhost:1/v1', 'model': 'm', 'api_key_env': 'LLM_GATEWAY'}
        self.config['models'] = {**self.config['models'], 'gateway': gateway}
        kwargs = {}
        if pool:
            path = Path(self.tmp.name) / 'pool.json'
            path.write_text(json.dumps({'models': self.config['models'], 'routes': self.config['routes']}))
            kwargs['models_path'] = str(path)
        calls = []
        def work(router, task, path, config, log, index):
            calls.append(task)
            (Path(path) / 'stage.txt').write_text(str(len(calls)))
            if len(calls) == 1:
                raise BudgetExceeded('pause')
            return {'finished': True}
        with patch.dict(os.environ, {'LLM_GATEWAY': 'ollama'}), patch('soso.engine.worker', side_effect=work):
            with patch.object(models, 'KEY_VARIABLES', set()):
                first = self.start('make a game served by http://ollama:11434', **kwargs)
            self.assertEqual(first['draft']['phase'], 'worker')
            with patch.object(models, 'KEY_VARIABLES', set()), \
                    patch('soso.delivery.time.time', return_value=time.time() + 5):  # a new process
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(len([p for r, p in self.prompts if 'Plan one small useful next milestone step' in p]), 1)
        self.assertEqual(len(calls), 2)  # the resume continued the draft without planning again
        return json.loads(Path(resumed['report']).read_text())['intent']

    def test_api_key_value_in_the_intent_is_redacted_before_the_goal_is_stored(self):
        self.assertEqual(self.api_key_in_intent(pool=True), 'make a game served by http://[REDACTED]:11434')

    def test_intent_stays_as_stored_when_a_key_variable_is_learned_later(self):
        # E.g. a goal stored before its api_key_env name was known: its draft's binding hashes the stored intent.
        self.assertEqual(self.api_key_in_intent(pool=False), 'make a game served by http://ollama:11434')

    def test_resume_without_exec_keeps_the_draft_and_the_goal_permission(self):
        calls = []
        def work(router, task, path, config, log, index):
            calls.append((task, config['allow_exec']))
            (Path(path) / 'stage.txt').write_text(str(len(calls)))
            if len(calls) == 1:
                raise BudgetExceeded('pause')
            return {'finished': True}
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
            self.assertEqual(first['draft']['phase'], 'worker')
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                resumed = self.start(resume_goal=first['goal_id'], allow_exec=False)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(calls, [(calls[0][0], True), (calls[0][0], False)])  # the draft resumed, without exec
        self.assertTrue(resumed['allow_exec'])  # a later resume may use exec again

    def test_model_override_on_resume_drops_the_retained_draft_once(self):
        # --backend/--model replace the epoch's models and routes, which the draft binding covers: the draft is
        # dropped like any other draft that cannot resume, and the next resume plans afresh.
        def pause(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('1')
            raise BudgetExceeded('pause')
        with patch('soso.engine.worker', side_effect=pause):
            first = self.start()
        def configured(c, backend='auto', model=None):
            models = {'other': {'kind': 'command', 'command': ['false']}} if backend == 'codex' else self.config['models']
            return {**c, **self.config, 'models': models, 'routes': {r: list(models) for r in ('worker', 'planner', 'reviewer')},
                    **{k: c[k] for k in ('evaluate', 'protected', 'delivery') if k in c}}
        with patch('soso.delivery.configure_models', side_effect=configured), \
                patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)), \
                patch('soso.delivery.time.time', return_value=time.time() + 5), \
                self.assertRaisesRegex(ValueError, 'retained candidate base or policy binding changed'):
            start_goal(self.repo, once=True, resume_goal=first['goal_id'], backend='codex')
        self.assertEqual(self.assert_dropped_then_planned(first, 'binding changed')['status'], 'incomplete')

    def test_interrupt_during_checkout_keeps_the_retained_draft(self):
        # The checkout deletes every file before writing them again; an interrupt in between must not
        # drop the draft that was retained before it.
        from soso import engine
        def work(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('2')
            return {'finished': True}
        checkout = engine.checkout
        def interrupted(path, *args):
            (Path(path) / 'stage.txt').unlink()
            raise KeyboardInterrupt
        with patch('soso.engine.worker', side_effect=work) as worker:
            with patch('soso.engine.checkout', side_effect=interrupted):
                first = self.start()
            self.assertEqual(first['status'], 'interrupted')
            self.assertEqual(first['draft']['phase'], 'evaluate')
            with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                    patch('soso.engine.checkout', side_effect=checkout):
                resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(worker.call_count, 1)

    def test_goal_worktree_is_checked_out_again_before_the_next_baseline(self):
        # merge --ff-only rewrites only changed files, so id.txt keeps "$Id$" although the accepted
        # .gitattributes now expands it; the next epoch's baseline must score the commit as checked out.
        self.config['max_seconds'] = 600
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Tester')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.repo.joinpath('id.txt').write_text('$Id$\n')
        git(self.repo, 'add', 'id.txt')
        git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'base')
        self.use_evaluator('''import json
from pathlib import Path
p=Path('stage.txt')
stage=int(p.read_text()) if p.exists() else 0
expanded='$Id' + ':' in Path('id.txt').read_text()
print(json.dumps({'score':10*stage+(50 if expanded else 0),'complete':False}))
''')
        def work(router, task, path, config, log, index):
            (Path(path) / '.gitattributes').write_text('id.txt ident\n')
            (Path(path) / 'stage.txt').write_text('1')
            return {'finished': True}
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
        self.assertEqual(first['epochs'][0]['best'], 60)
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                patch('soso.engine.worker', return_value={'finished': True}):
            second = self.start(resume_goal=first['goal_id'])
        report = json.loads(Path(second['epochs'][-1]['report']).read_text())
        self.assertEqual(report['baseline']['score'], 60)

    def test_interrupt_during_goal_checkout_is_finished_on_resume(self):
        # The epoch's checkout deletes every file before writing them again; resume completes it.
        from soso import delivery
        first = self.start()
        self.assertEqual((Path(first['worktree']) / 'stage.txt').read_text(), '1')
        def interrupted(root, *args):
            (Path(root) / 'stage.txt').unlink()
            raise KeyboardInterrupt
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                patch('soso.delivery.checkout', side_effect=interrupted):
            second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'interrupted')
        with patch('soso.delivery.time.time', return_value=time.time() + 10):
            third = self.start(resume_goal=first['goal_id'])
        self.assertEqual(third['status'], 'complete')
        self.assertEqual((Path(third['worktree']) / 'stage.txt').read_text(), '2')

    def test_killed_epoch_leaves_no_watched_setting_for_the_next_baseline(self):
        # A worker with exec changed a watched setting and the process was killed before the restore.
        first = self.start()
        state_path = Path(first['report'])
        state = json.loads(state_path.read_text())
        killed = Path(first['epochs'][-1]['report'])
        killed.write_text(json.dumps({**json.loads(killed.read_text()), 'status': 'running'}))  # never finished
        state.update(active_epoch=str(killed), epochs=state['epochs'][:-1], status='running')
        state_path.write_text(json.dumps(state))
        git(self.repo, 'config', '--local', 'filter.evil.clean', 'cat')
        runs = set((self.repo / '.git/soso').iterdir())
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
                self.assertRaisesRegex(ValueError, 'could not restore'):
            self.start(resume_goal=first['goal_id'])
        self.assertEqual(json.loads(state_path.read_text())['status'], 'blocked')
        records = [run / 'watched-config.json' for run in set((self.repo / '.git/soso').iterdir()) - runs]
        self.assertFalse([r for r in records if r.is_file() and 'filter.evil' in r.read_text()])
        git(self.repo, 'config', '--local', '--unset', 'filter.evil.clean')  # put back as recorded
        with patch('soso.delivery.time.time', return_value=time.time() + 10):
            resumed = self.start(resume_goal=first['goal_id'])
        self.assertEqual(resumed['status'], 'complete')

    def test_epoch_that_raised_restored_its_settings_and_a_later_user_change_is_kept(self):
        # Unlike a killed epoch, an epoch that ended with an error ran its restores: a watched setting the user
        # changes afterwards is the user's own, and the next resume proceeds.
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                raise RuntimeError('all routes failed: test: HTTPError')
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        with self.assertRaisesRegex(RuntimeError, 'all routes failed'):
            self.start()
        state_file = next((self.repo / '.git/soso/goals').glob('*/state.json'))
        report = json.loads(Path(json.loads(state_file.read_text())['active_epoch']).read_text())
        self.assertEqual(report['status'], 'blocked')
        self.assertTrue((Path(json.loads(state_file.read_text())['active_epoch']).parent / 'watched-config.json').is_file())
        exclude = self.repo / '.git/info/exclude'
        exclude.write_text(exclude.read_text() + 'scratch/\n')
        self.answer = original
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            resumed = self.start(resume_goal=state_file.parent.name)
        self.assertEqual(resumed['status'], 'incomplete')
        self.assertFalse((self.repo / '.git/soso/unrestored.json').exists())
        self.assertIn('scratch/', exclude.read_text())

    def test_completion_review_source_shows_implementation_and_tests_not_instructions(self):
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Tester')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.repo.joinpath('AGENTS.md').write_text('Project rules. ' * 300)
        self.repo.joinpath('tests').mkdir()
        self.repo.joinpath('tests/test_stage.py').write_text('# checks the stage file\n')
        git(self.repo, 'add', '--all')
        git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'base')
        def work(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('2')
            return {'finished': True}
        with patch('soso.engine.worker', side_effect=work):
            self.assertEqual(self.start()['status'], 'complete')
        final = [p for r, p in self.prompts if p.startswith('Perform independent final acceptance review.')]
        self.assertTrue(final)
        for prompt in final:
            names = [f['path'] for f in json.loads(prompt.split('\nSOURCE:\n')[1])]
            self.assertEqual(names, ['stage.txt', 'tests/test_stage.py'])  # not AGENTS.md, INTENT.md or .gitignore

    def test_completion_review_source_leaves_out_the_goal_config_file(self):
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Tester')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'custom.py').write_text(EVALUATOR)
        (self.repo / 'conf').mkdir()
        (self.repo / 'conf/goal.json').write_text(json.dumps({**self.config, 'evaluate': [sys.executable, '-B', 'custom.py']}))
        git(self.repo, 'add', '--all')
        git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'configured policy')
        def work(router, task, path, config, log, index):
            (Path(path) / 'stage.txt').write_text('2')
            return {'finished': True}
        with patch('soso.engine.worker', side_effect=work):
            self.assertEqual(self.start(config_path='conf/goal.json')['status'], 'complete')
        final = [p for r, p in self.prompts if p.startswith('Perform independent final acceptance review.')]
        self.assertTrue(final)
        for prompt in final:
            names = [f['path'] for f in json.loads(prompt.split('\nSOURCE:\n')[1])]
            self.assertEqual(names, ['custom.py', 'stage.txt'])  # not conf/goal.json

    def test_withheld_review_reason_reaches_the_worker(self):
        # A diff too large to show is not reviewed; the draft is kept and the next worker learns why.
        self.config['max_seconds'] = 600
        tasks = []
        def work(router, task, path, config, log, index):
            tasks.append(task)
            if len(tasks) == 1:
                (Path(path) / 'big.py').write_text('x = 1  # padding\n' * 150000)
                (Path(path) / 'stage.txt').write_text('1')
            return {'finished': True}
        with patch('soso.engine.worker', side_effect=work):
            first = self.start()
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                self.start(resume_goal=first['goal_id'])
        self.assertEqual(len(tasks), 2)
        self.assertIn('REVIEW REPAIR REQUIRED', tasks[1])
        self.assertIn('exceeds 2 MB', tasks[1])

    def test_interrupt_around_merge_preserves_or_reconciles_reviewed_draft(self):
        for after_merge in (False, True):
            with self.subTest(after_merge=after_merge):
                def work(router, task, path, config, log, index):
                    (Path(path) / 'stage.txt').write_text('2')
                    return {'finished': True}
                def interrupt(root, *args):
                    if args[:2] == ('merge', '--ff-only'):
                        if after_merge:
                            git(root, *args)
                        raise KeyboardInterrupt
                    return git(root, *args)
                with patch('soso.engine.worker', side_effect=work) as worker:
                    with patch('soso.delivery.git', side_effect=interrupt):
                        first = self.start()
                    self.assertEqual(first['status'], 'interrupted')
                    if after_merge:
                        self.assertIsNone(first['draft'])
                        self.assertEqual((Path(first['worktree']) / 'stage.txt').read_text(), '2')
                    else:
                        self.assertEqual(first['draft']['phase'], 'review')
                        self.assertFalse((Path(first['worktree']) / 'stage.txt').exists())
                    with patch('soso.delivery.time.time', return_value=time.time() + 5):
                        resumed = self.start(resume_goal=first['goal_id'])
                self.assertEqual(resumed['status'], 'complete')
                self.assertIsNone(resumed['draft'])
                self.assertEqual(worker.call_count, 1)

    def test_completion_requires_separate_reviewer(self):
        self.final_approve = False
        first = self.start()
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'incomplete')
        report = json.loads(Path(second['epochs'][-1]['report']).read_text())
        self.assertFalse(report['completion_review']['approve'])

    def test_goal_resume_checks_a_failed_restore_under_its_own_lock(self):
        # start_goal holds the run lock while it builds each epoch: the epoch's restore check must run, not be
        # refused by that same lock.
        first = self.start()
        marker = Path(git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')) / 'soso/unrestored.json'
        marker.write_text(json.dumps({'run': 'gone', 'file': 'config'}))
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
             self.assertRaisesRegex(ValueError, 'its record is gone'):  # past the retry delay
            self.start(resume_goal=first['goal_id'])
        state = json.loads((Path(first['report']).parent / 'state.json').read_text())
        self.assertEqual(state['status'], 'blocked')
        self.assertIn('its record is gone', state['reason'])  # the repair instructions, not a lock refusal

    def test_provider_wait_is_durable_and_resume_makes_no_calls_before_reset(self):
        retry = time.time() + 86400
        with patch('soso.delivery.configure_models', side_effect=ModelUnavailable(retry)) as discover:
            state = start_goal(self.repo, 'make game', once=True)
            resumed = start_goal(self.repo, once=True, resume_goal=state['goal_id'])
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(resumed['status'], 'waiting')
        self.assertEqual(resumed['next_retry_at'], retry)
        self.assertEqual(json.loads(Path(state['report']).read_text())['status'], 'waiting')

    def test_setup_protocol_failure_is_saved_and_resumable(self):
        original = self.answer
        def broken(*args, **kwargs):
            raise ModelProtocolError('local: invalid_schema after 3 attempts: milestones must be strings')
        self.answer = broken
        first = self.start()
        self.assertEqual(first['status'], 'blocked')
        self.assertIn('acceptance setup', first['reason'])
        self.assertIn('milestones must be strings', first['reason'])
        self.assertEqual(json.loads(Path(first['report']).read_text())['reason'], first['reason'])
        self.assertIsNone(first['next_retry_at'])
        self.answer = original
        second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['goal_id'], first['goal_id'])
        self.assertEqual(second['status'], 'incomplete')

    def test_contract_generation_quota_can_resume(self):
        original = self.answer
        def unavailable(*args, **kwargs):
            raise ModelUnavailable(time.time() + 1)
        self.answer = unavailable
        first = self.start()
        self.assertEqual(first['status'], 'waiting')
        self.answer = original
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'incomplete')

    def test_state_keeps_paths_that_contain_a_secret_value(self):
        self.repo = (Path(self.tmp.name) / 'postgres-tool').resolve()
        self.repo.mkdir()
        with patch.dict(os.environ, {'PGPASSWORD': 'postgres'}):
            first = self.start()
            state = json.loads(Path(first['report']).read_text())
            self.assertEqual(state['source'], str(self.repo))
            self.assertIn('postgres-tool', state['worktree'])
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'complete')

    def test_policy_tampering_and_head_change_are_blocked(self):
        state = self.start()
        root = Path(state['worktree'])
        (root / '.soso-delivery/evaluate.py').write_text('print(100)')
        git(root, 'add', '--all')
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'tamper')
        with self.assertRaisesRegex(ValueError, 'HEAD changed'):
            self.start(resume_goal=state['goal_id'])

    def test_policy_tampering_with_recorded_head_is_blocked(self):
        # Same recorded HEAD and clean status: only the frozen policy fingerprint detects the change.
        state = self.start()
        root = Path(state['worktree'])
        (root / '.soso-delivery/evaluate.py').write_text('print(100)')
        git(root, 'add', '--all')
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'tamper')
        saved = json.loads(Path(state['report']).read_text())
        saved['head'] = git(root, 'rev-parse', 'HEAD')
        Path(state['report']).write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, 'goal acceptance policy changed'):
            self.start(resume_goal=state['goal_id'])

    def test_approving_review_that_leaves_files_is_not_integrated(self):
        original = self.answer
        def answer(router, role, prompt, cwd, **kwargs):
            if role == 'reviewer' and prompt.startswith('Independently review this increment.'):
                (Path(cwd) / 'reviewer-scratch.txt').write_text('left behind')
            return original(router, role, prompt, cwd, **kwargs)
        self.answer = answer
        with self.assertRaisesRegex(ValueError, 'review modified accepted source'):
            self.start()
        state = json.loads(next((self.repo / '.git/soso/goals').glob('*/state.json')).read_text())
        self.assertFalse((Path(state['worktree']) / 'stage.txt').exists())

    def test_prepared_contract_evaluator_is_verified_before_recovery(self):
        for tamper in (False, True):
            with self.subTest(tamper=tamper):
                def interrupt(root, *args):
                    if 'Freeze goal acceptance contract' in args:
                        raise KeyboardInterrupt
                    return git(root, *args)
                with patch('soso.delivery.git', side_effect=interrupt):
                    first = self.start()
                self.assertEqual(first['status'], 'interrupted')
                self.assertNotIn('policy', first)
                evaluator = Path(first['worktree']) / '.soso-delivery/evaluate.py'
                if tamper:
                    evaluator.write_text(EVALUATOR.replace('stage>=2', 'stage>=0'))
                    with self.assertRaisesRegex(ValueError, 'prepared acceptance evaluator changed'):
                        self.start(resume_goal=first['goal_id'])
                else:
                    recovered = self.start(resume_goal=first['goal_id'])
                    self.assertIn('.soso-delivery/evaluate.py', recovered['policy'])
                    self.assertIn('Freeze goal acceptance contract', git(Path(recovered['worktree']), 'log', '--format=%s'))

    def test_dirty_source_is_preserved(self):
        self.start()
        (self.repo / 'keep.txt').write_text('pending')
        with self.assertRaisesRegex(ValueError, 'dirty'):
            self.start()
        self.assertEqual((self.repo / 'keep.txt').read_text(), 'pending')

    def test_default_intent_and_cli(self):
        with patch('soso.delivery.start_goal', return_value={'status': 'incomplete'}) as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 2)
            self.assertIsNone(call.call_args.args[1])
            self.assertEqual(main(['make game', '--once', '--no-exec', '--model', 'exact', '--models', 'pool.json']), 2)
            self.assertEqual(call.call_args.args[1], 'make game')
            self.assertFalse(call.call_args.kwargs['allow_exec'])
            self.assertEqual(call.call_args.kwargs['model'], 'exact')
            self.assertEqual(call.call_args.kwargs['models_path'], 'pool.json')

    def test_evaluation_rechecks_wall_budget(self):
        config = {**self.config, 'evaluate': ['unused']}
        budget = Budget(1, 20)
        def late(*a, **k):
            budget.deadline = time.monotonic() - 1
            return {'returncode': 0, 'timed_out': False, 'output': '{"score":1}'}
        with patch('soso.engine.execute', side_effect=late), self.assertRaises(BudgetExceeded):
            evaluate(self.repo, config, budget)

    def test_unknown_reset_uses_backoff(self):
        with patch('soso.delivery.configure_models', side_effect=ModelUnavailable()):
            before = time.time()
            state = start_goal(self.repo, 'game', once=True)
        self.assertGreaterEqual(state['next_retry_at'], before + 30)
        self.assertLess(state['next_retry_at'], before + 35)

    def test_provider_reset_that_is_always_now_still_backs_off(self):
        def unavailable(*args):
            raise ModelUnavailable(time.time())
        with patch('soso.delivery.configure_models', side_effect=unavailable):
            first = start_goal(self.repo, 'game', once=True)
            later = time.time() + 5
            with patch('soso.delivery.time.time', return_value=later):
                second = start_goal(self.repo, once=True, resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'waiting')
        self.assertEqual(second['failures'], 2)
        self.assertEqual(second['next_retry_at'], later + 30)

    def test_interrupt_wait_preserves_future_reset(self):
        retry = time.time() + 86400
        with patch('soso.delivery.configure_models', side_effect=ModelUnavailable(retry)):
            state = start_goal(self.repo, 'game', once=True)
        with patch('soso.delivery.time.sleep', side_effect=KeyboardInterrupt) as sleep, \
             patch('soso.delivery.configure_models') as model:
            result = start_goal(self.repo, resume_goal=state['goal_id'])
        model.assert_not_called()
        self.assertLessEqual(sleep.call_args.args[0], 1)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['next_retry_at'], retry)

    def test_worker_cannot_rewrite_contract(self):
        original = self.answer
        def rewrite(router, role, prompt, cwd, attempt=0, task=None):
            if role == 'worker':
                return {'tool': 'write', 'path': '.soso-delivery/evaluate.py', 'content': 'print(100)'}
            return original(router, role, prompt, cwd, attempt)
        self.answer = rewrite
        state = self.start()
        self.assertEqual(state['status'], 'blocked')
        self.assertEqual((Path(state['worktree']) / '.soso-delivery/evaluate.py').read_text(), EVALUATOR)
        report = json.loads(Path(state['epochs'][0]['report']).read_text())
        self.assertEqual(report['status'], 'execution_failed')
        self.assertEqual(report['plateau'], 0)
        self.assertFalse(report['rounds'][0]['accepted'])

    def test_contract_invalid_python_is_repaired_before_workers(self):
        original = self.answer
        count = 0
        def repair(router, role, prompt, cwd, attempt=0, task=None):
            nonlocal count
            if role == 'planner' and 'Create a small acceptance contract' in prompt:
                count += 1
                if count == 1:
                    return {'milestones': ['make'], 'evaluator': 'invalid python!'}
            return original(router, role, prompt, cwd, attempt)
        self.answer = repair
        result = self.start()
        self.assertEqual(count, 2)
        self.assertEqual(result['status'], 'incomplete')

    def test_contract_path_review_repairs_before_freeze_and_implementation(self):
        original = self.answer
        planned, reviews, worker_calls = 0, [], 0
        bad = EVALUATOR.replace("p=Path('stage.txt')", "p=Path(__file__).parent / 'stage.txt'")
        def checked(router, role, prompt, cwd, attempt=0, task=None):
            nonlocal planned, worker_calls
            if role == 'planner' and 'Create a small acceptance contract' in prompt:
                planned += 1
                self.assertIn('.soso-delivery/evaluate.py', prompt)
                self.assertIn('cwd equal to the project root', prompt)
                if planned == 1:
                    router.budget.claim()
                    return {'milestones': ['scaffold', 'finish game'], 'evaluator': bad}
                self.assertIn('policy directory instead of project root', prompt)
            if role == 'reviewer' and 'proposed acceptance contract BEFORE' in prompt:
                router.budget.claim()
                self.assertFalse((Path(cwd) / '.soso-delivery/contract.json').exists())
                self.assertEqual(worker_calls, 0)
                self.assertIn('EXECUTION CWD (PROJECT ROOT):\n' + str(cwd), prompt)
                self.assertIn('BASELINE:', prompt)
                self.assertIn('loopback-only app servers', prompt)
                approve = "p=Path(__file__).parent / 'stage.txt'" not in prompt
                reviews.append(approve)
                return {'approve': approve, 'reason': 'application path uses policy directory instead of project root' if not approve else 'cwd path and independent stage assertions are sound'}
            if role == 'worker':
                worker_calls += 1
                self.assertEqual(reviews, [False, True])
            return original(router, role, prompt, cwd, attempt, task)
        self.answer = checked
        first = self.start()
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(planned, 2)
        root = Path(first['worktree'])
        self.assertEqual((root / '.soso-delivery/evaluate.py').read_text(), EVALUATOR)
        history = [json.loads(line) for line in (Path(first['report']).parent / 'contract-reviews.jsonl').read_text().splitlines()]
        self.assertEqual([r['review']['approve'] for r in history], [False, True])
        self.assertEqual(history[0]['baseline']['score'], 0)
        self.assertIn('policy directory', history[0]['review']['reason'])
        self.assertTrue(first['contract']['acceptance_review']['review']['approve'])
        self.assertEqual(first['contract']['acceptance_review']['evaluator_sha256'], first['contract']['evaluator_sha256'])
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            complete = self.start(resume_goal=first['goal_id'])
        self.assertEqual(complete['status'], 'complete')
        self.assertEqual((root / 'stage.txt').read_text(), '2')

    def test_keyword_and_worker_pass_marker_oracle_is_rejected_without_freeze(self):
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Tester')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'index.html').write_text('<!-- game placeholder -->')
        (self.repo / 'test.js').write_text("console.log('PASS')")
        git(self.repo, 'add', '--all')
        git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'fake application')
        base = git(self.repo, 'rev-parse', 'HEAD')
        bad = """import json
from pathlib import Path
html=Path('index.html')
test=Path('test.js')
ok=html.exists() and test.exists() and 'game' in html.read_text() and 'PASS' in test.read_text()
print(json.dumps({'score':100 if ok else 0,'complete':ok}))
"""
        original = self.answer
        def reject(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'planner':
                return {'milestones': ['build game'], 'evaluator': bad}
            if role == 'reviewer':
                self.assertIn('HTML comments', prompt)
                self.assertIn('worker-owned test.js/test script', prompt)
                self.assertIn('independent assertions', prompt)
                self.assertIn('fake implementation could pass', prompt)
                return {'approve': False, 'reason': 'Keywords and worker-owned PASS marker do not exercise required behavior.'}
            self.fail('worker ran before independent acceptance approval')
        self.answer = reject
        incomplete = self.start()
        self.assertEqual(incomplete['status'], 'incomplete')
        self.assertEqual(incomplete['setup_repairs'], 1)
        self.assertEqual(incomplete['failures'], 0)
        self.assertGreater(incomplete['next_retry_at'], time.time())
        state_path = Path(incomplete['report'])
        state = json.loads(state_path.read_text())
        root = Path(state['worktree'])
        self.assertFalse((root / '.soso-delivery/contract.json').exists())
        self.assertEqual(git(root, 'rev-parse', 'HEAD'), base)
        records = [json.loads(line) for line in (state_path.parent / 'contract-reviews.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(r['baseline']['complete'] and not r['review']['approve'] for r in records))
        self.answer = original
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            resumed = self.start(resume_goal=state['goal_id'])
        self.assertEqual(resumed['status'], 'incomplete')
        repair_prompt = next(prompt for role, prompt in self.prompts if role == 'planner' and 'Create a small acceptance contract' in prompt)
        self.assertIn('PREVIOUS ACCEPTANCE REVIEW REJECTION', repair_prompt)
        self.assertIn('PASS marker', repair_prompt)

    def test_configured_evaluator_and_budget_preserved(self):
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Tester')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'custom.py').write_text(EVALUATOR)
        configured = {**self.config, 'evaluate': [sys.executable, '-B', 'custom.py']}
        (self.repo / 'soso.json').write_text(json.dumps(configured))
        git(self.repo, 'add', '--all')
        git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'configured policy')
        state = self.start()
        root = Path(state['worktree'])
        self.assertEqual(state['contract']['evaluate'], configured['evaluate'])
        self.assertFalse((root / '.soso-delivery/evaluate.py').exists())
        self.assertIn('custom.py', state['policy'])
        config = json.loads((Path(state['report']).parent / 'config.json').read_text())
        self.assertEqual(config['max_calls'], configured['max_calls'])
        self.assertIn('tests/*', config['protected'])
        self.assertIn('.github/*', config['protected'])

    def test_prompt_and_epoch_log_redact_environment_secrets(self):
        secret = 'secret-value-not-for-model'
        config = {'models': {'test': {'kind': 'command'}}, 'routes': {'worker': ['test']}}
        router = Router(config, Budget(1, 5), lambda *a, **k: None)
        with patch.dict(os.environ, {'TEST_API_KEY': secret}), patch.object(router, '_call', return_value='{"tool":"done"}') as call:
            router.ask('worker', 'value=' + secret, self.repo)
        self.assertNotIn(secret, call.call_args.args[1])

    def test_prompt_redacts_secret_escaped_by_embedded_json(self):
        # Prompts embed json.dumps'd data, where a secret with a quote or backslash appears escaped.
        config = {'models': {'test': {'kind': 'command'}}, 'routes': {'worker': ['test']}}
        for secret in ('quote"secret-value', 'back\\slash-secret', '\\edge-secret\\'):
            with self.subTest(secret=secret):
                router = Router(config, Budget(1, 5), lambda *a, **k: None)
                with patch.dict(os.environ, {'TEST_API_KEY': secret}), \
                        patch.object(router, '_call', return_value='{"tool":"done"}') as call:
                    router.ask('worker', 'data=' + json.dumps({'value': secret}), self.repo)
                sent = call.call_args.args[1]
                self.assertNotIn(json.dumps(secret)[1:-1], sent)
                self.assertEqual(sent, 'data={"value": "[REDACTED]"}')

    def test_setup_events_redact_environment_secrets(self):
        secret = 'setup"secret\\value'
        original = self.answer
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            if 'Create a small acceptance contract' in prompt:
                router.log('activity', task='uses ' + secret, **{'key ' + secret: 1})
            return original(router, role, prompt, cwd, attempt)
        self.answer = answer
        with patch.dict(os.environ, {'SOSO_TEST_SECRET': secret}):
            state = self.start()
        text = (Path(state['report']).parent / 'setup-events.jsonl').read_text()
        record = next(json.loads(line) for line in text.splitlines() if json.loads(line)['event'] == 'activity')
        self.assertNotIn(json.dumps(secret)[1:-1], text)
        self.assertEqual(record['task'], 'uses [REDACTED]')
        self.assertEqual(record['key [REDACTED]'], 1)

    def test_terminal_planning_or_review_error_is_recorded_in_epoch_report(self):
        original = self.answer
        for phase, error, message in (('plan', ValueError, 'planner returned no next step'),
                                      ('review', RuntimeError, 'all routes failed: test: FileNotFoundError')):
            with self.subTest(phase=phase):
                def answer(router, role, prompt, cwd, attempt=0, task=None):
                    if phase == 'plan' and role == 'planner' and 'Plan one small useful next milestone step' in prompt:
                        router.budget.claim()
                        return {'experiments': [{'task': ''}]}
                    if phase == 'review' and prompt.startswith('Independently review this increment.'):
                        raise RuntimeError(message)
                    return original(router, role, prompt, cwd, attempt)
                self.answer = answer
                with self.assertRaisesRegex(error, message):
                    self.start()
                state = json.loads(max(self.repo.glob('.git/soso/goals/*/state.json'), key=os.path.getmtime).read_text())
                self.assertEqual(state['status'], 'blocked')
                report = json.loads(Path(state['active_epoch']).read_text())
                self.assertEqual((report['status'], report.get('reason')), ('blocked', message))

    def test_interrupted_epoch_recovers_accepted_head(self):
        first = self.start()
        state_path = Path(first['report'])
        state = json.loads(state_path.read_text())
        report = json.loads(Path(first['epochs'][0]['report']).read_text())
        accepted = first['head']
        state['head'] = report['baseline']['head']
        state['active_epoch'] = first['epochs'][0]['report']
        state['epochs'] = []
        state['next_retry_at'] = time.time() + 100
        state_path.write_text(json.dumps(state))
        recovered = self.start(resume_goal=first['goal_id'])
        self.assertEqual(recovered['head'], accepted)
        self.assertEqual(recovered['epochs'][0]['status'], 'interrupted')

    def test_frozen_configuration_cannot_be_replaced_on_resume(self):
        state = self.start()
        config = Path(state['report']).parent / 'config.json'
        changed = json.loads(config.read_text())
        changed['evaluate'] = [sys.executable, '-c', 'print(100)']
        config.write_text(json.dumps(changed))
        with patch('soso.delivery.time.time', return_value=time.time() + 5), \
             self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.start(resume_goal=state['goal_id'])

    def test_zero_config_can_add_project_tests_and_ci(self):
        original = self.answer
        def add_tests(router, role, prompt, cwd, attempt=0, task=None):
            if role != 'worker':
                return original(router, role, prompt, cwd, attempt)
            last = json.loads(prompt.split('LAST RESULT:\n')[1])
            if 'files' in last:
                return {'tool': 'write', 'path': 'tests/test_project.py',
                        'content': 'import unittest\nclass GameTests(unittest.TestCase):\n    def test_start(self): self.assertEqual(1+1, 2)\n'}
            if last.get('path') == 'tests/test_project.py':
                return {'tool': 'write', 'path': '.github/workflows/ci.yml', 'content': 'name: tests\n'}
            return {'tool': 'done', 'summary': 'added tests and CI'}
        self.answer = add_tests
        state = self.start()
        root = Path(state['worktree'])
        self.assertTrue((root / 'tests/test_project.py').is_file())
        self.assertTrue((root / '.github/workflows/ci.yml').is_file())
        self.assertTrue(json.loads(Path(state['epochs'][0]['report']).read_text())['rounds'][0]['accepted'])
        self.assertIn('.soso-delivery/evaluate.py', state['policy'])
        self.assertNotIn('tests/*', state['protected'])
        self.assertNotIn('.github/*', state['protected'])

    def test_bare_intent_directory_initializes_without_git_prerequisite(self):
        (self.repo / 'INTENT.md').write_text('make a game')
        (self.repo / 'AGENTS.md').write_text('Keep changes small.')
        def configured(c, *a):
            return {**c, **self.config, **{k: c[k] for k in ('evaluate', 'protected', 'delivery') if k in c}}
        # Use an instruction-free worker response; this checks initial intent setup,
        # and keeps the source instruction file preserved in the source checkout.
        with patch('soso.delivery.configure_models', side_effect=configured), \
             patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)):
            state = start_goal(self.repo, once=True)
        self.assertEqual(state['intent'], 'make a game')
        self.assertEqual((self.repo / 'INTENT.md').read_text(), 'make a game')
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')

    def test_non_git_existing_source_is_not_silently_added(self):
        (self.repo / '.env').write_text('not-a-secret-fixture')
        with self.assertRaisesRegex(ValueError, 'initialize Git and commit'):
            start_goal(self.repo, 'game', once=True)
        self.assertFalse((self.repo / '.git').exists())

    def test_external_model_pool_is_frozen_and_survives_file_removal_on_resume(self):
        pool_path = Path(self.tmp.name) / 'models.json'
        routing = {'rules': [{'contains': ['game'], 'models': ['large']}],
                   'selector': {'model': 'classifier', 'routes': {'game': ['large']}}}
        pool = {'models': {'small': {'kind': 'command', 'command': ['false'], 'cost': 0},
                           'large': {'kind': 'command', 'command': ['false'], 'cost': 1},
                           'classifier': {'kind': 'command', 'command': ['false'], 'roles': []}},
                'routing': routing}
        pool_path.write_text(json.dumps(pool))
        def configured(c, *a):
            preserve = ('models', 'routes', 'routing', 'evaluate', 'protected', 'delivery', '_router_state')
            return {**c, **self.config, **{k: c[k] for k in preserve if k in c}}
        with patch('soso.delivery.configure_models', side_effect=configured), \
             patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)), \
             patch.dict(os.environ, {'SOSO_MODELS_FILE': str(pool_path)}):
            state = start_goal(self.repo, 'make game', once=True)
            config_path = Path(state['report']).parent / 'config.json'
            frozen = json.loads(config_path.read_text())
            self.assertEqual(set(frozen['models']), {'small', 'large', 'classifier'})
            self.assertEqual(frozen['routing'], routing)
            self.assertEqual(frozen['routes']['worker'], ['small', 'large'])
            self.assertEqual(frozen['_router_state'], str(Path(state['report']).parent / 'router-state.json'))
            pool_path.unlink()
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                resumed = start_goal(self.repo, once=True, resume_goal=state['goal_id'])
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(resumed['models_path'], str(pool_path.resolve()))
        self.assertEqual(config_path.read_text(), json.dumps(frozen, indent=2) + '\n')


    def test_fallback_identity_is_not_written_to_source_git_config(self):
        # A host without any Git identity: the runner must commit without editing the user's config.
        no_identity = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_CONFIG_COUNT': '1',
                       'GIT_CONFIG_KEY_0': 'user.useConfigOnly', 'GIT_CONFIG_VALUE_0': 'true'}
        with patch.dict(os.environ, no_identity), patch.dict('soso.process.IDENTITY', clear=True):
            first = self.start()
            self.assertEqual(first['status'], 'incomplete')
            with patch('soso.delivery.time.time', return_value=time.time() + 5):
                second = self.start(resume_goal=first['goal_id'])
            self.assertEqual(second['status'], 'complete')
            for key in ('user.name', 'user.email'):
                self.assertNotEqual(execute(['git', 'config', '--get', key], self.repo)['returncode'], 0)
            self.assertEqual(git(Path(second['worktree']), 'log', '-1', '--format=%an <%ae>'), 'Local Builder <builder@localhost>')

    def test_losing_concurrent_start_leaves_no_orphan_goal(self):
        import fcntl
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '--allow-empty', '-m', 'base')
        (self.repo / '.git/soso').mkdir()
        with (self.repo / '.git/soso/lock').open('w') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, 'another soso controller owns this repository'):
                self.start()
        self.assertEqual(list((self.repo / '.git/soso/goals').glob('*')), [])
        self.assertEqual(git(self.repo, 'branch', '--list', 'soso/*'), '')
        self.assertEqual(git(self.repo, 'worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_model_pool_replaces_project_routing_destinations(self):
        git(self.repo, 'init', '-b', 'main')
        (self.repo / 'evaluate.py').write_text(EVALUATOR)
        (self.repo / 'soso.json').write_text(json.dumps({
            **{k: self.config[k] for k in ('rounds', 'patience', 'parallel', 'max_steps', 'max_calls', 'max_seconds')},
            'evaluate': [sys.executable, 'evaluate.py'],
            'models': {'project-only': {'kind': 'command', 'command': ['false']}},
            'routes': {'worker': ['project-only']}, 'routing': {'default': ['project-only']}}))
        git(self.repo, 'add', '--all')
        git(self.repo, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-m', 'project policy')
        pool_path = Path(self.tmp.name) / 'models.json'
        pool_path.write_text(json.dumps({'models': {'test': {'kind': 'command', 'command': ['false']}}}))
        with patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)), \
             patch.dict(os.environ, {'SOSO_MODELS_FILE': str(pool_path), 'SOSO_LOCAL_ONLY': ''}):
            state = start_goal(self.repo, 'make game', once=True)
        frozen = json.loads((Path(state['report']).parent / 'config.json').read_text())
        self.assertNotIn('routing', frozen)
        self.assertEqual(set(frozen['models']), {'test'})


class RoutingTests(unittest.TestCase):
    def test_discovery_prefers_loaded_model_and_strong_planner(self):
        with patch('soso.delivery.ollama_models', return_value=['loaded', 'disk']), patch('soso.delivery.shutil.which', return_value='/bin/tool'):
            config = configure_models({})
        self.assertEqual(config['models']['local']['model'], 'loaded')
        self.assertEqual(config['models']['local-2']['model'], 'disk')
        self.assertEqual(config['routes']['worker'][0], 'local')
        self.assertEqual(config['routes']['planner'][0], 'codex')


    def test_pool_derives_roles_priority_cost_and_preserves_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models.json'
            models = {'premium': {'kind': 'codex', 'priority': 1, 'cost': 10},
                      'cheap': {'kind': 'codex', 'priority': 1, 'cost': 0},
                      'first': {'kind': 'codex', 'priority': 0, 'cost': 100, 'roles': ['worker']},
                      'classifier': {'kind': 'codex', 'roles': []}}
            routing = {'selector': {'model': 'classifier', 'routes': {'code': ['cheap']}}}
            path.write_text(json.dumps({'models': models, 'routing': routing}))
            pool = load_model_pool(path)
            self.assertEqual(pool['routes']['worker'], ['first', 'cheap', 'premium'])
            self.assertEqual(pool['routes']['planner'], ['cheap', 'premium'])
            self.assertEqual(pool['routing'], routing)
            path.write_text(json.dumps({'models': models, 'routes': {'worker': ['premium', 'cheap']}, 'routing': routing}))
            self.assertEqual(load_model_pool(path)['routes'], {'worker': ['premium', 'cheap']})
            path.write_text(json.dumps({'models': models, 'routing': []}))
            with self.assertRaisesRegex(ValueError, 'routing must be an object'):
                load_model_pool(path)

    def test_explicit_backend_does_not_retain_stale_pool_destinations(self):
        pool = {'models': {'old': {'kind': 'codex'}}, 'routes': {'worker': ['old']},
                'routing': {'default': ['old']}}
        with patch('soso.delivery.ollama_models', return_value=['loaded']):
            overridden = configure_models(pool, backend='ollama')
        self.assertEqual(overridden['routes']['worker'], ['local'])
        self.assertNotIn('routing', overridden)
        self.assertIn('routing', pool)

    def test_exact_missing_model_never_downloads_or_substitutes(self):
        with patch('soso.delivery.ollama_models', return_value=['other']):
            with self.assertRaises(ModelUnavailable):
                configure_models({}, model='exact')

    def test_config_models_honored_and_local_only(self):
        config = {'models': {'custom': {}}, 'routes': {'worker': ['custom']}}
        self.assertEqual(configure_models(config), config)
        with patch('soso.delivery.ollama_models', return_value=['loaded']), patch.dict(os.environ, {'SOSO_LOCAL_ONLY': '1'}):
            self.assertEqual(list(configure_models({})['models']), ['local'])

    def test_schema_failure_falls_through(self):
        config = {'delivery': True, 'models': {'a': {'kind': 'command'}, 'b': {'kind': 'command'}}, 'routes': {'worker': ['a', 'b']}}
        router = Router(config, Budget(3, 5), lambda *a, **k: None)
        with patch.object(router, '_call', side_effect=['{"answer":"oops"}', '{"tool":"done"}']):
            self.assertEqual(router.ask('worker', 'test', Path.cwd())['tool'], 'done')
        self.assertEqual(router.budget.calls, 2)

    def test_quota_cli_and_retry_after(self):
        now = time.time()
        self.assertAlmostEqual(retry_timestamp('30', now), now + 30)
        self.assertAlmostEqual(retry_timestamp(formatdate(now + 86400, usegmt=True), now), now + 86400, delta=1)
        self.assertIsNone(cli_unavailable('ordinary failure at 2026-10-15T12:00:00Z'))
        self.assertAlmostEqual(cli_unavailable('Usage limit reached; retry in 2 hours').retry_at, now + 7200, delta=1)

    def test_http_429_safe_metadata_and_503(self):
        statuses = [429, 503]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = {'models': [{'name': 'loaded' if self.path == '/api/ps' else 'disk'}]}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(statuses.pop(0))
                self.send_header('Retry-After', '86400')
                self.end_headers()
                self.wfile.write(b'secret-provider-body')
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(ollama_models(url), ['loaded', 'disk'])
            config = {'models': {'local': {'kind': 'ollama', 'url': url, 'model': 'test'}}, 'routes': {'worker': ['local']}}
            for _ in range(2):
                with self.assertRaises(ModelUnavailable) as raised:
                    Router(config, Budget(2, 5), lambda *a, **k: None).ask('worker', 'test', Path.cwd())
                self.assertGreater(raised.exception.retry_at, time.time() + 86000)
                self.assertNotIn('secret', str(raised.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
