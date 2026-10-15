import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from soso.intent import resolve_pool
from soso.models import ModelUnavailable, Router


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
            if args[0].startswith('Independently review this compiled model policy'):
                return json.dumps({'approve': True, 'reason': 'Faithful policy.'})
            return json.dumps({'models': {}, 'routes': {}} if model['model'] == 'tiny' else POLICY)

        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=INVENTORY), \
                patch('soso.intent.shutil.which', return_value=None), \
                patch.object(Router, '_call', infer):
            pool = resolve_pool(TEXT, Path(folder))
        self.assertEqual(calls, ['tiny', 'capable', 'tiny'])
        self.assertEqual(pool['models']['worker']['model'], 'tiny')
        self.assertEqual(pool['models']['planner']['model'], 'capable')

    def check_repair(self, settings):
        bad = {**POLICY, 'settings': settings}
        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=INVENTORY), \
                patch('soso.intent.shutil.which', return_value=None), \
                patch.object(Router, 'ask', side_effect=[bad, POLICY, {'approve': True}]) as infer:
            pool = resolve_pool(TEXT, Path(folder))
            self.assertEqual([call.kwargs['attempt'] for call in infer.call_args_list if call.args[0] == 'configuration'], [0, 1])
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


    def test_missing_explicit_classifier_fallback_is_rejected_then_repaired(self):
        inventory = {
            'models': {
                'tiny': {'kind': 'ollama', 'url': 'http://rtx:11434', 'model': 'tiny'},
                'qwen': {'kind': 'ollama', 'url': 'http://rtx:11434', 'model': 'Qwen'},
                'kimi': {'kind': 'kimi'},
            },
            'routes': {'worker': ['qwen', 'kimi']},
        }
        text = ('Build a TODO API. Use tiny as worker classifier on CPU with context 2048. '
                'Routine code uses Qwen with Kimi fallback; hard and other work uses Kimi. '
                'Qwen context is 8192. Planning and review go directly to Kimi; default is Kimi.')
        proposal = {
            'aliases': {n: n for n in inventory['models']},
            'settings': {'tiny': {'options': {'num_gpu': 0}, 'context_tokens': 2048},
                         'qwen': {'context_tokens': 8192}},
            'routes': {'worker': ['qwen', 'kimi'], 'planner': ['kimi'], 'reviewer': ['kimi']},
            'routing': {'rules': [{'role': r, 'models': ['kimi']} for r in ('planner', 'reviewer')],
                        'default': ['kimi'], 'selector': {'model': 'tiny', 'routes': {
                            'code': ['qwen'], 'hard': ['kimi'], 'other': ['kimi']}}},
        }
        calls = []
        def answer(router, role, prompt, cwd, **kwargs):
            calls.append(role)
            if role == 'configuration':
                result = json.loads(json.dumps(proposal))
                if calls.count(role) == 2:
                    self.assertIn('code must preserve ordered fallback [qwen, kimi]', prompt)
                    result['routing']['selector']['routes']['code'] = ['qwen', 'kimi']
                return result
            self.assertEqual(role, 'configuration_review')
            self.assertIn(text, prompt)
            self.assertIn('VERIFIED INVENTORY:', prompt)
            self.assertIn('CPU/GPU preferences, context and output limits', prompt)
            self.assertFalse(list((Path(cwd) / '.so-so/pools').glob('*.json')))
            compiled = json.loads(prompt.split('\nCOMPILED POLICY:\n')[1])
            approved = compiled['routing']['selector']['routes']['code'] == ['qwen', 'kimi']
            return {'approve': approved, 'reason': 'code must preserve ordered fallback [qwen, kimi]'}
        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=inventory), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', answer):
            pool = resolve_pool(text, Path(folder))
            self.assertEqual(pool['routing']['selector']['routes']['code'], ['qwen', 'kimi'])
            self.assertEqual(resolve_pool(text, Path(folder)), pool)
            reviews = [json.loads(line) for line in
                       (Path(folder) / '.so-so/pools/setup-events.jsonl').read_text().splitlines()]
        self.assertEqual(calls, ['configuration', 'configuration_review'] * 2)
        self.assertEqual([r['approve'] for r in reviews], [False, True])
        self.assertNotEqual(reviews[0]['policy_sha256'], reviews[1]['policy_sha256'])

    def test_invalid_generated_regex_is_repaired_before_review_and_cache(self):
        bad = {**POLICY, 'routing': {'rules': [{'regex': '[', 'models': ['worker']}]}}
        good = {**POLICY, 'routing': {'rules': [{'regex': r'\bcode\b', 'models': ['worker']}]}}
        calls = []
        def answer(router, role, prompt, cwd, **kwargs):
            calls.append(role)
            self.assertFalse(list((Path(cwd) / '.so-so/pools').glob('*.json')))
            if len(calls) == 1:
                return bad
            if len(calls) == 2:
                self.assertEqual(role, 'configuration')
                self.assertIn('REPAIR POLICY:', prompt)
                self.assertIn('unterminated character set', prompt)
                self.assertFalse(list((Path(cwd) / '.so-so/pools').glob('*.pending')))
                return good
            self.assertEqual(role, 'configuration_review')
            compiled = json.loads(prompt.split('\nCOMPILED POLICY:\n')[1])
            self.assertEqual(compiled['routing'], good['routing'])
            return {'approve': True, 'reason': 'Matches the requested coding route.'}
        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=INVENTORY), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', answer):
            pool = resolve_pool(TEXT, Path(folder))
            cached = list((Path(folder) / '.so-so/pools').glob('*.json'))
            self.assertEqual(len(cached), 1)
            self.assertEqual(json.loads(cached[0].read_text())['routing'], good['routing'])
            self.assertEqual(pool['routing'], good['routing'])
        self.assertEqual(calls, ['configuration', 'configuration', 'configuration_review'])

    def test_nonboolean_approval_never_caches_and_setup_stays_bounded(self):
        calls = []
        def infer(router, model, prompt, *args):
            review = prompt.startswith('Independently review this compiled model policy')
            calls.append(review)
            return json.dumps({'approve': 'true'} if review else POLICY)
        with tempfile.TemporaryDirectory() as folder, \
                patch('soso.intent.configure_models', return_value=INVENTORY), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, '_call', infer):
            with self.assertRaisesRegex(ModelUnavailable, 'model policy translation needs another attempt'):
                resolve_pool(TEXT, Path(folder))
            self.assertFalse(list((Path(folder) / '.so-so/pools').glob('*.json')))
            self.assertFalse(list((Path(folder) / '.so-so/pools').glob('*.pending')))
        self.assertEqual(calls, [False, True] * 3)


if __name__ == '__main__':
    unittest.main()
