import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

spec=importlib.util.spec_from_file_location('host_bootstrap',Path(__file__).parents[1]/'bootstrap.py')
bootstrap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

class BootstrapTests(unittest.TestCase):
    def test_checkout_needs_no_network(self):
        with tempfile.TemporaryDirectory() as folder:
            result=bootstrap.runtime(Path(folder))
            self.assertTrue((result/'soso/__init__.py').is_file())
    def test_launcher_idempotent_and_protects_files(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=Path(folder)
            checkout=Path(__file__).parents[1]
            first=bootstrap.install_launcher(checkout,workspace)
            self.assertEqual(first,bootstrap.install_launcher(checkout,workspace))
            self.assertEqual(first.resolve(),(checkout/'bin/soso').resolve())
            first.unlink()
            first.write_text('user content')
            with self.assertRaises(ValueError): bootstrap.install_launcher(checkout,workspace)
            self.assertEqual(first.read_text(),'user content')
    def test_install_only_has_no_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(bootstrap.main(['--workspace',folder,'--install-only']),0)
            self.assertTrue((Path(folder)/'soso').is_symlink())

    def test_timeout_hides_command_credentials(self):
        argv = ['git', 'clone', 'https://SECRET@example.com/repo']
        with patch.object(bootstrap.subprocess, 'run', side_effect=subprocess.TimeoutExpired(argv, 180)):
            with self.assertRaisesRegex(RuntimeError, 'git timed out') as caught:
                bootstrap.command(argv)
        self.assertNotIn('SECRET', str(caught.exception))
