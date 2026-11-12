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


if __name__ == '__main__':
    unittest.main()
