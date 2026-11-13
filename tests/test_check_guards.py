import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('check_guards', Path(__file__).parents[1] / 'scripts/check_guards.py')
check_guards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_guards)

SAMPLE = '''import os, signal, unittest
import guarded
class T(unittest.TestCase):
    def test_passes(self): pass
    def test_fails(self): self.assertEqual(guarded.LIMIT, 1)
    def test_errors(self): raise ValueError('a crash the missing guard allows')
    def test_imports(self): import missing_module_for_check_guards
    def test_names(self): undefined_name
    def test_killed(self): os.kill(os.getpid(), signal.SIGKILL)
'''


class CheckGuardsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_sample.py').write_text(SAMPLE)
        (self.root / 'guarded.py').write_text('LIMIT = 1\n')

    def test_outcomes_tell_a_broken_or_killed_test_from_a_failing_one(self):
        expected = {'passes': 'pass', 'fails': 'pass', 'errors': 'error', 'imports': 'broken', 'names': 'broken',
                    'killed': 'killed'}
        for name, outcome in expected.items():
            with self.subTest(name=name):
                self.assertEqual(check_guards.run_tests(self.root, [f'test_sample.T.test_{name}'])[0], outcome)

    def test_only_failures_and_ordinary_errors_count_as_red(self):
        def check(tests, disabled='LIMIT = 2\n'):
            return check_guards.check(self.root, 'guarded.py', 'LIMIT = 1\n', disabled, tests)
        line, _, problem = check(['test_sample.T.test_fails', 'test_sample.T.test_errors'])
        self.assertTrue(line.startswith('red'), line)
        self.assertIsNone(problem)
        for test in ('test_sample.T.test_imports', 'test_sample.T.test_killed'):
            with self.subTest(test=test):
                line, _, problem = check(['test_sample.T.test_fails', test])
                self.assertFalse(line.startswith('red'), line)
                self.assertIn(test, problem)
        line, _, problem = check(['test_sample.T.test_fails'], disabled='LIMIT = UNDEFINED\n')
        self.assertFalse(line.startswith('red'), line)  # the mutant itself breaks the module
        self.assertIn('broken', problem)
        self.assertEqual((self.root / 'guarded.py').read_text(), 'LIMIT = 1\n')
