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


    def test_sentence_punctuation_is_not_part_of_endpoint_and_unreachable_endpoint_is_logged(self):
        import json
        from soso.models import ModelUnavailable
        text = 'Ollama runs on http://127.0.0.1:11439. Also try http://gpu:11434, then decide.'
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', side_effect=ModelUnavailable()), \
                patch('soso.intent.shutil.which', return_value=None), \
                patch('soso.intent.ollama_models', return_value=[]) as probe:
            with self.assertRaises(ModelUnavailable):
                resolve_pool(text, Path(tmp))
            self.assertEqual([c.args[0] for c in probe.call_args_list], ['http://127.0.0.1:11439', 'http://gpu:11434'])
            events = [json.loads(line) for line in (Path(tmp) / '.so-so/pools/setup-events.jsonl').read_text().splitlines()]
            self.assertEqual([(e['event'], e['url']) for e in events],
                             [('endpoint_unavailable', 'http://127.0.0.1:11439'), ('endpoint_unavailable', 'http://gpu:11434')])

    def test_local_only_never_sends_the_intent_to_a_cli_adapter(self):
        import json, os
        pool = {'models': {'local': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small'}},
                'routes': {'worker': ['local']}}
        sent = []
        def call(router, model, prompt, cwd, timeout):
            sent.append((model['kind'], prompt))
            return json.dumps(APPROVAL if 'Independently review this compiled model policy' in prompt else pool)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SOSO_LOCAL_ONLY': '1'}), \
                patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value='/usr/local/bin/kimi'), \
                patch.object(Router, '_call', call):
            self.assertEqual(resolve_pool('Private plan: build the ledger', Path(tmp)), pool)
        self.assertTrue(sent)
        self.assertEqual({kind for kind, _ in sent}, {'ollama'})

    def test_pool_compiled_with_every_adapter_is_not_reused_under_local_only(self):
        import os
        pool = {'models': {'local': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small'}},
                'routes': {'worker': ['local']}}
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), \
                patch.object(Router, 'ask', side_effect=[pool, APPROVAL, pool, APPROVAL]) as ask:
            for local in ('', '1', '', '1'):
                with patch.dict(os.environ, {'SOSO_LOCAL_ONLY': local}):
                    self.assertEqual(resolve_pool('Use the small model', Path(tmp)), pool)
            self.assertEqual(ask.call_count, 4)  # compiled once per mode, then each reused
            self.assertEqual(len(list((Path(tmp) / '.so-so/pools').glob('*.json'))), 2)

    def test_setup_events_redact_environment_secrets(self):
        import json, os
        from soso.models import ModelUnavailable
        secret = 'tok-abcdef123'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SOSO_TEST_TOKEN': secret}), \
                patch('soso.intent.configure_models', side_effect=ModelUnavailable()), \
                patch('soso.intent.shutil.which', return_value=None), patch('soso.intent.ollama_models', return_value=[]):
            with self.assertRaises(ModelUnavailable):
                resolve_pool('Ollama runs on http://user:' + secret + '@gpu:11434 here.', Path(tmp))
            text = (Path(tmp) / '.so-so/pools/setup-events.jsonl').read_text()
            self.assertNotIn(secret, text)
            self.assertEqual(json.loads(text)['url'], 'http://user:[REDACTED]@gpu:11434')


class ExplicitProjectPolicyTests(unittest.TestCase):
    def test_explicit_policy_bypasses_discovery_and_intent_cache(self):
        import json
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
