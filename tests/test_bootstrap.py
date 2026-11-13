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

    def test_model_pool_replaces_checkout_routing_destinations(self):
        # The checkout's soso.json routing names its own models, absent from a supplied pool.
        import contextlib, io
        from soso.engine import Run
        with tempfile.TemporaryDirectory() as folder:
            pool = Path(folder) / 'pool.json'
            pool.write_text(json.dumps({'models': {'test': {'kind': 'command', 'command': ['false']}}}))
            report = {'status': 'plateau', 'baseline': {'score': 1}, 'best': 1}
            with patch.object(Run, 'run', return_value=report), patch.dict(os.environ, {'SOSO_LOCAL_ONLY': ''}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                code = bootstrap.main(['--workspace', folder, '--models', str(pool)])
            self.assertEqual(code, 0, errors.getvalue())
            config = json.loads(next(Path(folder).glob('self-*/soso.json')).read_text())
            self.assertNotIn('routing', config)
            self.assertEqual(set(config['models']), {'test'})

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

    def test_help_leaves_no_managed_state(self):
        for python in (True, False):
            with self.subTest(python=python), tempfile.TemporaryDirectory() as folder:
                root, env, log = self.shell_fixture(folder)
                if not python:
                    (Path(folder) / 'bin/python3').unlink()
                cwd = Path(folder) / 'elsewhere'
                cwd.mkdir()
                result = subprocess.run([str(root / 'so-so'), '--help'], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('LAUNCH' if python else 'usage: so-so', result.stdout)
                self.assertFalse((cwd / '.so-so').exists())
                self.assertFalse(log.exists())

    def test_help_never_runs_a_tool_from_an_unchecked_state_directory(self):
        # Help skips the .so-so ownership check, so it must not execute anything stored there.
        with tempfile.TemporaryDirectory() as folder:
            root, env, log = self.shell_fixture(folder)
            (Path(folder) / 'bin/python3').unlink()
            cwd = Path(folder) / 'elsewhere'
            (cwd / '.so-so/tools').mkdir(parents=True)
            marker = Path(folder) / 'uv-ran'
            uv = cwd / '.so-so/tools/uv'
            uv.write_text(f'#!/bin/sh\n: > {marker}\n')
            uv.chmod(0o755)
            result = subprocess.run([str(root / 'so-so'), '--help'], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage: so-so', result.stdout)
            self.assertFalse(marker.exists())

    def test_help_never_runs_an_unchecked_managed_runtime(self):
        # A launcher used alone runs .so-so/runtime; help skips the ownership check, so it must not run it.
        with tempfile.TemporaryDirectory() as folder:
            root, env, log = self.shell_fixture(folder)
            launcher = Path(folder) / 'alone/so-so'
            launcher.parent.mkdir()
            shutil.copy2(root / 'so-so', launcher)
            cwd = Path(folder) / 'elsewhere'
            runtime = cwd / '.so-so/runtime/soso'
            runtime.mkdir(parents=True)
            marker = Path(folder) / 'runtime-ran'
            (runtime / '__init__.py').write_text('')
            (runtime / 'launch.py').write_text(f'open({str(marker)!r}, "w").close()\n')
            result = subprocess.run([str(launcher), '--help'], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage: so-so', result.stdout)
            self.assertFalse(marker.exists())

    def test_resume_of_bundled_assignment_prepares_its_check_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root, env, log = self.shell_fixture(folder)
            # The launcher reuses the runtime's saved-task lookup; this stub stands in for it.
            (root / 'soso/launch.py').write_text(
                'from shutil import which\n'
                'def _resume_snapshot(managed, task_id):\n'
                '    assert task_id == "", task_id\n'
                '    return None, None, None, (managed / "saved-intent.md").read_text(), None\n'
                'if __name__ == "__main__":\n'
                '    print("LAUNCH", bool(which("node")), bool(which("cc")))\n')
            state = root / '.so-so'
            state.mkdir()
            (state / 'owner.json').write_text('{"owner":"so-so","version":1}\n')
            (state / 'saved-intent.md').write_text((root / 'INTENT.md').read_text())
            result = subprocess.run([str(root / 'so-so'), 'resume'], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('LAUNCH True True', result.stdout)
            self.assertIn('nodejs', log.read_text())

    def test_uv_installer_is_version_pinned_and_checksum_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            root, env, log = self.shell_fixture(folder)
            tools = Path(folder) / 'bin'
            (tools / 'python3').unlink()  # force the local runtime path
            for name in ('sh', 'rm', 'touch', 'sha256sum', 'shasum'):
                if shutil.which(name):
                    (tools / name).symlink_to(shutil.which(name))
            marker = Path(folder) / 'installer-ran'
            requests = Path(folder) / 'curl.log'
            (tools / 'curl').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + str(requests) + '\n'
                                        'while [ "$#" -gt 1 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                                        'printf "%s\\n" "touch ' + str(marker) + '" > "$out"\n')
            (tools / 'curl').chmod(0o755)
            result = subprocess.run([str(root / 'so-so')], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn('checksum', result.stderr)
            self.assertRegex(requests.read_text(), r'https://astral\.sh/uv/\d+\.\d+\.\d+/install\.sh')

    def test_failing_checksum_tool_removes_unverified_installer(self):
        for tool in ('sha256sum', 'shasum'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as folder:
                root, env, log = self.shell_fixture(folder)
                tools = Path(folder) / 'bin'
                (tools / 'python3').unlink()  # force the local runtime path
                for name in ('sh', 'rm'):
                    (tools / name).symlink_to(shutil.which(name))
                (tools / 'curl').write_text('#!/bin/sh\nwhile [ "$#" -gt 1 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                                            'printf "%s\\n" "exit 0" > "$out"\n')
                # The tool prints a matching digest but fails, as a broken checksum tool might.
                (tools / tool).write_text('#!/bin/sh\nprintf "%s  %s\\n" d263ee68314cf5bea36b08e6bd9e2c4563240d7adb73756cf33af5c199b92db4 x\n'
                                          'exit 3\n')
                for name in ('curl', tool):
                    (tools / name).chmod(0o755)
                result = subprocess.run([str(root / 'so-so')], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1)
                self.assertIn('so-so: could not verify the pinned uv installer checksum.', result.stderr)
                self.assertFalse((root / '.so-so/tools/install-uv.sh').exists())

    def test_timeout_hides_command_credentials(self):
        argv = ['git', 'clone', 'https://SECRET@example.com/repo']
        with patch.object(bootstrap.subprocess, 'run', side_effect=subprocess.TimeoutExpired(argv, 180)):
            with self.assertRaisesRegex(RuntimeError, 'git timed out') as caught:
                bootstrap.command(argv)
        self.assertNotIn('SECRET', str(caught.exception))
