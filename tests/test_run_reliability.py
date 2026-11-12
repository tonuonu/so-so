import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from soso.engine import DEFAULTS, Run
from soso.process import git


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Test')
        git(self.root, 'config', 'user.email', 'test@example.invalid')
        (self.root / '.gitignore').write_text('__pycache__/\n')
        (self.root / 'value.txt').write_text('0')
        (self.root / 'evaluate.py').write_text("import json\nfrom pathlib import Path\nprint(json.dumps({'score':int(Path('value.txt').read_text())}))\n")
        git(self.root, 'add', '--all')
        git(self.root, 'commit', '-m', 'fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **changes):
        return {**DEFAULTS, 'models': {'test': {'kind': 'command', 'command': ['false'], 'parallel': 2}},
                'routes': {'worker': ['test']}, 'evaluate': [sys.executable, 'evaluate.py'],
                'rounds': 3, 'patience': 1, 'max_steps': 5, 'max_calls': 40, **changes}


class RedactionTests(RepoCase):
    def test_secret_needing_json_escape_is_redacted_in_events(self):
        secret = 'ab"cd\\ef-secret'
        with patch.dict(os.environ, {'SOSO_TEST_SECRET': secret}):
            runner = Run(self.root, self.config(), 'Improve')
            runner.log('activity', task='uses ' + secret, **{'key ' + secret: 1})
        text = (runner.folder / 'events.jsonl').read_text()
        self.assertNotIn(secret, json.dumps(json.loads(text)))
        self.assertIn('[REDACTED]', json.loads(text)['task'])

    def test_numeric_secret_does_not_corrupt_checkpoint_numbers(self):
        with patch.dict(os.environ, {'SOSO_TEST_TOKEN': '1234567'}):
            runner = Run(self.root, self.config(), 'Improve')
            runner.integration = self.root
            runner.report = {'deadline_unix': 1234567.5, 'note': 'token 1234567'}
            runner.checkpoint()
        report = json.loads(runner.report_path.read_text())
        self.assertEqual(report['deadline_unix'], 1234567.5)
        self.assertEqual(report['note'], 'token [REDACTED]')

    def test_delivery_state_keeps_numbers_and_redacts_escaped_strings(self):
        from soso.delivery import _save
        secret = 'x"y\\z-secret'
        with patch.dict(os.environ, {'SOSO_TEST_SECRET': secret, 'SOSO_TEST_TOKEN': '1234567'}):
            _save(self.root, {'next_retry_at': 1234567.5, 'reason': secret})
        state = json.loads((self.root / 'state.json').read_text())
        self.assertEqual(state, {'next_retry_at': 1234567.5, 'reason': '[REDACTED]'})


def done(router, role, prompt, cwd, attempt=0, task=None):
    router.budget.claim()
    return {'tool': 'done'}


class BaselineTests(RepoCase):
    def run_quietly(self, runner):
        with patch('soso.models.Router.ask', done), contextlib.redirect_stdout(io.StringIO()):
            return runner.run()

    def test_baseline_failure_leaves_durable_error_report(self):
        runner = Run(self.root, self.config(evaluate=['no-such-binary-for-soso-test']), 'Improve')
        report = self.run_quietly(runner)
        self.assertEqual(report['status'], 'error')
        saved = json.loads(runner.report_path.read_text())
        self.assertEqual(saved['status'], 'error')
        self.assertIn('no-such-binary', saved['reason'])
        self.assertFalse(saved['baseline']['valid'])
        self.assertEqual(saved['branch'], 'soso/' + runner.run_id)

    def test_baseline_budget_exhaustion_is_durable(self):
        from soso.models import BudgetExceeded
        runner = Run(self.root, self.config(), 'Improve')
        with patch('soso.engine.evaluate', side_effect=BudgetExceeded('wall time exhausted')):
            report = self.run_quietly(runner)
        self.assertEqual(report['status'], 'budget_exhausted')
        self.assertEqual(json.loads(runner.report_path.read_text())['status'], 'budget_exhausted')

    def test_resume_remeasures_missing_or_invalid_baseline(self):
        runner = Run(self.root, self.config(rounds=1), 'Improve')
        with patch('soso.engine.evaluate', return_value={'valid': False, 'reason': 'evaluator failed'}):
            self.assertEqual(self.run_quietly(runner)['status'], 'invalid_baseline')
        resumed = self.run_quietly(Run(self.root, self.config(rounds=1), 'Improve', resume=runner.run_id))
        self.assertTrue(resumed['baseline']['valid'])
        self.assertEqual(resumed['best'], 0)
        self.assertEqual(resumed['status'], 'round_limit')


class IncompleteRoundTests(RepoCase):
    def answer(self, review_unavailable):
        from soso.models import ModelUnavailable
        def ask(router, role, prompt, cwd, attempt=0, task=None):
            if role == 'reviewer':
                if review_unavailable:
                    raise ModelUnavailable(None, 'reviewer offline')
                return {'approve': True}
            if '-c0-' in str(cwd):
                raise ModelUnavailable(None, 'worker offline')
            router.budget.claim()
            if 'LAST RESULT:\n{"files"' in prompt:
                return {'tool': 'write', 'path': 'value.txt', 'content': '1'}
            return {'tool': 'done'}
        return ask

    def run_round(self, runner, review_unavailable=False):
        with patch('soso.models.Router.ask', self.answer(review_unavailable)), contextlib.redirect_stdout(io.StringIO()):
            return runner.run()

    def test_valid_sibling_is_accepted_when_another_candidate_waits(self):
        runner = Run(self.root, self.config(parallel=2, rounds=1), 'Improve')
        report = self.run_round(runner)
        self.assertEqual(report['status'], 'waiting')
        self.assertEqual(report['best'], 1)
        self.assertEqual(report['rounds'][0]['outcome'], 'accepted')

    def test_unreviewed_round_is_not_consumed_and_is_reviewed_on_resume(self):
        config = self.config(parallel=2, rounds=1, routes={'worker': ['test'], 'reviewer': ['test']})
        runner = Run(self.root, config, 'Improve')
        report = self.run_round(runner, review_unavailable=True)
        self.assertEqual(report['status'], 'waiting')
        self.assertEqual(report['best'], 0)
        self.assertEqual(len(report['rounds']), 1)
        self.assertEqual(report['rounds'][0]['outcome'], 'incomplete')
        import time as clock
        later = clock.time() + 60
        with patch('soso.engine.time.time', side_effect=lambda: later):
            resumed = self.run_round(Run(self.root, config, 'Improve', resume=runner.run_id))
        self.assertEqual(resumed['best'], 1)
        self.assertEqual(resumed['rounds'][0]['outcome'], 'accepted')
        self.assertEqual(resumed['status'], 'round_limit')
        self.assertEqual(git(runner.integration, 'show', 'HEAD:value.txt'), '1')

    def test_interrupted_round_keeps_its_record(self):
        runner = Run(self.root, self.config(parallel=2), 'Improve')
        with patch('soso.engine.concurrent.futures.as_completed', side_effect=KeyboardInterrupt):
            report = self.run_round(runner)
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual([r['outcome'] for r in report['rounds']], ['incomplete'])


class OptimizationEpochTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'project'
        self.root.mkdir()
        self.intent = 'Reduce the numeric value while preserving behavior.'
        (self.root / 'INTENT.md').write_text(self.intent)
        (self.root / 'value.txt').write_text('200')
        (self.root / 'evaluate.py').write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "if not Path(os.environ.get('SOSO_TEST_GATE', '/nonexistent')).exists(): sys.exit(1)\n"
            "print(json.dumps({'score':int(Path('value.txt').read_text())}))\n")
        self.gate = Path(self.tmp.name) / 'gate'
        self.gate.write_text('open')
        self.pool = {'models': {'test': {'kind': 'command', 'command': ['false']}},
                     'routes': {r: ['test'] for r in ('worker', 'planner', 'reviewer')}}
        self.config = {**DEFAULTS, **self.pool, 'direction': 'min', 'evaluate': ['python3', '-B', 'evaluate.py'],
                       'parallel': 1, 'rounds': 3, 'patience': 1, 'max_steps': 5, 'max_calls': 30,
                       'max_seconds': 30, 'mode': 'optimize',
                       'prepared_intent_sha256': hashlib.sha256(self.intent.encode()).hexdigest()}

    def tearDown(self):
        self.tmp.cleanup()

    def answer(self, router, role, prompt, cwd, attempt=0, task=None):
        router.budget.claim()
        if role == 'planner':
            return {'experiments': [{'task': 'reduce value'}]}
        if role == 'reviewer':
            return {'approve': True}
        if 'LAST RESULT:\n{"files"' in prompt:
            return {'tool': 'write', 'path': 'value.txt', 'content': '100'}
        return {'tool': 'done'}

    def launch(self, waits=None):
        from soso.launch import launch
        (self.root / 'soso.json').write_text(json.dumps(self.config))
        def wait(until):
            if waits is not None:
                waits.append(until)
                if len(waits) > 6:
                    raise AssertionError('optimization kept opening zero-progress epochs')
        with patch.dict(os.environ, {'SOSO_TEST_GATE': str(self.gate)}), \
             patch('soso.launch.resolve_pool', return_value=self.pool), \
             patch('soso.models.Router.ask', lambda router, *a, **k: self.answer(router, *a, **k)), \
             patch('soso.optimize._wait', side_effect=wait), contextlib.redirect_stdout(io.StringIO()):
            return launch(self.root)

    def test_epochs_without_a_completed_round_stop_blocked(self):
        self.config['max_calls'] = 1  # planning consumes the whole budget of every epoch
        waits = []
        result = self.launch(waits)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('before completing a round', result['reason'])
        self.assertEqual(len(result['epochs']), 3)
        self.assertTrue(all(e['status'] == 'budget_exhausted' for e in result['epochs']))

    def test_invalid_baseline_epoch_is_not_resumed(self):
        self.gate.unlink()
        first = self.launch()
        self.assertEqual(first['status'], 'blocked')
        self.assertIsNone(first['run_id'])
        self.assertIn('invalid baseline', first['reason'])
        again = self.launch()
        self.assertEqual(again['status'], 'blocked')
        self.assertEqual(len(again['epochs']), 2)
        self.gate.write_text('open')
        fixed = self.launch()
        self.assertEqual(fixed['status'], 'optimized')
        self.assertNotEqual(fixed['run_id'], first['epochs'][0]['run_id'])
        report = json.loads(Path(fixed['report']).read_text())
        self.assertTrue(report['baseline']['valid'])
        self.assertEqual(report['best'], 100)


EXIT_CODES = {'plateau': 0, 'round_limit': 0, 'execution_failed': 1, 'budget_exhausted': 1, 'error': 1,
              'invalid_baseline': 1, 'waiting': 2, 'interrupted': 2}


class ExitCodeTests(unittest.TestCase):
    def test_run_command_exits_zero_only_for_completed_runs(self):
        from unittest.mock import Mock
        from soso.cli import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'INTENT.md').write_text('Improve')
            (root / 'soso.json').write_text(json.dumps({'models': {'m': {'kind': 'command', 'command': ['false']}},
                                                        'routes': {'worker': ['m']}, 'evaluate': ['true']}))
            for status, code in EXIT_CODES.items():
                runner = Mock(report_path=root / 'report.json')
                runner.run.return_value = {'status': status, 'baseline': {}}
                with self.subTest(status=status), patch('soso.cli.Run', return_value=runner), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['run', '--repo', folder]), code)

    def test_bootstrap_exits_zero_only_for_completed_runs(self):
        import importlib.util
        from unittest.mock import Mock
        spec = importlib.util.spec_from_file_location('host_bootstrap', Path(__file__).parents[1] / 'bootstrap.py')
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / 'self'
            repo.mkdir()
            (repo / 'INTENT.md').write_text('Improve')
            for status, code in EXIT_CODES.items():
                runner = Mock(report_path=repo / 'report.json')
                runner.run.return_value = {'status': status, 'baseline': {'valid': False}}
                with self.subTest(status=status), patch.object(bootstrap, 'self_project', return_value=repo), \
                     patch.object(bootstrap, 'command'), patch('soso.engine.load_config', return_value={'checks': []}), \
                     patch('soso.delivery.configure_models', side_effect=lambda c, **k: c), \
                     patch('soso.engine.Run', return_value=runner), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(bootstrap.main(['--workspace', folder]), code)


class DeterministicRouteFailureTests(unittest.TestCase):
    def router(self, model):
        from soso.models import Budget, Router
        return Router({'delivery': True, 'models': {'m': model}, 'routes': {'worker': ['m']}}, Budget(5, 30), lambda *a, **k: None)

    def envelope(self, value):
        return {'returncode': 0, 'timed_out': False, 'truncated': False, 'output': json.dumps(value)}

    def test_deterministic_failures_are_terminal_in_delivery(self):
        from soso.models import ModelUnavailable
        http = {'kind': 'openai', 'url': 'http://127.0.0.1:9/v1', 'model': 'x'}
        cases = {'context': (self.router({'kind': 'command', 'command': ['false'], 'context_bytes': 4}), None),
                 'missing key': (self.router({**http, 'api_key_env': 'SOSO_TEST_UNSET_KEY'}), None),
                 'HTTP 401': (self.router(http), self.envelope({'status': 401}))}
        for name, (router, response) in cases.items():
            with self.subTest(name), patch('soso.models.execute', return_value=response):
                with self.assertRaises(RuntimeError) as raised:
                    router.ask('worker', 'prompt', Path.cwd())
                self.assertNotIsInstance(raised.exception, ModelUnavailable)
                self.assertIn('all routes failed', str(raised.exception))

    def test_failed_cli_process_and_server_error_still_wait_in_delivery(self):
        from soso.models import ModelUnavailable
        with self.assertRaises(ModelUnavailable):
            self.router({'kind': 'command', 'command': ['false']}).ask('worker', 'prompt', Path.cwd())
        router = self.router({'kind': 'openai', 'url': 'http://127.0.0.1:9/v1', 'model': 'x'})
        with patch('soso.models.execute', return_value=self.envelope({'status': 522})), self.assertRaises(ModelUnavailable):
            router.ask('worker', 'prompt', Path.cwd())

    def test_unusable_success_body_is_bounded_protocol_failure(self):
        import http.server
        import threading
        from soso.models import ModelProtocolError
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body = b'<html>proxy login</html>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            router = self.router({'kind': 'openai', 'url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'x'})
            with self.assertRaises(ModelProtocolError):
                router.ask('worker', 'prompt', Path.cwd())
            self.assertEqual(router.budget.calls, 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_delivery_blocks_with_reason_instead_of_waiting_forever(self):
        from soso.delivery import start_goal
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = Path(folder.name).resolve()
        config = {**DEFAULTS, 'models': {'tiny': {'kind': 'command', 'command': ['false'], 'context_bytes': 10}},
                  'routes': {r: ['tiny'] for r in ('worker', 'planner', 'reviewer')}, 'rounds': 1, 'parallel': 1,
                  'max_calls': 10, 'max_seconds': 15, 'protected': list(DEFAULTS['protected'])}
        def configured(c, *a):
            return {**c, **config, **{k: c[k] for k in ('evaluate', 'protected', 'delivery') if k in c}}
        with patch('soso.delivery.configure_models', side_effect=configured), self.assertRaises(RuntimeError) as raised:
            start_goal(repo / 'project', 'make a game', once=True)
        self.assertIn('context budget exceeded', str(raised.exception))
        state = json.loads(next((repo / 'project/.git/soso/goals').glob('*/state.json')).read_text())
        self.assertEqual(state['status'], 'blocked')
        self.assertIn('context budget exceeded', state['reason'])
        self.assertIsNone(state['next_retry_at'])


class QuotaTextTests(unittest.TestCase):
    def router(self, command, state=None):
        from soso.models import Budget, Router
        config = {'models': {'m': {'kind': 'command', 'command': command}}, 'routes': {'worker': ['m']}}
        if state:
            config['_router_state'] = str(state)
        return Router(config, Budget(5, 30), lambda *a, **k: None)

    def test_returned_answer_mentioning_rate_limits_is_not_a_cooldown(self):
        from soso.models import ModelProtocolError
        router = self.router(['m'])
        answer = 'The API docs say: rate limit exceeded, retry after 3 hours.'
        with patch.object(router, '_call', return_value=answer), self.assertRaises(ModelProtocolError):
            router.ask('worker', 'prompt', Path.cwd())
        self.assertEqual(router.cooldowns, {})

    def test_failed_process_quota_diagnostic_still_cools_down(self):
        import time
        from soso.models import ModelUnavailable
        router = self.router(['sh', '-c', 'echo "Usage limit reached; retry in 2 hours" >&2; exit 1'])
        with self.assertRaises(ModelUnavailable) as raised:
            router.ask('worker', 'prompt', Path.cwd())
        self.assertAlmostEqual(raised.exception.retry_at, time.time() + 7200, delta=30)


class FailureBackoffTests(unittest.TestCase):
    def test_success_resets_persisted_failure_backoff(self):
        import time
        from soso.models import Budget, ModelUnavailable, Router
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'router-state.json'
            state.write_text(json.dumps({'cooldowns': {}, 'failures': {'m': 6}}))
            config = {'models': {'m': {'kind': 'command', 'command': ['m']}}, 'routes': {'worker': ['m']},
                      '_router_state': str(state)}
            router = Router(config, Budget(5, 30), lambda *a, **k: None)
            with patch.object(router, '_call', return_value='{"tool":"done"}'):
                router.ask('worker', 'prompt', Path.cwd())
            restarted = Router(config, Budget(5, 30), lambda *a, **k: None)
            self.assertNotIn('m', restarted.failures)
            with patch.object(restarted, '_call', side_effect=ModelUnavailable()), self.assertRaises(ModelUnavailable):
                restarted.ask('worker', 'prompt', Path.cwd())
            self.assertLess(restarted.cooldowns['m'], time.time() + 60)


class LauncherFailureTests(unittest.TestCase):
    def test_failed_goal_is_recorded_as_blocked(self):
        from soso.launch import launch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / 'project'
            root.mkdir()
            (root / 'INTENT.md').write_text('Build a calculator.')
            pool = {'models': {'w': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['w']}}
            secret = 'launcher-secret-value'
            with patch.dict(os.environ, {'SOSO_TEST_SECRET': secret}), \
                 patch('soso.launch.resolve_pool', return_value=pool), \
                 patch('soso.launch.start_goal', side_effect=ValueError('invalid delivery baseline ' + secret)), \
                 self.assertRaisesRegex(ValueError, 'invalid delivery baseline'):
                launch(root)
            active = json.loads((root / '.so-so/active.json').read_text())
            entry = json.loads(next((root / '.so-so').rglob('entry.json')).read_text())
            for record in (active, entry):
                self.assertEqual(record['status'], 'blocked')
                self.assertIn('invalid delivery baseline', record['reason'])
                self.assertNotIn(secret, record['reason'])
                self.assertIsNone(record['next_retry_at'])
            done = {'goal_id': active['goal_id'], 'status': 'complete', 'worktree': str(root), 'report': str(root / 'r.json')}
            with patch('soso.launch.start_goal', return_value=done):
                launch(root)
            active = json.loads((root / '.so-so/active.json').read_text())
            self.assertEqual(active['status'], 'complete')
            self.assertIsNone(active['reason'])


class DashboardMalformedRecordTests(unittest.TestCase):
    def fixture(self, root, review, events):
        managed = root / '.so-so'
        project = managed / 'projects/demo/project'
        report = project / '.git/soso/run/report.json'
        report.parent.mkdir(parents=True)
        (managed / 'active.json').write_text(json.dumps({'project': str(project), 'report': str(report), 'status': 'running'}))
        (project / '.soso-delivery').mkdir()
        (project / '.soso-delivery/contract.json').write_text(json.dumps({'milestones': ['one']}))
        report.write_text(json.dumps({'best': 1, 'rounds': [{'accepted': 'a', 'experiments': [{'commit': 'a', 'review': review}]}]}))
        (report.parent / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))

    def test_snapshot_tolerates_malformed_reviews_and_call_events(self):
        from soso.dashboard import Monitor
        events = [{'event': 'call_started', 'time': 1, 'role': 'worker'},
                  {'event': 'call_started', 'call_id': 'b', 'time': 2},
                  {'event': 'call_finished', 'call_id': 'b', 'time': 3, 'seconds': 1, 'status': 'returned'}]
        for review in ({'approve': True, 'milestone_statuses': None}, 'approved', {'approve': True, 'milestone_statuses': 'all'}):
            with self.subTest(review=review), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.fixture(root, review, events)
                state = Monitor(root).snapshot()
                self.assertEqual(state['milestones'][0]['status'], 'not_reported')
                self.assertEqual(state['best'], 1)

    def test_unexpected_snapshot_error_is_500_and_unknown_route_is_404(self):
        import urllib.error
        import urllib.request
        from soso.dashboard import Dashboard, Monitor
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root, {}, [])
            with Dashboard(root) as dashboard, patch.object(Monitor, 'snapshot', side_effect=AttributeError('broken record')), \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(urllib.error.HTTPError) as failed:
                    urllib.request.urlopen(dashboard.url + 'api')
                self.assertEqual(failed.exception.code, 500)
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(dashboard.url + 'missing')
                self.assertEqual(missing.exception.code, 404)
            self.assertIn('broken record', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
