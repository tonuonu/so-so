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


if __name__ == '__main__':
    unittest.main()
