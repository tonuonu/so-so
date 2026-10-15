import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from soso.models import Router, Budget, ModelUnavailable


def config():
    return {'models': {n: {'kind': 'command', 'command': [n]} for n in ('qwen', 'fable', 'x', 'triage')},
            'routes': {'worker': ['qwen', 'x']},
            'routing': {'rules': [
                {'contains': ['design', 'branding'], 'not_contains': ['database'], 'models': ['fable']},
                {'role': 'worker', 'regex': r'\b(code|implement)\b', 'models': ['qwen', 'x']}],
                'default': ['x']}}


class RoutingPolicyTests(unittest.TestCase):
    def test_rules_match_task_not_protocol_and_default_is_explicit(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        calls = []
        def call(model, *args):
            calls.append(model['command'][0]); return '{"tool":"done"}'
        with patch.object(router, '_call', side_effect=call):
            for task in ['branding for a cafe', 'implement a parser', 'database design', 'unknown task']:
                router.ask('worker', 'protocol mentions branding and design', Path.cwd(), task=task)
        self.assertEqual(calls, ['fable', 'qwen', 'x', 'x'])

    def test_classifier_selects_named_route_and_invalid_output_uses_default(self):
        cfg = config()
        cfg['routing']['selector'] = {'model': 'triage', 'prompt': 'Visual tasks use visual.',
                                      'routes': {'visual': ['fable'], 'coding': ['qwen']}}
        router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
        calls = []
        def call(model, *args):
            name = model['command'][0]; calls.append(name)
            return '{"route":"visual"}' if name == 'triage' else '{"tool":"done"}'
        with patch.object(router, '_call', side_effect=call):
            router.ask('worker', 'packet', Path.cwd(), task='Make the screen look elegant')
        self.assertEqual(calls, ['triage', 'fable'])
        with patch.object(router, '_call', side_effect=['{"route":"invented"}', '{"tool":"done"}']) as mocked:
            router.ask('worker', 'packet', Path.cwd(), task='unclear')
        self.assertEqual(mocked.call_args_list[-1].args[0]['command'], ['x'])

    def test_quota_cooldown_persists_and_other_model_keeps_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(); cfg['routing'] = {'default': ['qwen', 'x']}
            cfg['_router_state'] = str(Path(tmp) / 'routing.json')
            until = time.time() + 3600
            router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
            calls = []
            def call(model, *args):
                name = model['command'][0]; calls.append(name)
                if name == 'qwen': raise ModelUnavailable(until)
                return '{"tool":"done"}'
            with patch.object(router, '_call', side_effect=call):
                router.ask('worker', 'p', Path.cwd())
                router.ask('worker', 'p', Path.cwd())
            self.assertEqual(calls, ['qwen', 'x', 'x'])
            restarted = Router(cfg, Budget(10, 30), lambda *a, **k: None)
            with patch.object(restarted, '_call', return_value='{"tool":"done"}') as mocked:
                restarted.ask('worker', 'p', Path.cwd())
            self.assertEqual(mocked.call_args.args[0]['command'], ['x'])
            self.assertEqual(restarted.cooldowns['qwen'], until)

    def test_all_routes_wait_until_earliest_reset_without_dispatch(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        router.cooldowns['x'] = time.time() + 60
        with patch.object(router, '_call') as mocked:
            with self.assertRaises(ModelUnavailable) as caught:
                router.ask('worker', 'unknown', Path.cwd())
        mocked.assert_not_called()
        self.assertEqual(caught.exception.retry_at, router.cooldowns['x'])

    def test_unknown_destination_is_rejected(self):
        cfg = config(); cfg['routing']['default'] = ['missing']
        with self.assertRaises(ValueError): Router(cfg, Budget(10, 30), lambda *a, **k: None)
