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


if __name__ == '__main__':
    unittest.main()
