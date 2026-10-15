import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from soso.launch import launch, main
from soso.models import ModelUnavailable
from soso.process import git


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'project'
        self.root.mkdir()
        (self.root / 'INTENT.md').write_text('Build a calculator. Use the model described here.')
        (self.root / 'app.py').write_text('value = 1\n')
        self.pool = {'models': {'worker': {'kind': 'command', 'command': ['false']}},
                     'routes': {'worker': ['worker']}}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, project, intent, **kwargs):
        self.calls.append((Path(project), intent, kwargs))
        return {'goal_id': kwargs.get('resume_goal') or 'goal-1', 'status': 'complete',
                'worktree': str(project), 'report': str(project / 'report.json')}

    def run_goal(self, goal=None):
        with patch('soso.launch.resolve_pool', return_value=self.pool), patch('soso.launch.start_goal', side_effect=self.result):
            return launch(self.root, goal)

    def test_no_flags_reads_intent_snapshots_and_passes_internal_pool(self):
        with patch('soso.launch.Path.cwd', return_value=self.root), \
             patch('soso.launch.resolve_pool', return_value=self.pool) as model, \
             patch('soso.launch.start_goal', side_effect=self.result), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        project, intent, args = self.calls[0]
        self.assertEqual(intent, (self.root / 'INTENT.md').read_text())
        self.assertEqual((project / 'app.py').read_text(), 'value = 1\n')
        self.assertTrue(project.is_relative_to(self.root / '.so-so'))
        self.assertEqual(json.loads(Path(args['models_path']).read_text()), self.pool)
        model.assert_called_once_with(intent, self.root)
        self.assertIn('Goal complete.', output.getvalue())
        self.assertFalse((self.root / '.git').exists())
        self.assertEqual((self.root / 'app.py').read_text(), 'value = 1\n')

    def test_same_inputs_resume_and_changed_intent_gets_distinct_snapshot(self):
        first = self.run_goal()
        second = self.run_goal()
        self.assertEqual(first['snapshot'], second['snapshot'])
        self.assertEqual(self.calls[-1][2]['resume_goal'], 'goal-1')
        (self.root / 'INTENT.md').write_text('Build a different app.')
        third = self.run_goal()
        self.assertNotEqual(first['snapshot'], third['snapshot'])
        self.assertIsNone(self.calls[-1][2]['resume_goal'])
        self.assertTrue(Path(first['snapshot']).is_dir())
        self.assertTrue(Path(third['snapshot']).is_dir())
        active = json.loads((self.root / '.so-so/active.json').read_text())
        self.assertEqual(active['project'], third['snapshot'])

    def test_changed_source_keeps_previous_snapshot(self):
        first = self.run_goal()
        (self.root / 'app.py').write_text('value = 2\n')
        second = self.run_goal()
        self.assertNotEqual(first['snapshot'], second['snapshot'])
        self.assertEqual((Path(first['snapshot']) / 'app.py').read_text(), 'value = 1\n')
        self.assertEqual((Path(second['snapshot']) / 'app.py').read_text(), 'value = 2\n')

    def test_actual_executable_with_no_arguments_uses_intent(self):
        checkout = Path(__file__).resolve().parents[1]
        hooks = Path(self.tmp.name) / 'test-runtime'
        hooks.mkdir()
        (hooks / 'sitecustomize.py').write_text(
            "import sys,types\nfrom pathlib import Path\nimport soso.delivery\n"
            "def done(project,intent,**kwargs):\n"
            "    Path(project,'.git','test-intent').write_text(intent)\n"
            "    return {'goal_id':'subprocess-goal','status':'complete','worktree':str(project)}\n"
            "soso.delivery.start_goal=done\n"
            "sys.modules['soso.intent']=types.SimpleNamespace(resolve_pool=lambda text,cwd:None)\n")
        env = dict(os.environ, PYTHONPATH=str(hooks) + os.pathsep + str(checkout))
        result = subprocess.run([str(checkout / 'so-so')], cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Goal complete.', result.stdout)
        entry = json.loads((self.root / '.so-so/active.json').read_text())
        snapshot = Path(entry['project'])
        self.assertEqual((snapshot / '.git/test-intent').read_text(), (self.root / 'INTENT.md').read_text())
        self.assertFalse((self.root / '.git').exists())

    def test_dirty_git_files_preserved_untracked_included_secrets_ignored(self):
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Test')
        git(self.root, 'config', 'user.email', 'test@example.invalid')
        (self.root / '.gitignore').write_text('ignored.txt\n')
        (self.root / 'tracked-ignored.txt').write_text('tracked despite ignore')
        git(self.root, 'add', '--all')
        git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'source')
        (self.root / '.gitignore').write_text('ignored.txt\ntracked-ignored.txt\n')
        (self.root / 'app.py').write_text('dirty change\n')
        (self.root / 'untracked.txt').write_text('include')
        (self.root / 'ignored.txt').write_text('ignore')
        (self.root / '.env').write_text('SENSITIVE=fixture-not-for-model')
        (self.root / 'private.key').write_text('private fixture')
        (self.root / 'keys').mkdir()
        (self.root / 'keys/key.txt').write_text('key fixture')
        base = git(self.root, 'rev-parse', 'HEAD')
        before = git(self.root, 'status', '--porcelain')
        result = self.run_goal()
        snapshot = Path(result['snapshot'])
        self.assertEqual((snapshot / 'app.py').read_text(), 'dirty change\n')
        self.assertTrue((snapshot / 'untracked.txt').is_file())
        self.assertTrue((snapshot / 'tracked-ignored.txt').is_file())
        self.assertIn('tracked-ignored.txt', git(snapshot, 'ls-files').splitlines())
        for name in ('ignored.txt', '.env', 'private.key', 'keys/key.txt'):
            self.assertFalse((snapshot / name).exists())
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), base)
        self.assertEqual(git(self.root, 'status', '--porcelain'), before)
        self.assertEqual((self.root / '.env').read_text(), 'SENSITIVE=fixture-not-for-model')

    def test_crash_after_goal_creation_recovers_goal_without_manual_id(self):
        first = self.run_goal()
        folder = Path(first['snapshot']).parent
        entry = json.loads((folder / 'entry.json').read_text())
        entry['goal_id'] = None
        (folder / 'entry.json').write_text(json.dumps(entry))
        goal_state = Path(first['snapshot']) / '.git/soso/goals/recovered-1/state.json'
        goal_state.parent.mkdir(parents=True)
        goal_state.write_text(json.dumps({'goal_id': 'recovered-1', 'source': first['snapshot'], 'status': 'running'}))
        with patch('soso.launch.resolve_pool') as model:
            self.run_goal()
        self.assertEqual(self.calls[-1][2]['resume_goal'], 'recovered-1')
        model.assert_not_called()

    def test_setup_quota_wait_persists_before_sleep_and_recovers(self):
        retry = time.time() + 86400
        with patch('soso.launch.resolve_pool', side_effect=ModelUnavailable(retry)), \
             patch('soso.launch._wait', side_effect=KeyboardInterrupt) as wait, \
             patch('soso.launch.start_goal') as worker:
            result = launch(self.root)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['next_retry_at'], retry)
        wait.assert_called_once_with(retry)
        worker.assert_not_called()
        with patch('soso.launch.resolve_pool', return_value=self.pool) as resolver, \
             patch('soso.launch._wait', side_effect=KeyboardInterrupt):
            resumed = launch(self.root)
        resolver.assert_not_called()
        self.assertEqual(resumed['next_retry_at'], retry)

    def test_explicit_goal_needs_no_intent_file(self):
        (self.root / 'INTENT.md').unlink()
        self.run_goal('Build a game.')
        self.assertEqual(self.calls[0][1], 'Build a game.')
        self.assertFalse((self.root / 'INTENT.md').exists())

    def test_optional_goal_keeps_markdown_model_settings(self):
        with patch('soso.launch.resolve_pool', return_value=self.pool) as resolver, \
             patch('soso.launch.start_goal', side_effect=self.result):
            launch(self.root, 'Build a game instead.')
        text = resolver.call_args.args[0]
        self.assertIn((self.root / 'INTENT.md').read_text(), text)
        self.assertIn('Build a game instead.', text)
        self.assertEqual(self.calls[-1][1], 'Build a game instead.')

    def test_existing_unowned_state_is_preserved(self):
        state = self.root / '.so-so'
        state.mkdir()
        (state / 'mine.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            self.run_goal()
        self.assertEqual((state / 'mine.txt').read_text(), 'keep')

    def test_root_executable_has_no_setup_flags(self):
        script = Path(__file__).resolve().parents[1] / 'so-so'
        result = subprocess.run([str(script), '--help'], cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('INTENT.md', result.stdout)
        self.assertNotIn('--backend', result.stdout)
        self.assertNotIn('--models', result.stdout)
