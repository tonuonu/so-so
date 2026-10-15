from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from soso.intent import resolve_pool
from soso.models import Router


class MarkdownPoolTests(unittest.TestCase):
    def test_plain_markdown_compiles_and_cache_needs_no_further_model(self):
        pool = {'models': {'qwen': {'kind': 'ollama', 'url': 'http://rtx:11434', 'model': 'qwen'},
                           'kimi': {'kind': 'kimi'}},
                'routes': {'worker': ['qwen', 'kimi'], 'planner': ['kimi'], 'reviewer': ['kimi']},
                'routing': {'rules': [{'contains': ['code'], 'models': ['qwen']}], 'default': ['kimi']}}
        text = 'Build a game. Use Qwen for code and Kimi for anything else.'
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', return_value=pool) as ask:
            result = resolve_pool(text, Path(tmp))
            self.assertEqual(result, pool)
            self.assertIn(text, ask.call_args.args[1])
            self.assertEqual(resolve_pool(text, Path(tmp)), pool)
            self.assertEqual(ask.call_count, 1)

    def test_invalid_destination_is_repaired_before_cache(self):
        pool = {'models': {'local': {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small'}},
                'routes': {'worker': ['local']}}
        bad = {**pool, 'routing': {'default': ['imaginary']}}
        with tempfile.TemporaryDirectory() as tmp, patch('soso.intent.configure_models', return_value=pool), \
                patch('soso.intent.shutil.which', return_value=None), patch.object(Router, 'ask', side_effect=[bad, pool]) as ask:
            self.assertEqual(resolve_pool('Make something useful', Path(tmp)), pool)
            self.assertEqual(ask.call_count, 2)
