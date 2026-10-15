from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from soso.intent import resolve_pool
from soso.models import Router


APPROVAL = {'approve': True, 'reason': 'Policy preserves the Markdown requirements.'}


class MarkdownPoolTests(unittest.TestCase):
    def test_plain_markdown_compiles_and_cache_needs_no_further_model(self):
        pool = {'models': {'qwen': {'kind': 'ollama', 'url': 'http://rtx:11434', 'model': 'qwen'},
                           'kimi': {'kind': 'kimi'}},
                'routes': {'worker': ['qwen', 'kimi'], 'planner': ['kimi'], 'reviewer': ['kimi']},
                'routing': {'rules': [{'contains': ['code'], 'models': ['qwen']}], 'default': ['kimi']}}
        text = 'Build a game. Use Qwen for code and Kimi for anything else.'
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', side_effect=[pool, APPROVAL]) as ask:
            result = resolve_pool(text, Path(tmp))
            self.assertEqual(result, pool)
            self.assertIn(text, ask.call_args.args[1])
            self.assertEqual(resolve_pool(text, Path(tmp)), pool)
            self.assertEqual(ask.call_count, 2)

    def test_invalid_destination_is_repaired_before_cache(self):
        pool = {'models': {'local': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small'}},
                'routes': {'worker': ['local']}}
        bad = {**pool, 'routing': {'default': ['imaginary']}}
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', side_effect=[bad, pool, APPROVAL]) as ask:
            self.assertEqual(resolve_pool('Make something useful', Path(tmp)), pool)
            self.assertEqual(ask.call_count, 3)

    def test_invented_adapter_is_rejected_and_next_model_is_tried(self):
        from soso.models import ModelUnavailable
        pool = {'models': {'local': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small'}},
                'routes': {'worker': ['local']}}
        invented = {'models': {'new': {'kind': 'command', 'command': ['sh', '-c', 'echo unexpected']}},
                    'routes': {'worker': ['new']}}
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', side_effect=[invented, pool, APPROVAL]) as ask:
            result = resolve_pool('Build a calculator with the available local models', Path(tmp))
            self.assertEqual(result, pool)
            self.assertEqual([c.kwargs['attempt'] for c in ask.call_args_list if c.args[0] == 'configuration'], [0, 1])

    def test_alias_policy_copies_verified_adapter_and_applies_cpu_tuning(self):
        pool = {'models': {'detected': {'kind': 'ollama', 'url': 'http://rtx:11434', 'model': 'small'}},
                'routes': {'worker': ['detected']}}
        answer = {'aliases': {'router': 'detected'}, 'routes': {'worker': ['router']},
                  'settings': {'router': {'options': {'num_gpu': 0}}}}
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', side_effect=[answer, APPROVAL]):
            result = resolve_pool('Use small on RTX for routing', Path(tmp))
            self.assertEqual(result['models']['router']['url'], 'http://rtx:11434')
            self.assertEqual(result['models']['router']['options'], {'num_gpu': 0})


class ExplicitProjectPolicyTests(unittest.TestCase):
    def test_explicit_policy_bypasses_discovery_and_intent_cache(self):
        import json
        from soso.models import Budget
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {'use_project_model_policy': True,
                      'models': {'main': {'kind': 'ollama', 'model': 'main-model'}},
                      'routes': {'worker': ['main']}}
            (root / 'soso.json').write_text(json.dumps(policy))
            with patch('soso.intent.configure_models') as discover, patch.object(Router, 'ask') as ask:
                self.assertEqual(resolve_pool('first assignment', root)['routes']['worker'], ['main'])
                policy['models']['main']['model'] = 'updated-main-model'
                (root / 'soso.json').write_text(json.dumps(policy))
                self.assertEqual(resolve_pool('different assignment', root)['models']['main']['model'], 'updated-main-model')
                discover.assert_not_called()
                ask.assert_not_called()
                self.assertFalse((root / '.so-so').exists())

    def test_invalid_explicit_policy_fails_without_silent_discovery(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'soso.json').write_text(json.dumps({'use_project_model_policy': True, 'models': {}}))
            with patch('soso.intent.configure_models') as discover:
                with self.assertRaises(ValueError):
                    resolve_pool('assignment', root)
                discover.assert_not_called()
