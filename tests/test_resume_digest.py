import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_core import RepoCase
from soso.cli import main
from soso.delivery import DeliveryEpoch, start_goal
from soso.models import Router
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


class DraftBindingDigestTests(unittest.TestCase):
    # start_goal rebuilds the epoch config in code on resume; pending_candidate refuses a retained
    # draft whose binding no longer matches, so this digest must change only on purpose.
    PINNED = '7e2e386663d58fc54337b0ee30c4bd2d87f204240f4deb64398f05fde6b3eaf2'

    def binding(self, folder):
        repo = Path(folder) / 'project'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        (repo / 'evaluate.py').write_text("print('{\"score\": 0, \"complete\": false}')\n")
        (repo / 'soso.json').write_text(json.dumps({
            'models': {'test': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['test']},
            'evaluate': ['python3', 'evaluate.py']}))
        git(repo, 'add', '--all')
        git(repo, 'commit', '-m', 'digest fixture')
        epochs = []
        def capture(epoch):
            epochs.append(epoch)
            raise KeyboardInterrupt
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            return {'milestones': ['observable step'], 'assumptions': []}
        env = {k: v for k, v in os.environ.items() if k not in ('SOSO_LOCAL_ONLY', 'SOSO_MODELS_FILE')}
        with patch.dict(os.environ, env, clear=True), patch.object(Router, 'ask', answer), \
                patch.object(DeliveryEpoch, 'run_epoch', capture):
            state = start_goal(repo, 'Improve value', once=True)
            start_goal(repo, once=True, resume_goal=state['goal_id'])
        bindings = []
        for epoch in epochs:
            # The router state lives in the goal folder; it is the only path in the config.
            self.assertEqual(epoch.config['_router_state'], str(Path(state['report']).parent / 'router-state.json'))
            epoch.config['_router_state'] = 'GOAL/router-state.json'
            bindings.append(epoch.draft_binding()['config_sha256'])
        return bindings

    def test_delivery_draft_binding_digest_is_pinned(self):
        digests = []
        for _ in range(2):  # different temporary paths and goal ids
            with tempfile.TemporaryDirectory() as folder:
                started, resumed = self.binding(folder)
            self.assertEqual(started, resumed)
            digests.append(resumed)
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[0], self.PINNED,
                         'The config that delivery rebuilds on resume changed, so retained drafts from before this '
                         'change can no longer resume. Update PINNED only on purpose, and say so in the pull request.')


if __name__ == '__main__':
    unittest.main()
