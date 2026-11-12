import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch
from test_core import RepoCase
from soso.cli import main
from soso.engine import Run
from soso.models import Router
from soso.workspace import Workspace


class SandboxIntegrityTests(RepoCase):
    def test_git_dir_is_rejected_in_any_case(self):
        # Case-insensitive filesystems (macOS default) map .GIT onto .git.
        ws = Workspace(self.root, [])
        for name in ('.GIT', '.Git/config', 'sub/.gIt/HEAD'):
            with self.assertRaises(ValueError):
                ws.path(name)

    def test_protection_matches_normalised_names(self):
        ws = Workspace(self.root, ['scripts/*'])
        for name in ('./scripts/x.py', 'scripts//x.py'):
            with self.assertRaises(PermissionError):
                ws.tool({'tool': 'write', 'path': name, 'content': ''})

    def test_candidate_is_evaluated_without_ignored_files(self):
        # Ignored files are never committed, so they must not influence the accepted score.
        code = ("import json\nfrom pathlib import Path\nbonus=Path('data/bonus.txt')\n"
                "print(json.dumps({'score':int(Path('value.txt').read_text())+(int(bonus.read_text()) if bonus.exists() else 0)}))\n")
        steps = iter([{'tool': 'write', 'path': 'data/.gitignore', 'content': '*\n'},
                      {'tool': 'write', 'path': 'data/bonus.txt', 'content': '999'},
                      {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                      {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            if role == 'planner': return {'experiments': [{'task': 'improve value'}]}
            return next(steps)
        runner = Run(self.root, self.config(evaluate=[sys.executable, '-c', code], parallel=1, rounds=1), 'Improve')
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        experiment = report['rounds'][0]['experiments'][0]
        self.assertTrue(experiment.get('commit'), experiment)
        self.assertEqual(experiment['evaluation']['score'], 0)
        self.assertEqual(report['best'], 0)

    def test_run_protects_in_repo_evaluator_and_check_programs(self):
        (self.root / 'bench').mkdir()
        (self.root / 'bench/score.py').write_text("print('{\"score\":0}')\n")
        (self.root / 'check.py').write_text('')
        (self.root / 'INTENT.md').write_text('Improve value')
        (self.root / 'soso.json').write_text(json.dumps({
            'models': {'test': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['test']},
            'evaluate': ['python3', 'bench/score.py'], 'checks': [['python3', './check.py']]}))
        captured = {}
        class FakeRun:
            report_path = self.root / 'report.json'
            def __init__(self, root, config, intent, allow_exec=False, resume=None):
                captured.update(config)
            def run(self):
                return {'status': 'plateau'}
        with patch('soso.cli.Run', FakeRun), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--repo', str(self.root)]), 0)
        self.assertIn('bench/score.py', captured['protected'])
        self.assertTrue(Workspace(self.root, captured['protected']).is_protected('check.py'))


if __name__ == '__main__':
    unittest.main()
