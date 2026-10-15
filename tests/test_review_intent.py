import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from soso.intent import resolve_pool
from soso.models import Router


INVENTORY = {
    'models': {
        'small': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'tiny'},
        'large': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'capable'},
    },
    'routes': {'worker': ['small', 'large']},
}

POLICY = {
    'aliases': {'worker': 'small', 'planner': 'large'},
    'routes': {'worker': ['worker'], 'planner': ['planner'], 'reviewer': ['planner']},
}

TEXT = 'Use tiny for ordinary coding and capable for planning and review.'


class IntentCompilerReviewTests(unittest.TestCase):
    def test_invalid_small_model_policy_reaches_next_actual_provider(self):
        calls = []

        def infer(router, model, *args):
            calls.append(model['model'])
            return json.dumps({'models': {}, 'routes': {}} if model['model'] == 'tiny' else POLICY)

        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=INVENTORY), \
                patch('soso.intent.shutil.which', return_value=None), \
                patch.object(Router, '_call', infer):
            pool = resolve_pool(TEXT, Path(folder))
        self.assertEqual(calls, ['tiny', 'capable'])
        self.assertEqual(pool['models']['worker']['model'], 'tiny')
        self.assertEqual(pool['models']['planner']['model'], 'capable')

    def check_repair(self, settings):
        bad = {**POLICY, 'settings': settings}
        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=INVENTORY), \
                patch('soso.intent.shutil.which', return_value=None), \
                patch.object(Router, 'ask', side_effect=[bad, POLICY]) as infer:
            pool = resolve_pool(TEXT, Path(folder))
            self.assertEqual([call.kwargs['attempt'] for call in infer.call_args_list], [0, 1])
            cached = list((Path(folder) / '.so-so' / 'pools').glob('*.json'))
            self.assertEqual(len(cached), 1)
            self.assertEqual(json.loads(cached[0].read_text())['models'], pool['models'])
            return pool

    def test_null_settings_are_repaired_instead_of_crashing(self):
        pool = self.check_repair(None)
        self.assertNotIn('parallel', pool['models']['worker'])

    def test_zero_parallel_is_repaired_instead_of_caching_unusable_model(self):
        pool = self.check_repair({'worker': {'parallel': 0}})
        self.assertNotIn('parallel', pool['models']['worker'])


if __name__ == '__main__':
    unittest.main()
