import json
from pathlib import Path
import unittest
from unittest.mock import patch
from soso.models import Router, Budget, ModelUnavailable

class KimiAdapterTests(unittest.TestCase):
    def test_profile_disables_tools_and_final_answer_is_separated(self):
        router = Router({'models': {'kimi': {'kind': 'kimi'}}, 'routes': {'worker': ['kimi']}}, Budget(2, 30), lambda *a, **k: None)
        def execute(argv, *args, **kwargs):
            profile = Path(argv[argv.index('--agent-file') + 1]).read_text()
            self.assertIn('tools: []', profile)
            self.assertIn('subagents: []', profile)
            self.assertEqual(kwargs['env']['KIMI_MODEL_THINKING_EFFORT'], 'max')
            self.assertEqual(kwargs['env']['KIMI_MODEL_MAX_COMPLETION_TOKENS'], '8192')
            return {'returncode': 0, 'timed_out': False, 'truncated': False,
                    'output': '\n'.join(json.dumps(x) for x in [
                        {'role': 'meta', 'type': 'system.version'},
                        {'role': 'assistant', 'content': '{"tool":"done","summary":"ready"}'},
                        {'role': 'meta', 'content': 'resume instructions'}])}
        with patch('soso.models.execute', side_effect=execute):
            self.assertEqual(router.ask('worker', 'Return a done action', Path.cwd())['tool'], 'done')

    def test_quota_failure_becomes_wait(self):
        router = Router({'models': {'kimi': {'kind': 'kimi'}}, 'routes': {'worker': ['kimi']}}, Budget(2, 30), lambda *a, **k: None)
        with patch('soso.models.execute', return_value={'returncode':1,'timed_out':False,'truncated':False,
                                                       'output':'Usage limit reached. Retry in 2 hours.'}):
            with self.assertRaises(ModelUnavailable) as caught:
                router.ask('worker', 'Return JSON', Path.cwd())
        self.assertIsNotNone(caught.exception.retry_at)
