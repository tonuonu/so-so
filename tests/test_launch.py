import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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

    def test_history_identifies_command_line_intent_and_results(self):
        result = self.run_goal('Build a chess game')
        folder = Path(result['snapshot']).parent
        self.assertTrue(folder.name.startswith('build-a-chess-game--'))
        self.assertEqual((folder / 'INTENT.md').read_text(), 'Build a chess game')
        self.assertIn('Status: complete', (folder / 'README.md').read_text())
        self.assertIn('Result: `' + result['worktree'] + '`', (folder / 'README.md').read_text())
        history = (self.root / '.so-so/README.md').read_text()
        self.assertIn('Build a chess game', history)
        self.assertIn(folder.name, history)
        self.assertEqual(self.run_goal('Build a chess game')['snapshot'], result['snapshot'])

    def test_old_hash_snapshot_resumes_without_moving_git_worktrees(self):
        first = self.run_goal()
        folder = Path(first['snapshot']).parent
        manifest = json.loads((folder / 'manifest.json').read_text())
        legacy = folder.with_name(manifest['fingerprint'])
        folder.rename(legacy)
        entry = json.loads((legacy / 'entry.json').read_text())
        entry['project'] = str(legacy / 'project')
        (legacy / 'entry.json').write_text(json.dumps(entry))
        result = self.run_goal()
        self.assertEqual(result['snapshot'], str(legacy / 'project'))
        self.assertEqual(self.calls[-1][2]['resume_goal'], 'goal-1')

    def test_explicit_intent_file_uses_its_text_without_changing_default(self):
        original = (self.root / 'INTENT.md').read_text()
        example = self.root / 'examples/tetris/INTENT.md'
        example.parent.mkdir(parents=True)
        example.write_text('Create game tetris')
        with patch('soso.launch.Path.cwd', return_value=self.root), \
             patch('soso.launch.resolve_pool', return_value=self.pool), \
             patch('soso.launch.start_goal', side_effect=self.result), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--intent', './examples/tetris/INTENT.md']), 0)
        self.assertEqual(self.calls[0][1], 'Create game tetris')
        self.assertFalse((self.calls[0][0] / 'app.py').exists())
        folder = self.calls[0][0].parent
        self.assertEqual((folder / 'INTENT.md').read_text(), 'Create game tetris')
        self.assertEqual((self.root / 'INTENT.md').read_text(), original)

    def test_resume_last_uses_saved_intent_and_snapshot_after_source_changes(self):
        first = self.run_goal('Create game tetris')
        (self.root / 'INTENT.md').unlink()
        (self.root / 'app.py').write_text('changed after interruption')
        with patch('soso.launch.resolve_pool', side_effect=AssertionError('must keep saved pool')), \
             patch('soso.launch.start_goal', side_effect=self.result):
            resumed = launch(self.root, resume='')
        self.assertEqual(resumed['snapshot'], first['snapshot'])
        self.assertEqual(self.calls[-1][1], 'Create game tetris')
        self.assertEqual(self.calls[-1][2]['resume_goal'], 'goal-1')
        self.assertEqual((Path(resumed['snapshot'])/'app.py').read_text(), 'value = 1\n')
        self.assertEqual((self.root/'app.py').read_text(), 'changed after interruption')

    def test_explicit_resume_selects_older_task(self):
        first = self.run_goal('First task')
        self.run_goal('Second task')
        with patch('soso.launch.start_goal', side_effect=self.result):
            resumed = launch(self.root, resume=first['resume_id'])
        self.assertEqual(resumed['snapshot'], first['snapshot'])
        self.assertEqual(self.calls[-1][1], 'First task')

    def test_interrupt_prints_working_resume_command(self):
        with patch('soso.launch.Path.cwd', return_value=self.root), \
             patch('soso.launch.resolve_pool', return_value=self.pool), \
             patch('soso.launch.start_goal', side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['Create game tetris']), 2)
        self.assertIn('To continue, execute: ./so-so resume', output.getvalue())
        with patch('soso.launch.Path.cwd', return_value=self.root), \
             patch('soso.launch.resolve_pool', side_effect=AssertionError('must reuse pool')), \
             patch('soso.launch.start_goal', side_effect=self.result), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['resume']), 0)
        self.assertEqual(self.calls[-1][1], 'Create game tetris')

    def test_resume_rejects_missing_or_tampered_saved_task(self):
        with self.assertRaisesRegex(ValueError, 'no previous task'):
            launch(self.root, resume='')
        first = self.run_goal()
        with self.assertRaisesRegex(ValueError, 'not found'):
            launch(self.root, resume='unknown-task')
        with self.assertRaisesRegex(ValueError, 'invalid saved task'):
            launch(self.root, resume='../outside')
        (Path(first['snapshot']).parent/'INTENT.md').write_text('Changed saved intent')
        with self.assertRaisesRegex(ValueError, 'intent or fingerprint changed'):
            launch(self.root, resume=first['resume_id'])

    def test_changed_source_keeps_previous_snapshot(self):
        first = self.run_goal()
        (self.root / 'app.py').write_text('value = 2\n')
        second = self.run_goal()
        self.assertNotEqual(first['snapshot'], second['snapshot'])
        self.assertEqual((Path(first['snapshot']) / 'app.py').read_text(), 'value = 1\n')
        self.assertEqual((Path(second['snapshot']) / 'app.py').read_text(), 'value = 2\n')

    def trusted_runtime(self):
        checkout = Path(__file__).resolve().parents[1]
        runtime = Path(self.tmp.name) / 'trusted-runtime'
        shutil.copytree(checkout / 'soso', runtime / 'soso', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(checkout / 'so-so', runtime / 'so-so')
        # Providers are deterministic fixtures adjacent to the trusted runtime.
        # Source cwd and Python environment injection remain isolated by -I.
        delivery = runtime / 'soso/delivery.py'
        with delivery.open('a') as stream:
            stream.write(
                "\ndef start_goal(project,intent,**kwargs):\n"
                "    Path(project,'.git','test-intent').write_text(intent)\n"
                "    return {'goal_id':'subprocess-goal','status':'complete','worktree':str(project)}\n")
        (runtime / 'soso/intent.py').write_text('def resolve_pool(text,cwd):\n    return None\n')
        return runtime

    def test_actual_executable_accepts_intent_file(self):
        runtime = self.trusted_runtime()
        assignment = self.root / 'game.md'
        assignment.write_text('Create game tetris')
        result = subprocess.run([str(runtime / 'so-so'), '--intent', './game.md'], cwd=self.root,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        active = json.loads((self.root / '.so-so/active.json').read_text())
        self.assertEqual((Path(active['project']) / '.git/test-intent').read_text(), 'Create game tetris')

    def test_actual_executable_with_no_arguments_uses_intent(self):
        runtime = self.trusted_runtime()
        result = subprocess.run([str(runtime / 'so-so')], cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Goal complete.', result.stdout)
        entry = json.loads((self.root / '.so-so/active.json').read_text())
        snapshot = Path(entry['project'])
        self.assertEqual((snapshot / '.git/test-intent').read_text(), (self.root / 'INTENT.md').read_text())
        self.assertFalse((self.root / '.git').exists())

    def test_project_stdlib_shadow_is_not_executed_on_startup(self):
        runtime = self.trusted_runtime()
        (self.root / 'json.py').write_text("raise RuntimeError('PROJECT_JSON_EXECUTED')\n")
        (self.root / 'sitecustomize.py').write_text("raise RuntimeError('PROJECT_SITE_EXECUTED')\n")
        env = dict(os.environ, PYTHONPATH=str(self.root))
        result = subprocess.run([str(runtime / 'so-so')], cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Goal complete.', result.stdout)
        self.assertNotIn('PROJECT_JSON_EXECUTED', result.stdout + result.stderr)
        self.assertNotIn('PROJECT_SITE_EXECUTED', result.stdout + result.stderr)
        self.assertEqual((self.root / 'json.py').read_text(), "raise RuntimeError('PROJECT_JSON_EXECUTED')\n")

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
