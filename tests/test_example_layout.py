"""Public examples stay minimal; regression inputs remain independently runnable."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from soso.cli import main
from soso.models import Router

ROOT = Path(__file__).resolve().parents[1]


class ExampleLayoutTests(unittest.TestCase):
    def test_public_example_contains_only_intent(self):
        files = sorted(str(p.relative_to(ROOT / 'examples')) for p in (ROOT / 'examples').rglob('*') if p.is_file())
        self.assertEqual(files, ['security-review/INTENT.md', 'tetris/INTENT.md'])
        self.assertIn('game', (ROOT / 'examples/tetris/INTENT.md').read_text())

    def test_default_trial_preserves_configured_routes(self):
        spec = importlib.util.spec_from_file_location('example_trials', ROOT / 'scripts/run_examples.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pool = {'models': {'chosen': {'kind': 'command', 'command': ['false']}},
                'routes': {'worker': ['chosen']}}
        configurations = []
        class Trial:
            def __init__(self, repo, config, intent):
                configurations.append(config)
                self.report_path = repo / 'report.json'
            def run(self):
                return {'baseline': {'score': 0}, 'best': 1, 'status': 'round_limit', 'calls': 0,
                        'rounds': [], 'branch': 'test'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'resolve_pool', return_value=pool), \
             patch.object(module, 'Run', Trial), patch('sys.argv', ['run_examples.py', '--output', tmp + '/trials']), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertEqual(len(configurations), 3)
        self.assertTrue(all(c['models'] == pool['models'] and c['routes'] == pool['routes'] for c in configurations))

    def test_probe_requests_valid_role_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text(json.dumps({'models': {'test': {'kind': 'command', 'command': ['false']}},
                                         'routes': {'worker': ['test']}, 'evaluate': ['false']}))
            def reply(router, model, prompt, cwd, timeout):
                return prompt.removeprefix('Return exactly ').removesuffix('. Do not execute tools.')
            for role in ('worker', 'reviewer', 'planner', 'coordinator', 'architect'):
                with patch.object(Router, '_call', reply), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['probe', '--config', str(config), '--role', role]), 0)
