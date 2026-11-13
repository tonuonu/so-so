import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from soso.cli import main


class CliTests(unittest.TestCase):
    def test_goal_exit_codes_match_the_launcher(self):
        # ./so-so and bootstrap.py exit 1 for a blocked goal; 2 means it can continue.
        for status, code in (('complete', 0), ('blocked', 1), ('incomplete', 2), ('waiting', 2)):
            with self.subTest(status=status), patch('soso.delivery.start_goal', return_value={'status': status}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['make game']), code)

    def test_malformed_config_is_a_one_line_error(self):
        for config in ([], {'evaluate': ['true'], 'models': {'m': 'x'}}, {'min_delta': 'a'}, {'checks': 5}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'soso.json'
                path.write_text(json.dumps(config))
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                    try:
                        code = main(['doctor', '--config', str(path)])
                    except Exception as exc:
                        self.fail(f'{type(exc).__name__} escaped: {exc}')
                self.assertEqual(code, 1)
                self.assertRegex(errors.getvalue(), r'^soso: [^\n]+\n$')
