import contextlib
import io
import json
from pathlib import Path
import unittest
from test_core import RepoCase
from soso.cli import main
from soso.process import git


class ResumeDigestTests(RepoCase):
    # `soso run --resume` rebuilds its config from soso.json and refuses a different digest.
    PINNED = 'fe87772e50e5393e97d0cb358db2338b627c0c99e3aea613c7b4f351d63ccd73'

    def test_soso_run_config_digest_is_pinned(self):
        (self.root / 'check.py').write_text('')
        (self.root / 'INTENT.md').write_text('Improve value')
        (self.root / 'soso.json').write_text(json.dumps({
            'models': {'test': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['test']},
            'evaluate': ['python3', '-c', 'raise SystemExit(1)'], 'checks': [['python3', 'check.py'], ['python3', 'evaluate.py']]}))
        git(self.root, 'add', '--all')
        git(self.root, 'commit', '-m', 'digest fixture')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(['run', '--repo', str(self.root)])
        report = json.loads(Path(json.loads(output.getvalue())['report']).read_text())
        self.assertEqual(report['config_sha256'], self.PINNED,
                         'The config that soso run prepares changed, so runs started before this change can no '
                         'longer resume. Update PINNED only on purpose, and say so in the pull request.')


if __name__ == '__main__':
    unittest.main()
