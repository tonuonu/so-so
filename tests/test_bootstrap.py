import importlib.util
import hashlib
import json
import os
import shutil
import sys
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

    def shell_fixture(self, folder, installer='apt-get', permitted=True, existing=False):
        root = Path(folder) / 'checkout'
        tools = Path(folder) / 'bin'
        root.mkdir()
        tools.mkdir()
        checkout = Path(__file__).resolve().parents[1]
        shutil.copy2(checkout / 'so-so', root / 'so-so')
        (root / 'soso').mkdir()
        (root / 'soso/__init__.py').write_text('')
        (root / 'soso/launch.py').write_text('from shutil import which\nprint("LAUNCH",bool(which("node")),bool(which("cc")))\n')
        intent = 'Simplify this prepared runner.\n'
        (root / 'INTENT.md').write_text(intent)
        (root / 'soso.json').write_text(json.dumps({'mode': 'optimize',
            'prepared_intent_sha256': hashlib.sha256(intent.encode()).hexdigest(),
            'checks': [['python3', 'scripts/check_examples.py']]}))
        for name, target in [('python3', sys.executable), ('mkdir', shutil.which('mkdir')),
                             ('grep', shutil.which('grep'))]:
            (tools / name).symlink_to(target)
        def executable(name, text):
            path = tools / name
            path.write_text(text)
            path.chmod(0o755)
        executable('git', '#!/bin/sh\nexit 0\n')
        executable('id', '#!/bin/sh\nprintf "%s\\n" ' + ('0' if permitted else '1000') + '\n')
        executable('sudo', '#!/bin/sh\nexit 1\n')
        log = Path(folder) / 'packages.jsonl'
        if installer:
            executable(installer, '#!' + sys.executable + ' -I\n' +
                'import json,os,pathlib,sys\n'
                'with open(os.environ["SOSO_TEST_PACKAGES"],"a") as log: log.write(json.dumps(sys.argv[1:])+"\\n")\n'
                'if any(x in sys.argv for x in ("install","add")):\n'
                '    for name in ("cc","node"):\n'
                '        target=pathlib.Path(os.environ["PATH"])/name\n'
                '        target.write_text("#!/bin/sh\\nexit 0\\n");target.chmod(0o755)\n')
        if existing:
            executable('cc', '#!/bin/sh\nexit 0\n')
            executable('node', '#!/bin/sh\nexit 0\n')
        env = dict(os.environ, PATH=str(tools), SOSO_TEST_PACKAGES=str(log))
        return root, env, log

    def test_normal_launcher_installs_missing_bundled_check_tools(self):
        for installer, expected in (('apt-get', ['nodejs', 'build-essential']),
                                    ('dnf', ['nodejs', 'gcc', 'glibc-devel']),
                                    ('apk', ['nodejs', 'build-base'])):
            with self.subTest(installer=installer), tempfile.TemporaryDirectory() as folder:
                root, env, log = self.shell_fixture(folder, installer=installer)
                result = subprocess.run([str(root / 'so-so')], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('LAUNCH True True', result.stdout)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(calls[-1][-len(expected):], expected)
                # Existing fixed checks are not disabled to avoid provisioning.
                self.assertEqual(json.loads((root / 'soso.json').read_text())['checks'],
                                 [['python3', 'scripts/check_examples.py']])
                again = subprocess.run([str(root / 'so-so')], cwd=root, env=env,
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual([json.loads(line) for line in log.read_text().splitlines()], calls)

    def test_bundled_tools_do_not_provision_for_user_projects_or_changed_assignment(self):
        for mode in ('project', 'edited-intent', 'explicit-goal', 'already-installed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root, env, log = self.shell_fixture(folder, existing=mode == 'already-installed')
                cwd = root
                argv = [str(root / 'so-so')]
                if mode == 'project':
                    cwd = Path(folder) / 'project'
                    cwd.mkdir()
                    (cwd / 'INTENT.md').write_text('Build an app')
                elif mode == 'edited-intent':
                    (root / 'INTENT.md').write_text('Build an app')
                elif mode == 'explicit-goal':
                    argv.append('Build an app')
                result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('LAUNCH', result.stdout)
                self.assertFalse(log.exists())

    def test_bundled_tool_install_preserves_noninteractive_permission_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root, env, log = self.shell_fixture(folder, permitted=False)
            result = subprocess.run([str(root / 'so-so')], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('needs system installation permission', result.stderr)
            self.assertNotIn('LAUNCH', result.stdout)
            self.assertFalse(log.exists())

    def test_timeout_hides_command_credentials(self):
        argv = ['git', 'clone', 'https://SECRET@example.com/repo']
        with patch.object(bootstrap.subprocess, 'run', side_effect=subprocess.TimeoutExpired(argv, 180)):
            with self.assertRaisesRegex(RuntimeError, 'git timed out') as caught:
                bootstrap.command(argv)
        self.assertNotIn('SECRET', str(caught.exception))
