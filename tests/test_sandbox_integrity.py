import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_core import RepoCase
from soso.cli import main
from soso.engine import Run, fingerprint, local_programs
from soso.models import Router
from soso.process import git
from soso.workspace import Workspace


class SandboxIntegrityTests(RepoCase):
    def test_git_dir_is_rejected_in_any_case(self):
        # Case-insensitive filesystems (macOS default) map .GIT onto .git.
        ws = Workspace(self.root, [])
        for name in ('.GIT', '.Git/config', 'sub/.gIt/HEAD'):
            with self.assertRaises(ValueError):
                ws.path(name)

    def test_protection_matches_normalised_names(self):
        ws = Workspace(self.root, ['scripts/*'])
        for name in ('./scripts/x.py', 'scripts//x.py'):
            with self.assertRaises(PermissionError):
                ws.tool({'tool': 'write', 'path': name, 'content': ''})

    def test_candidate_is_evaluated_without_ignored_files(self):
        # Ignored files are never committed, so they must not influence the accepted score.
        code = ("import json\nfrom pathlib import Path\nbonus=Path('data/bonus.txt')\n"
                "print(json.dumps({'score':int(Path('value.txt').read_text())+(int(bonus.read_text()) if bonus.exists() else 0)}))\n")
        steps = iter([{'tool': 'write', 'path': 'data/.gitignore', 'content': '*\n'},
                      {'tool': 'write', 'path': 'data/bonus.txt', 'content': '999'},
                      {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                      {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            if role == 'planner': return {'experiments': [{'task': 'improve value'}]}
            return next(steps)
        runner = Run(self.root, self.config(evaluate=[sys.executable, '-c', code], parallel=1, rounds=1), 'Improve')
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        experiment = report['rounds'][0]['experiments'][0]
        self.assertTrue(experiment.get('commit'), experiment)
        self.assertEqual(experiment['evaluation']['score'], 0)
        self.assertEqual(report['best'], 0)

    def test_candidate_adding_embedded_repository_is_invalid(self):
        # A nested repository commits as a gitlink; its files stay on disk and could feed the evaluator.
        code = ("import json\nfrom pathlib import Path\nbonus=Path('sub/bonus.txt')\n"
                "print(json.dumps({'score':int(Path('value.txt').read_text())+(int(bonus.read_text()) if bonus.exists() else 0)}))\n")
        nested = ('git init -q sub && echo 999 > sub/bonus.txt && git -C sub add bonus.txt && git -C sub '
                  '-c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false commit -qm nested')
        steps = iter([{'tool': 'run', 'argv': ['sh', '-c', nested]}, {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            return next(steps)
        runner = Run(self.root, self.config(evaluate=[sys.executable, '-c', code], parallel=1, rounds=1), 'Improve',
                     allow_exec=True)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        experiment = report['rounds'][0]['experiments'][0]
        self.assertTrue(experiment.get('commit'), experiment)
        self.assertIn('160000', git(self.root, 'ls-tree', experiment['commit'], 'sub'))
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'embedded repositories are not allowed'})
        self.assertEqual(report['best'], 0)

    def test_sparse_checkout_flags_inherited_by_worktrees_do_not_invalidate_candidates(self):
        # Worktrees of a sparse checkout start with skip-worktree entries the worker did not set.
        (self.root / 'extra').mkdir()
        (self.root / 'extra/x.txt').write_text('outside the sparse cone')
        git(self.root, 'add', 'extra/x.txt')
        git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'extra')
        git(self.root, 'sparse-checkout', 'set', 'keep')
        steps = iter([{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            return next(steps)
        runner = Run(self.root, self.config(parallel=1, rounds=1), 'Improve')
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        self.assertIn('S extra/x.txt', git(runner.integration, 'ls-files', '-v'))
        self.assertEqual(report['rounds'][0]['experiments'][0]['evaluation'].get('score'), 5, report['rounds'][0]['experiments'][0]['evaluation'])
        self.assertEqual(report['best'], 5)

    def test_candidate_hiding_worktree_changes_with_index_flags_is_invalid(self):
        # The flagged edit stays on disk but out of the commit, so the score would not match the tree.
        for flag in ('--skip-worktree', '--assume-unchanged'):
            with self.subTest(flag=flag):
                hide = f'git update-index {flag} value.txt && echo 999 > value.txt'
                steps = iter([{'tool': 'run', 'argv': ['sh', '-c', hide]},
                              {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                              {'tool': 'done', 'summary': 'done'}])
                def answer(router, role, prompt, cwd, attempt=0, task=None):
                    router.budget.claim()
                    if role == 'reviewer': return {'approve': True}
                    return next(steps)
                runner = Run(self.root, self.config(parallel=1, rounds=1), 'Improve', allow_exec=True)
                with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
                    report = runner.run()
                experiment = report['rounds'][0]['experiments'][0]
                self.assertTrue(experiment.get('commit'), experiment)
                self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'index flags hide worktree changes'})
                self.assertEqual(report['best'], 0)

    def commit_submodule(self):
        # A base commit with a gitlink; candidate worktrees get an empty, uninitialised sub/.
        (self.root / 'sub').mkdir()
        identity = ('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false')
        git(self.root / 'sub', 'init', '-q')
        (self.root / 'sub/f.txt').write_text('x')
        git(self.root / 'sub', 'add', 'f.txt')
        git(self.root / 'sub', *identity, 'commit', '-qm', 'nested')
        git(self.root, 'add', 'sub')
        git(self.root, *identity, 'commit', '-qm', 'add submodule')

    def test_tool_refuses_writes_inside_a_base_submodule(self):
        # Git neither records nor cleans files under a gitlink, so they would feed the evaluator unseen.
        self.commit_submodule()
        code = ("import json\nfrom pathlib import Path\nbonus=Path('sub/bonus.txt')\n"
                "print(json.dumps({'score':int(Path('value.txt').read_text())+(int(bonus.read_text()) if bonus.exists() else 0)}))\n")
        steps = iter([{'tool': 'write', 'path': 'sub/bonus.txt', 'content': '999'},
                      {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                      {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            return next(steps)
        runner = Run(self.root, self.config(evaluate=[sys.executable, '-c', code], parallel=1, rounds=1), 'Improve')
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        self.assertEqual(report['best'], 0)
        ws = Workspace(self.root, [])
        for name in ('sub', 'sub/bonus.txt', './SUB/x.txt'):
            with self.subTest(name=name), self.assertRaisesRegex(PermissionError, 'submodule'):
                ws.tool({'tool': 'write', 'path': name, 'content': '1'})

    def test_candidate_with_files_inside_a_submodule_directory_is_invalid(self):
        # Command execution bypasses the write tool; a fresh worktree leaves submodule directories empty.
        self.commit_submodule()
        code = ("import json\nfrom pathlib import Path\nbonus=Path('sub/bonus.txt')\n"
                "print(json.dumps({'score':int(Path('value.txt').read_text())+(int(bonus.read_text()) if bonus.exists() else 0)}))\n")
        steps = iter([{'tool': 'run', 'argv': ['sh', '-c', 'echo 999 > sub/bonus.txt']},
                      {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                      {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            return next(steps)
        runner = Run(self.root, self.config(evaluate=[sys.executable, '-c', code], parallel=1, rounds=1), 'Improve',
                     allow_exec=True)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        experiment = report['rounds'][0]['experiments'][0]
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'files inside a submodule directory'})
        self.assertEqual(report['best'], 0)

    def test_protection_ignores_case(self):
        # Case-insensitive filesystems (macOS default) map EVALUATE.PY onto evaluate.py.
        ws = Workspace(self.root, ['evaluate.py', 'tests/*'])
        for name in ('EVALUATE.PY', 'Tests/t.py', 'TESTS/new.py'):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                ws.tool({'tool': 'write', 'path': name, 'content': 'x'})
        self.assertIn('print(json.dumps', (self.root / 'evaluate.py').read_text())

    def test_agents_md_name_check_ignores_case(self):
        # Workspace.instructions reads sub/agents.md as sub/AGENTS.md on a case-insensitive filesystem.
        (self.root / 'sub').mkdir()
        (self.root / 'sub/agents.md').write_text('rules')
        ws = Workspace(self.root, [])
        for name in ('sub/agents.md', 'sub/Agents.MD'):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                ws.tool({'tool': 'write', 'path': name, 'content': 'other rules'})
        self.assertEqual((self.root / 'sub/agents.md').read_text(), 'rules')
        self.assertIn('sub/agents.md', fingerprint(self.root, []))

    def test_protection_keeps_case_sensitive_character_classes(self):
        # Casefolding a pattern turns [!A-Z] into [!a-z]; the pattern as written must still match.
        patterns = ['[!A-Z]*.lock', '[!a-z]*', 'data/[!A-Z]*']
        (self.root / 'data').mkdir()
        for name in ('poetry.lock', 'Makefile', 'data/golden.json'):
            (self.root / name).write_text('original')
        ws = Workspace(self.root, patterns)
        for name in ('poetry.lock', 'Makefile', 'data/golden.json'):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                ws.tool({'tool': 'write', 'path': name, 'content': 'changed'})
            self.assertEqual((self.root / name).read_text(), 'original')
        self.assertLessEqual({'poetry.lock', 'Makefile', 'data/golden.json'}, set(fingerprint(self.root, patterns)))

    def test_run_protects_in_repo_evaluator_and_check_programs(self):
        (self.root / 'bench').mkdir()
        (self.root / 'bench/score.py').write_text("print('{\"score\":0}')\n")
        (self.root / 'check.py').write_text('')
        (self.root / 'INTENT.md').write_text('Improve value')
        (self.root / 'soso.json').write_text(json.dumps({
            'models': {'test': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['test']},
            'evaluate': ['python3', 'bench/score.py'], 'checks': [['python3', './check.py']]}))
        captured = {}
        class FakeRun:
            report_path = self.root / 'report.json'
            def __init__(self, root, config, intent, allow_exec=False, resume=None):
                captured.update(config)
            def run(self):
                return {'status': 'plateau'}
        with patch('soso.cli.Run', FakeRun), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--repo', str(self.root)]), 0)
        self.assertIn('bench/score.py', captured['protected'])
        self.assertEqual(captured['protected'].count('bench/score.py'), 1)
        self.assertTrue(Workspace(self.root, captured['protected']).is_protected('check.py'))

    def prepared_protection(self, evaluate, checks):
        (self.root / 'INTENT.md').write_text('Improve value')
        (self.root / 'soso.json').write_text(json.dumps({
            'models': {'test': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['test']},
            'evaluate': evaluate, 'checks': checks}))
        captured = {}
        class FakeRun:
            report_path = self.root / 'report.json'
            def __init__(self, root, config, intent, allow_exec=False, resume=None):
                captured.update(config)
            def run(self):
                return {'status': 'plateau'}
        with patch('soso.cli.Run', FakeRun), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--repo', str(self.root)]), 0)
        return captured['protected']

    def test_run_protects_normalised_program_paths_inside_repo_only(self):
        (self.root / 'bench').mkdir()
        (self.root / 'tools').mkdir()
        (self.root / 'tools/check.py').write_text('')
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / 'o.py').write_text('')
        protected = self.prepared_protection(['python3', 'bench/../tools/check.py'],
                                             [['python3', os.path.relpath(outside / 'o.py', self.root)]])
        self.assertTrue(Workspace(self.root, protected).is_protected('tools/check.py'))
        self.assertFalse([name for name in protected if name.startswith('..')], protected)

    def test_run_protects_module_named_by_python_m(self):
        (self.root / 'bench').mkdir()
        (self.root / 'bench/score.py').write_text('')
        (self.root / 'tool').mkdir()
        (self.root / 'tool/__main__.py').write_text('')
        protected = self.prepared_protection(['python3', '-m', 'bench.score'], [['python3', '-m', 'tool']])
        ws = Workspace(self.root, protected)
        self.assertTrue(ws.is_protected('bench/score.py'))
        self.assertTrue(ws.is_protected('tool/__main__.py'))

    def test_run_protects_program_path_that_reenters_repo(self):
        (self.root / 'tools').mkdir()
        (self.root / 'tools/check.py').write_text('')
        protected = self.prepared_protection(['python3', '../' + self.root.resolve().name + '/tools/check.py'], [])
        self.assertIn('tools/check.py', protected)

    def test_run_protects_parent_packages_of_python_m_module(self):
        # Importing bench.sub.score runs bench/__init__.py and bench/sub/__init__.py first.
        (self.root / 'bench/sub').mkdir(parents=True)
        for name in ('bench/__init__.py', 'bench/sub/__init__.py', 'bench/sub/score.py'):
            (self.root / name).write_text('')
        protected = self.prepared_protection(['python3', '-m', 'bench.sub.score'], [])
        for name in ('bench/__init__.py', 'bench/sub/__init__.py', 'bench/sub/score.py'):
            self.assertIn(name, protected)

    def test_python_m_module_names_are_protected_before_they_exist(self):
        # A worker creating a/b/__main__.py or a/__init__.py would take over python3 -m a.b.
        (self.root / 'a').mkdir()
        (self.root / 'a/b.py').write_text('')
        protected = local_programs(self.root, [['python3', '-m', 'a.b']])
        for name in ('a/b.py', 'a/__init__.py', 'a/b/__init__.py', 'a/b/__main__.py'):
            self.assertIn(name, protected)
        before = fingerprint(self.root, protected)
        with self.assertRaises(PermissionError):
            Workspace(self.root, protected).tool({'tool': 'write', 'path': 'a/b/__main__.py', 'content': ''})
        (self.root / 'a/b').mkdir()
        (self.root / 'a/b/__main__.py').write_text('')  # e.g. created with command execution
        self.assertNotEqual(fingerprint(self.root, protected), before)

    def test_python_m_is_recognised_only_for_python_in_its_spellings(self):
        (self.root / 'a').mkdir()
        (self.root / 'a/b.py').write_text('')
        (self.root / 'slow.py').write_text('')
        self.assertNotIn('slow.py', local_programs(self.root, [['pytest', '-m', 'slow']]))
        for command in (['python3', '-ma.b'], ['python3', '-Bm', 'a.b'], ['/usr/bin/python3.11', '-B', '-m', 'a.b']):
            with self.subTest(command=command):
                self.assertIn('a/b.py', local_programs(self.root, [command]))

    def test_python_m_is_recognised_behind_a_wrapper(self):
        (self.root / 'a').mkdir()
        (self.root / 'a/b.py').write_text('')
        for command in (['uv', 'run', 'python', '-m', 'a.b'], ['env', 'python3', '-m', 'a.b'],
                        ['poetry', 'run', 'python', '-B', '-m', 'a.b']):
            with self.subTest(command=command):
                self.assertIn('a/b/__main__.py', local_programs(self.root, [command]))

    def test_python_directory_argument_protects_its_main(self):
        (self.root / 'pkgdir').mkdir()
        (self.root / 'pkgdir/helper.py').write_text('')
        self.assertIn('pkgdir/__main__.py', local_programs(self.root, [['python3', 'pkgdir']]))

    def test_program_paths_resolve_symlinks(self):
        (self.root / 'real').mkdir()
        (self.root / 'real/check.py').write_text('')
        (self.root / 'link').symlink_to('real')
        (self.root / 'top.py').write_text('')
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / 'alias').symlink_to(self.root)
        protected = local_programs(self.root, [['python3', 'link/check.py'], ['python3', str(outside / 'alias/top.py')]])
        self.assertIn('real/check.py', protected)
        self.assertIn('top.py', protected)
        # Replacing the directory link with a real directory must still change protected state.
        self.assertIn('link/check.py', protected)
        before = fingerprint(self.root, protected)
        (self.root / 'link').unlink()
        (self.root / 'link').mkdir()
        (self.root / 'link/check.py').write_text('worker code')
        self.assertNotEqual(fingerprint(self.root, protected), before)

    def test_program_selection_tolerates_unusable_names(self):
        self.assertEqual(local_programs(self.root, [['python3', '-c', 'x' * 300], ['python3', 'a\0b']]), [])

    def test_already_protected_evaluator_leaves_config_unchanged(self):
        # An unchanged protected list keeps the config digest, so earlier runs stay resumable.
        (self.root / 'INTENT.md').write_text('Improve value')
        (self.root / 'soso.json').write_text(json.dumps({
            'models': {'test': {'kind': 'command', 'command': ['false']}}, 'routes': {'worker': ['test']},
            'evaluate': ['python3', 'evaluate.py'], 'checks': [['python3', 'evaluate.py']]}))
        captured = {}
        class FakeRun:
            report_path = self.root / 'report.json'
            def __init__(self, root, config, intent, allow_exec=False, resume=None):
                captured.update(config)
            def run(self):
                return {'status': 'plateau'}
        with patch('soso.cli.Run', FakeRun), contextlib.redirect_stdout(io.StringIO()):
            main(['run', '--repo', str(self.root)])
        self.assertEqual(captured['protected'].count('evaluate.py'), 1)


if __name__ == '__main__':
    unittest.main()
