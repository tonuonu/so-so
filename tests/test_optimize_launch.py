import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from soso.delivery import start_goal
from soso.engine import DEFAULTS
from soso.launch import launch, main
from soso.models import ModelUnavailable, Router
from soso.process import git


class PreparedOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'project'
        self.root.mkdir()
        self.intent = 'Reduce the numeric value while preserving behavior.'
        (self.root / 'INTENT.md').write_text(self.intent)
        (self.root / 'value.txt').write_text('200')
        (self.root / 'evaluate.py').write_text("from pathlib import Path\nimport json\nprint(json.dumps({'score':int(Path('value.txt').read_text())}))\n")
        self.pool = {'models': {'test': {'kind': 'command', 'command': ['false']}},
                     'routes': {r: ['test'] for r in ('worker', 'planner', 'reviewer')}}
        self.config = {**DEFAULTS, **self.pool, 'direction': 'min', 'evaluate': ['python3', '-B', 'evaluate.py'],
                       'parallel': 1, 'rounds': 3, 'patience': 1, 'max_steps': 5, 'max_calls': 30,
                       'max_seconds': 30, 'mode': 'optimize',
                       'prepared_intent_sha256': hashlib.sha256(self.intent.encode()).hexdigest()}
        self.write_config()
        self.model_calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self):
        (self.root / 'soso.json').write_text(json.dumps(self.config))

    def answer(self, router, role, prompt, cwd, attempt=0, task=None):
        router.budget.claim()
        self.model_calls += 1
        if role == 'planner':
            return {'experiments': [{'task': 'reduce value if it is 200'}]}
        if role == 'reviewer':
            return {'approve': True}
        last = json.loads(prompt.split('LAST RESULT:\n')[1])
        if 'files' in last:
            return {'tool': 'read', 'path': 'value.txt'}
        if last.get('content') == '200':
            return {'tool': 'write', 'path': 'value.txt', 'content': '100'}
        return {'tool': 'done', 'summary': 'small improvement or no further improvement'}

    def run_goal(self, answer=None):
        with patch('soso.launch.resolve_pool', return_value=self.pool), \
             patch.object(Router, 'ask', lambda router, *a, **k: (answer or self.answer)(router, *a, **k)), \
             contextlib.redirect_stdout(io.StringIO()):
            return launch(self.root)

    def test_default_prepared_assignment_uses_unrestricted_score_and_stops_plateau(self):
        with patch('soso.launch.Path.cwd', return_value=self.root), \
             patch('soso.launch.resolve_pool', return_value=self.pool), \
             patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        active = json.loads((self.root / '.so-so/active.json').read_text())
        self.assertEqual(active['mode'], 'optimize')
        self.assertEqual(active['status'], 'optimized')
        self.assertIn('plateau', output.getvalue())
        report = json.loads(Path(active['report']).read_text())
        self.assertEqual(report['baseline']['score'], 200)
        self.assertEqual(report['best'], 100)
        self.assertEqual((Path(active['worktree']) / 'value.txt').read_text(), '100')
        self.assertEqual((self.root / 'value.txt').read_text(), '200')
        self.assertFalse((self.root / '.git').exists())
        with patch('soso.launch.resolve_pool', side_effect=AssertionError('must reuse resolved pool')), \
             patch.object(Router, 'ask', side_effect=AssertionError('must reuse optimized result')):
            cached = launch(self.root)
        self.assertEqual(cached['status'], 'optimized')
        self.assertEqual(cached['head'], report['head'])

    def test_round_limit_opens_distinct_epoch_at_accepted_head(self):
        self.config['rounds'] = 1
        self.write_config()
        with patch('soso.optimize._wait'):
            result = self.run_goal()
        self.assertEqual(result['status'], 'optimized')
        self.assertEqual(len(result['epochs']), 2)
        first, second = [json.loads(Path(e['report']).read_text()) for e in result['epochs']]
        self.assertEqual(first['status'], 'round_limit')
        self.assertEqual(second['base'], first['head'])
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(second['baseline']['score'], 100)
        self.assertEqual(git(Path(result['snapshot']), 'show', 'HEAD:value.txt'), '200')

    def test_non_improvement_patience_carries_across_short_epochs(self):
        self.config.update(rounds=1, patience=2)
        self.write_config()
        def stagnant(router, role, prompt, cwd, **kwargs):
            if role == 'worker':
                router.budget.claim()
                return {'tool': 'done'}
            return self.answer(router, role, prompt, cwd, **kwargs)
        waits = 0
        def bounded_wait(until):
            nonlocal waits
            waits += 1
            if waits > 2:
                self.fail('short epochs lost cumulative plateau progress')
        with patch('soso.optimize._wait', side_effect=bounded_wait):
            result = self.run_goal(stagnant)
        self.assertEqual(result['status'], 'optimized')
        self.assertEqual(result['plateau'], 2)
        self.assertEqual(len(result['epochs']), 2)
        self.assertEqual([e['plateau'] for e in result['epochs']], [1, 2])
        self.assertEqual([e['plateau_before'] for e in result['epochs']], [0, 1])

    def test_accepted_commit_resets_cumulative_patience(self):
        self.config.update(rounds=1, patience=2)
        self.write_config()
        current_epoch = 0
        def increment(router, role, prompt, cwd, **kwargs):
            nonlocal current_epoch
            if role == 'planner':
                current_epoch += 1
            if role == 'worker' and current_epoch == 1:
                router.budget.claim()
                return {'tool': 'done'}
            return self.answer(router, role, prompt, cwd, **kwargs)
        waits = 0
        def bounded_wait(until):
            nonlocal waits
            waits += 1
            if waits > 4:
                self.fail('accepted increment did not preserve bounded plateau progression')
        with patch('soso.optimize._wait', side_effect=bounded_wait):
            result = self.run_goal(increment)
        self.assertEqual(result['status'], 'optimized')
        self.assertEqual(len(result['epochs']), 4)
        self.assertEqual([e['plateau'] for e in result['epochs']], [1, 0, 1, 2])
        self.assertEqual(result['best'], 100)

    def test_quota_wait_resumes_same_run_without_renewing_original_budget(self):
        self.config['max_seconds'] = 120
        self.write_config()
        retry = time.time() + 60
        raised = False
        def quota(router, role, prompt, cwd, **kwargs):
            nonlocal raised
            if not raised:
                raised = True
                router.budget.claim()
                self.model_calls += 1
                raise ModelUnavailable(retry)
            return self.answer(router, role, prompt, cwd, **kwargs)
        with patch('soso.optimize._wait', side_effect=KeyboardInterrupt):
            first = self.run_goal(quota)
        self.assertEqual(first['status'], 'interrupted')
        self.assertEqual(first['next_retry_at'], retry)
        before = json.loads(Path(first['report']).read_text())
        calls = self.model_calls
        with patch('soso.optimize._wait', side_effect=KeyboardInterrupt):
            waiting = self.run_goal(quota)
        self.assertEqual(self.model_calls, calls)
        self.assertEqual(waiting['run_id'], first['run_id'])
        with patch('soso.optimize.time.time', return_value=retry + 1):
            completed = self.run_goal(quota)
        after = json.loads(Path(completed['report']).read_text())
        self.assertEqual(completed['status'], 'optimized')
        self.assertEqual(after['run_id'], before['run_id'])
        self.assertEqual(after['deadline_unix'], before['deadline_unix'])
        self.assertGreater(after['calls'], before['calls'])
        self.assertEqual(len(completed['epochs']), 1)

    def test_quota_longer_than_original_deadline_opens_explicit_new_budget_epoch(self):
        now = time.time()
        retry = now + 3600
        raised = False
        def quota(router, role, prompt, cwd, **kwargs):
            nonlocal raised
            if not raised:
                raised = True
                router.budget.claim()
                raise ModelUnavailable(retry)
            return self.answer(router, role, prompt, cwd, **kwargs)
        with patch('soso.optimize._wait', side_effect=KeyboardInterrupt):
            first = self.run_goal(quota)
        original = json.loads(Path(first['report']).read_text())
        clock = retry + 1
        def wait(until):
            nonlocal clock
            clock = max(clock, until)
        with patch('soso.optimize.time.time', side_effect=lambda: clock), patch('soso.optimize._wait', side_effect=wait):
            result = self.run_goal(quota)
        self.assertEqual(result['status'], 'optimized')
        self.assertEqual(len(result['epochs']), 2)
        resumed = json.loads(Path(result['epochs'][0]['report']).read_text())
        renewed = json.loads(Path(result['epochs'][1]['report']).read_text())
        self.assertEqual(resumed['status'], 'budget_exhausted')
        self.assertEqual(resumed['deadline_unix'], original['deadline_unix'])
        self.assertNotEqual(resumed['run_id'], renewed['run_id'])
        self.assertGreater(renewed['deadline_unix'], retry)

    def test_edited_intent_generates_delivery_oracle_without_stale_ast_checks(self):
        (self.root / 'INTENT.md').write_text('Build a different application.')
        (self.root / 'evaluate.py').write_text("raise AssertionError('STALE_AST_ORACLE_EXECUTED')\n")
        def delivery(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'planner':
                if 'Create a small acceptance contract' in prompt:
                    return {'milestones': ['build application'], 'evaluator': "import json\nprint(json.dumps({'score':0,'complete':False}))\n"}
                return {'experiments': [{'task': 'make next small increment'}]}
            return {'tool': 'done'}
        def once(project, intent, **kwargs):
            self.assertTrue(kwargs['ignore_project_config'])
            return start_goal(project, intent, once=True, **kwargs)
        with patch('soso.launch.resolve_pool', return_value=self.pool), \
             patch('soso.launch.start_goal', side_effect=once), \
             patch.object(Router, 'ask', delivery):
            result = launch(self.root)
        self.assertEqual(result['status'], 'incomplete')
        state = json.loads(Path(result['report']).read_text())
        config = json.loads((Path(result['report']).parent / 'config.json').read_text())
        self.assertTrue(state['ignore_project_config'])
        self.assertIn('.soso-delivery/evaluate.py', config['evaluate'])
        self.assertEqual(config['direction'], 'max')
        self.assertNotIn('checks', config)
        self.assertEqual(json.loads((self.root / '.so-so/active.json').read_text())['mode'], 'delivery')
