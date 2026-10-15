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
from soso.models import Budget, BudgetExceeded, ModelUnavailable, Router, cli_unavailable, retry_timestamp
from soso.process import git


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

    def start(self, **kwargs):
        def configured(c, *a):
            return {**c, **self.config, **{k: c[k] for k in ('evaluate', 'protected', 'delivery') if k in c}}
        with patch('soso.delivery.configure_models', side_effect=configured), \
             patch.object(Router, 'ask', lambda router, *a, **k: self.answer(router, *a, **k)):
            return start_goal(self.repo, 'make a game', once=True, **kwargs)


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

    def test_completion_requires_separate_reviewer(self):
        self.final_approve = False
        first = self.start()
        with patch('soso.delivery.time.time', return_value=time.time() + 5):
            second = self.start(resume_goal=first['goal_id'])
        self.assertEqual(second['status'], 'incomplete')
        report = json.loads(Path(second['epochs'][-1]['report']).read_text())
        self.assertFalse(report['completion_review']['approve'])

    def test_provider_wait_is_durable_and_resume_makes_no_calls_before_reset(self):
        retry = time.time() + 86400
        with patch('soso.delivery.configure_models', side_effect=ModelUnavailable(retry)) as discover:
            state = start_goal(self.repo, 'make game', once=True)
            resumed = start_goal(self.repo, once=True, resume_goal=state['goal_id'])
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(resumed['status'], 'waiting')
        self.assertEqual(resumed['next_retry_at'], retry)
        self.assertEqual(json.loads(Path(state['report']).read_text())['status'], 'waiting')

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

    def test_policy_tampering_and_head_change_are_blocked(self):
        state = self.start()
        root = Path(state['worktree'])
        (root / '.soso-delivery/evaluate.py').write_text('print(100)')
        git(root, 'add', '--all')
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'tamper')
        with self.assertRaisesRegex(ValueError, 'HEAD changed'):
            self.start(resume_goal=state['goal_id'])

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
        self.assertEqual(state['status'], 'incomplete')
        self.assertEqual((Path(state['worktree']) / '.soso-delivery/evaluate.py').read_text(), EVALUATOR)
        report = json.loads(Path(state['epochs'][0]['report']).read_text())
        self.assertEqual(report['status'], 'plateau')
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
        with patch.dict(os.environ, {'TEST_API_KEY': secret}), patch.object(router, '_call', return_value='{"ok":true}') as call:
            router.ask('worker', 'value=' + secret, self.repo)
        self.assertNotIn(secret, call.call_args.args[1])

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
