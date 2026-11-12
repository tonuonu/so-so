"""The accepted commit is exactly what was evaluated: every way a worker made them differ.

Each bypass test checks every validly evaluated candidate against a fresh clone of its commit, which
has none of the worker's leftovers and only Git's default configuration.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from test_core import RepoCase
from soso import engine
from soso.delivery import DeliveryEpoch
from soso.engine import Run
from soso.models import Router
from soso.process import execute, git

SCORE = "import json\nprint(json.dumps({'score': %s}))\n"
VALUE = "int(open('value.txt').read())"


def score_of(expression, imports=''):
    return [sys.executable, '-c', imports + SCORE % expression]


class EvaluatedTreeTests(RepoCase):
    def common_dir(self):
        return (self.root / '.git').resolve()  # RepoCase is a plain repository

    def run_candidate(self, steps, evaluate=None, allow_exec=True, prompts=None, **config):
        """One candidate taking the given worker steps (then done); returns the report."""
        steps = iter(steps + [{'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer':
                if prompts is not None:
                    prompts.append(prompt)
                return {'approve': True}
            return next(steps)
        settings = {'parallel': 1, 'rounds': 1, **({'evaluate': evaluate} if evaluate else {}), **config}
        runner = Run(self.root, self.config(**settings), 'Improve', allow_exec=allow_exec)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            return runner.run()

    def fresh_score(self, commit, evaluate):
        """The evaluator's score on a fresh clone of commit, or None when it fails there."""
        with tempfile.TemporaryDirectory() as folder:
            clone = Path(folder) / 'clone'
            git(self.root, 'clone', '-q', '--no-checkout', str(self.root), str(clone))
            git(clone, 'fetch', '-q', 'origin', '+refs/soso/*:refs/soso/*')
            git(clone, 'checkout', '-q', '--detach', commit)
            result = execute(evaluate, clone, 30)
            try:
                return float(json.loads(result['output'].strip().splitlines()[-1])['score'])
            except (ValueError, KeyError, IndexError):
                return None

    def assert_scored_as_committed(self, report, evaluate):
        for experiment in report['rounds'][0]['experiments']:
            evaluation = experiment['evaluation']
            if evaluation.get('valid'):
                self.assertEqual(evaluation['score'], self.fresh_score(experiment['commit'], evaluate), experiment)

    def experiment(self, report):
        return report['rounds'][0]['experiments'][0]

    def restored_keys(self, report):
        return [change['key'] for change in self.experiment(report).get('config_restored', [])]

    def commit(self, message, *names):
        git(self.root, 'add', *names)
        git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-qm', message)

    # 1. An ident attribute collapses "$Id: ... $" when staging, so the commit holds other code than the
    # worker's file; no command execution is needed.
    def test_ident_attribute_cannot_hide_the_scored_code(self):
        (self.root / 'mod.py').write_text('def f():\n    return 0\n')
        self.commit('module', 'mod.py')
        evaluate = score_of('mod.f()', 'import mod\n')
        report = self.run_candidate([
            {'tool': 'write', 'path': '.gitattributes', 'content': 'mod.py ident\n'},
            {'tool': 'write', 'path': 'mod.py', 'content': 'def f():\n    x = "$Id: "; return 999; " $"\n    return 1\n'}],
            evaluate, allow_exec=False)
        self.assertIn('$Id$', git(self.root, 'show', self.experiment(report)['commit'] + ':mod.py'))
        self.assert_scored_as_committed(report, evaluate)
        self.assertEqual(report['best'], 1)

    # 1b. attr.tree replaces the worktree's .gitattributes for checkout and diff alike.
    def test_attribute_tree_cannot_change_how_the_commit_is_checked_out_or_reviewed(self):
        (self.root / 'mod.py').write_text('def f():\n    return 0\n')
        self.commit('module', 'mod.py')
        evaluate = score_of('mod.f()', 'import mod\n')
        tree = ('t="$(printf \'100644 blob %s\\t.gitattributes\\n\' "$(printf \'mod.py ident\\n* -diff\\n\' | '
                'git hash-object -w --stdin)" | git mktree)" && git config attr.tree "$t"')
        prompts = []
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', tree]},
                                     {'tool': 'write', 'path': 'mod.py', 'content': 'x = "$Id$"\ndef f():\n    return 999 if len(x) > 4 else 1\n'}],
                                    evaluate, prompts=prompts, routes={'worker': ['test'], 'reviewer': ['test']})
        self.assert_scored_as_committed(report, evaluate)
        self.assertIn('local:attr.tree', self.restored_keys(report))
        self.assertEqual(git(self.root, 'config', '--get', 'attr.tree', ok=(0, 1)), '')

    # 2. File types and modes Git records differently from the files on disk.
    STAGE_SYMLINK = 'echo 999 > value.txt && git update-index --cacheinfo "120000,$(git hash-object -w value.txt),value.txt"'
    EXECUTABLE = "999 if __import__('os').access('value.txt', __import__('os').X_OK) else " + VALUE

    def test_symlink_staged_with_symlinks_turned_off_is_not_scored(self):
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.symlinks false && ' + self.STAGE_SYMLINK]},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}])
        self.assert_scored_as_committed(report, score_of(VALUE))
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertIn('local:core.symlinks', self.restored_keys(report))
        self.assertEqual(git(self.root, 'config', '--get', 'core.symlinks', ok=(0, 1)), '')

    def test_symlink_staged_under_the_users_symlinks_off_is_not_scored(self):
        # Nothing is restored here: the setting is the user's, and checkout writes the link as a file.
        git(self.root, 'config', 'core.symlinks', 'false')
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', self.STAGE_SYMLINK]},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}])
        self.assert_scored_as_committed(report, score_of(VALUE))
        self.assertEqual(self.experiment(report)['evaluation'],
                         {'valid': False, 'reason': 'symlinks cannot be checked out with core.symlinks=false'})

    def test_executable_bit_hidden_with_file_mode_turned_off_is_not_scored(self):
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.fileMode false && chmod +x value.txt']},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}], score_of(self.EXECUTABLE))
        self.assert_scored_as_committed(report, score_of(self.EXECUTABLE))
        self.assertIn('local:core.filemode', self.restored_keys(report))
        self.assertEqual(git(self.root, 'config', '--get', 'core.fileMode'), 'true')

    def test_executable_bit_hidden_under_the_users_file_mode_off_is_not_scored(self):
        git(self.root, 'config', 'core.fileMode', 'false')
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'chmod +x value.txt']},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}], score_of(self.EXECUTABLE))
        self.assert_scored_as_committed(report, score_of(self.EXECUTABLE))
        self.assertEqual(self.experiment(report)['evaluation'].get('score'), 0)

    # 3. A file outside the sparse checkout stays skip-worktree, so Git neither stages nor compares it.
    OUTSIDE = "int(open('extra/x.txt').read()) if __import__('os').path.exists('extra/x.txt') else " + VALUE

    def sparse_repository(self, *options):
        (self.root / 'extra').mkdir()
        (self.root / 'extra/x.txt').write_text('0')
        (self.root / 'keep').mkdir()
        (self.root / 'keep/k.txt').write_text('k')
        self.commit('sparse', 'extra/x.txt', 'keep/k.txt')
        git(self.root, 'sparse-checkout', 'set', '--cone', *options, 'keep')

    def test_file_outside_the_sparse_checkout_is_not_scored(self):
        self.sparse_repository()
        plant = 'git config sparse.expectFilesOutsideOfPatterns true && mkdir -p extra && echo 999 > extra/x.txt'
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', plant]},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}], score_of(self.OUTSIDE))
        self.assert_scored_as_committed(report, score_of(self.OUTSIDE))
        self.assertIn('local:sparse.expectfilesoutsideofpatterns', self.restored_keys(report))

    def test_file_outside_the_sparse_checkout_is_not_scored_under_the_users_setting(self):
        self.sparse_repository()
        git(self.root, 'config', 'sparse.expectFilesOutsideOfPatterns', 'true')
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'mkdir -p extra && echo 999 > extra/x.txt']},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}], score_of(self.OUTSIDE))
        self.assert_scored_as_committed(report, score_of(self.OUTSIDE))
        self.assertEqual(self.experiment(report)['evaluation'].get('score'), 0)

    # 4. Git records no directories and no special files, and git clean leaves special files behind.
    FLAG = "999 if __import__('os').path.exists('flag') else " + VALUE

    def test_empty_directory_is_not_scored(self):
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'mkdir flag']},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}], score_of(self.FLAG))
        self.assert_scored_as_committed(report, score_of(self.FLAG))
        self.assertEqual(self.experiment(report)['evaluation'].get('score'), 0)

    def test_special_file_is_not_scored(self):
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'mkfifo flag']},
                                     {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}], score_of(self.FLAG))
        self.assert_scored_as_committed(report, score_of(self.FLAG))
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'files Git cannot remove'})

    # 5. The reviewer must see the change that is accepted.
    def reviewed_diff(self, steps, **config):
        prompts = []
        report = self.run_candidate(steps, prompts=prompts, routes={'worker': ['test'], 'reviewer': ['test']}, **config)
        self.assertEqual(report['best'], 999)
        return prompts[0].split('\nDIFF:\n', 1)[1].split('\nEVALUATION:\n', 1)[0]

    def test_replace_objects_do_not_hide_the_change_from_the_reviewer(self):
        # A replace ref already in the repository (one a worker adds is removed, see below) shows the base
        # value.txt as 999, so a diff that honoured it would show no change at all.
        replaced = git(self.root, 'hash-object', '-w', '--stdin', stdin='999')
        git(self.root, 'replace', git(self.root, 'rev-parse', 'HEAD:value.txt'), replaced)
        diff = self.reviewed_diff([{'tool': 'write', 'path': 'value.txt', 'content': '999'}])
        self.assertIn('+999', diff)

    @contextlib.contextmanager
    def old_git(self):
        """Git before 2.40: --attr-source is unknown, and the runner knows it."""
        real = engine.git
        def old(root, *args, **kwargs):
            if any(str(arg).startswith('--attr-source') for arg in args):
                raise RuntimeError('git: unknown option')
            return real(root, *args, **kwargs)
        with patch('soso.engine.git', old), patch('soso.engine.supports_attr_source', return_value=False, create=True):
            yield

    # A worker that sets diff.* is rejected and restored (see below); the user's own external diff
    # program or textconv driver, e.g. a structural diff tool, must not shape the reviewer's view either.
    def reviewed_diffs(self, steps):
        """The reviewer's diff with this Git, and with one that lacks --attr-source (before 2.40)."""
        diffs = [self.reviewed_diff(steps)]
        for path in self.common_dir().glob('soso/*-*/result'):
            git(self.root, 'worktree', 'remove', '--force', str(path))  # back to the same base for the next run
        git(self.root, 'checkout', '-q', '--', 'value.txt')
        with self.old_git():
            diffs.append(self.reviewed_diff(steps))
        return diffs

    def test_external_diff_does_not_hide_the_change_from_the_reviewer(self):
        git(self.root, 'config', 'diff.external', 'true')
        for diff in self.reviewed_diffs([{'tool': 'write', 'path': 'value.txt', 'content': '999'}]):
            self.assertIn('+999', diff)

    def test_textconv_does_not_hide_the_change_from_the_reviewer(self):
        # A driver from the user's own info/attributes, which applies whatever --attr-source says.
        (self.common_dir() / 'info/attributes').write_text('value.txt diff=hide\n')
        git(self.root, 'config', 'diff.hide.textconv', 'true')
        for diff in self.reviewed_diffs([{'tool': 'write', 'path': 'value.txt', 'content': '999'}]):
            self.assertIn('+999', diff)

    def test_diff_driver_settings_do_not_hide_the_change_from_the_reviewer(self):
        # diff.<driver>.binary on a driver the base's attributes use shows only "Binary files differ".
        (self.root / '.gitattributes').write_text('*.py diff=python\n')
        (self.root / 'mod.py').write_text('x = 0\n')
        self.commit('module', '.gitattributes', 'mod.py')
        prompts = []
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git config diff.python.binary true']},
                                     {'tool': 'write', 'path': 'mod.py', 'content': 'x = 999\n'},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}],
                                    prompts=prompts, routes={'worker': ['test'], 'reviewer': ['test']})
        self.assertTrue(all('+x = 999' in prompt for prompt in prompts), prompts)  # reviewed only if it shows
        self.assertIn('local:diff.python.binary', self.restored_keys(report))
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    def test_delivery_reviewer_diff_ignores_external_diff_and_textconv(self):
        (self.root / '.gitattributes').write_text('value.txt diff=hide\n')
        self.commit('attributes', '.gitattributes')
        base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'value.txt').write_text('999')
        self.commit('candidate', 'value.txt')
        commit = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'checkout', '-q', '--detach', base)
        for key, value in (('diff.external', 'true'), ('diff.hide.textconv', 'true')):
            with self.subTest(key=key):
                git(self.root, 'config', key, value)
                prompts = []
                def ask(role, prompt, cwd, task=None):
                    prompts.append(prompt)
                    return {'approve': True, 'useful': True}
                epoch = SimpleNamespace(integration=self.root, router=SimpleNamespace(ask=ask), intent='Improve',
                                        contract={}, report={'baseline': {}})
                DeliveryEpoch.review(epoch, {'commit': commit, 'evaluation': {}})
                self.assertIn('+999', prompts[0])
                git(self.root, 'config', '--unset', key)

    # 6. A filter planted in a config file the user's config includes.
    STUB_FILTER = 'sed s/999/1/'
    PLANT = ('c="$(git rev-parse --git-common-dir)" && printf "value.txt filter=x\\n" > "$c/attrs" && '
             f'printf "[filter \\"x\\"]\\n\\tclean = {STUB_FILTER}\\n[core]\\n\\tattributesFile = $(cd "$c" && pwd)/attrs\\n" >> "$c/user-include" && '
             'echo 999 > value.txt')

    def test_filter_planted_in_an_included_file_is_restored_and_not_adopted_by_the_next_run(self):
        included = self.common_dir() / 'user-include'
        included.write_text('# the user\'s settings\n')
        git(self.root, 'config', 'include.path', str(included))
        first = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', self.PLANT]}])
        self.assertEqual(included.read_text(), '# the user\'s settings\n')
        self.assertEqual(self.experiment(first)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(self.restored_keys(first), ['included:' + str(included)])
        self.assertIn(self.STUB_FILTER, json.dumps(self.experiment(first)['config_restored']))
        second = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '999'}])
        self.assert_scored_as_committed(second, score_of(VALUE))
        self.assertEqual(git(self.root, 'show', self.experiment(second)['commit'] + ':value.txt'), '999')

    def test_included_file_the_worker_creates_is_removed(self):
        # The user's include names a file that does not exist yet; creating it brings settings in.
        included = self.common_dir() / 'later-include'
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', self.PLANT.replace('>> "$c/user-include"', '> "$c/later-include"')]}])
        self.assertFalse(included.exists())
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    def test_included_file_replaced_by_a_symlink_is_not_written_through(self):
        included, target = self.common_dir() / 'user-include', self.common_dir() / 'elsewhere'
        included.write_text('')
        target.write_text(f'[filter "x"]\n\tclean = {self.STUB_FILTER}\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'rm "{included}" && ln -s "{target}" "{included}"']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertFalse(included.is_symlink())  # the link itself is replaced by the recorded file
        self.assertEqual(included.read_text(), '')
        self.assertIn('clean', target.read_text())
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    def test_included_file_replaced_by_a_symlink_to_the_same_bytes_is_put_back(self):
        # The worker would control the link's target later on; a recorded file is never read through a link.
        included, target = self.common_dir() / 'user-include', self.common_dir() / 'elsewhere'
        included.write_text('[core]\n\teol = lf\n')
        target.write_text('[core]\n\teol = lf\n')
        git(self.root, 'config', 'include.path', str(included))
        self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'rm "{included}" && ln -s "{target}" "{included}"']},
                            {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertFalse(included.is_symlink())

    def test_users_symlinked_include_is_recorded_and_restored_at_its_target(self):
        # e.g. a dotfiles repository: the link stays, its target gets its bytes back.
        target, included = self.common_dir() / 'dotfiles-include', self.common_dir() / 'user-include'
        target.write_text('# the user\'s settings\n')
        included.symlink_to(target)
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', self.PLANT]}])
        self.assertTrue(included.is_symlink())
        self.assertEqual(target.read_text(), '# the user\'s settings\n')
        self.assertEqual(self.restored_keys(report), ['included:' + str(target.resolve())])

    def test_included_file_is_put_back_only_when_the_values_it_supplies_change(self):
        # The worker changes .git/config itself; an unrelated edit in the included file stays.
        included = self.common_dir() / 'user-include'
        included.write_text('[core]\n\teol = lf\n')
        git(self.root, 'config', 'include.path', str(included))
        change = f'git config core.autocrlf true && printf "[user]\\n\\tname = Someone\\n" >> "{included}"'
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', change]},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertIn('Someone', included.read_text())
        self.assertEqual(self.restored_keys(report), ['local:core.autocrlf'])

    def test_included_file_that_cannot_be_put_back_blocks_new_runs(self):
        folder = self.common_dir() / 'conf.d'
        folder.mkdir()
        included = folder / 'user-include'
        included.write_text('[core]\n\teol = lf\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'rm -r "{folder}"']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertIn(f'could not restore {included}; see config_restored', self.experiment(report)['error'])
        with self.assertRaisesRegex(ValueError, 'could not restore ' + re.escape(str(included))):
            Run(self.root, self.config(), 'Improve', allow_exec=True)

    def test_run_refuses_to_start_after_a_failed_restore_until_it_is_repaired(self):
        git(self.root, 'config', 'core.autocrlf', 'false')
        real = engine.git
        def dropping_add(root, *args, **kwargs):
            if args[:1] == ('config',) and '--add' in args:
                return ''
            return real(root, *args, **kwargs)
        with patch('soso.engine.git', dropping_add):
            report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.autocrlf true && echo 5 > value.txt']}])
        self.assertIn('could not restore', self.experiment(report)['error'])
        config = str(self.common_dir() / 'config')
        with self.assertRaisesRegex(ValueError, 'could not restore ' + re.escape(config)):
            Run(self.root, self.config(), 'Improve', allow_exec=True)
        git(self.root, 'config', 'core.autocrlf', 'false')  # the user repairs it
        Run(self.root, self.config(), 'Improve', allow_exec=True)
        self.assertFalse((self.common_dir() / 'soso/unrestored.json').exists())

    def test_include_activated_by_an_unwatched_setting_ends_the_candidate_and_blocks_new_runs(self):
        # The included file is unchanged; adding a remote turns its condition on, which no restore can undo.
        included = self.common_dir() / 'conditional'
        included.write_text(f'[filter "x"]\n\tclean = {self.STUB_FILTER}\n')
        git(self.root, 'config', 'includeIf.hasconfig:remote.*.url:https://example.invalid/**.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git remote add origin https://example.invalid/p.git']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertFalse(self.experiment(report)['evaluation']['valid'])
        self.assertIn('filter.x.clean changed in ' + str(included.resolve()), self.experiment(report)['error'])
        with self.assertRaisesRegex(ValueError, 'could not restore ' + re.escape(str(included.resolve()))):
            Run(self.root, self.config(), 'Improve', allow_exec=True)

    # Review follow-ups: the restore record, its repair check and what a restore may touch.
    def failed_restore(self):
        """A run whose restore of core.autocrlf fails, leaving soso/unrestored.json behind."""
        git(self.root, 'config', 'core.autocrlf', 'false')
        real = engine.git
        def dropping_add(root, *args, **kwargs):
            if args[:1] == ('config',) and '--add' in args:
                return ''
            return real(root, *args, **kwargs)
        with patch('soso.engine.git', dropping_add):
            report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.autocrlf true && echo 5 > value.txt']}])
        self.assertTrue((self.common_dir() / 'soso/unrestored.json').exists())
        return report

    def test_included_file_replaced_by_a_fifo_does_not_hang_the_restore(self):
        included = self.common_dir() / 'user-include'
        included.write_text('[core]\n\tautocrlf = false\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'},
                                     {'tool': 'run', 'argv': ['sh', '-c', f'rm "{included}" && mkfifo "{included}"']}])
        self.assertTrue(included.is_file())
        self.assertEqual(included.read_text(), '[core]\n\tautocrlf = false\n')
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    def test_config_files_are_read_only_when_regular(self):
        fifo = self.common_dir() / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            engine.config_bytes(fifo)  # returns at once, never waits for a writer
        self.assertIs(engine.current_bytes(fifo), engine.IRREGULAR)

    def test_restore_replaces_a_fifo_before_git_reads_config(self):
        # Config reads are stubbed: with the FIFO still there, Git itself would block on it.
        included = self.common_dir() / 'user-include'
        runner = Run(self.root, self.config(), 'Improve', allow_exec=True)
        runner.watched = (([], []), None, {str(included): b'[core]\n'}, engine.UNRECORDED, engine.UNRECORDED)
        os.mkfifo(included)
        own = (self.root / 'none', str(self.root / 'none'), ([], []))
        with patch('soso.engine.watched_config', return_value=([], [], {})):
            changed = runner.restore_watched(self.root, own, [])
        self.assertTrue(included.is_file())
        self.assertEqual(included.read_bytes(), b'[core]\n')
        self.assertEqual(changed, ['included:' + str(included)])

    def test_missing_record_of_a_failed_restore_gives_the_clear_message(self):
        report = self.failed_restore()
        os.unlink(self.common_dir() / 'soso' / report['run_id'] / 'watched-config.json')
        with self.assertRaisesRegex(ValueError, 'delete ' + re.escape(str(self.common_dir() / 'soso/unrestored.json'))):
            Run(self.root, self.config(), 'Improve', allow_exec=True)

    def test_repair_is_judged_in_the_context_the_record_was_taken_in(self):
        # Conditional includes that hold in the main worktree but not in a detached candidate worktree.
        conditions = (('onbranch', 'includeIf.onbranch:main.path'),
                      ('gitdir', f'includeIf.gitdir:{os.path.realpath(self.root / ".git")}.path'))
        for name, key in conditions:
            with self.subTest(condition=name):
                extra = self.common_dir() / f'{name}-include'
                extra.write_text('[core]\n\teol = lf\n')
                git(self.root, 'config', key, str(extra))
                self.assertEqual(git(self.root, 'config', 'core.eol'), 'lf')  # in effect here, not in candidates
                self.failed_restore()
                git(self.root, 'config', 'core.autocrlf', 'false')  # the user repairs it
                Run(self.root, self.config(), 'Improve', allow_exec=True)
                self.assertFalse((self.common_dir() / 'soso/unrestored.json').exists())
                git(self.root, 'config', '--unset', key)

    def test_restore_record_is_private(self):
        included = self.common_dir() / 'user-include'
        included.write_text('[http]\n\textraHeader = Authorization: Bearer secret-token\n')
        included.chmod(0o600)
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        record = self.common_dir() / 'soso' / report['run_id'] / 'watched-config.json'
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_included_file_is_restored_by_replacing_it_with_its_mode(self):
        included = self.common_dir() / 'user-include'
        included.write_text('# the user\'s settings\n')
        included.chmod(0o640)
        inode = included.stat().st_ino
        git(self.root, 'config', 'include.path', str(included))
        self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', self.PLANT]}])
        self.assertEqual(included.read_text(), '# the user\'s settings\n')
        self.assertEqual(included.stat().st_mode & 0o777, 0o640)
        self.assertNotEqual(included.stat().st_ino, inode)  # a new file renamed into place, never half written

    def test_included_file_edit_that_changes_no_watched_setting_is_kept(self):
        included = self.common_dir() / 'user-include'
        included.write_text('[core]\n\teol = lf\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'printf "[user]\\n\\tname = Someone\\n" >> "{included}"']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertIn('Someone', included.read_text())
        self.assertEqual(self.experiment(report)['evaluation'].get('score'), 5, self.experiment(report))
        self.assertNotIn('config_restored', self.experiment(report))

    def test_included_restore_evidence_holds_watched_values_not_file_text(self):
        included = self.common_dir() / 'user-include'
        included.write_text('[http]\n\textraHeader = Authorization: Bearer secret-token\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'printf "[core]\\n\\teol = crlf\\n" >> "{included}"']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        evidence = self.experiment(report)['config_restored']
        self.assertTrue(evidence[0].pop('kept'))  # the replaced file itself is kept privately, see below
        self.assertEqual(evidence, [{'key': 'included:' + str(included.resolve()), 'restored': [], 'found': ['core.eol=crlf']}])
        self.assertNotIn('secret-token', json.dumps(report))

    def test_include_whose_condition_never_holds_does_not_stop_runs(self):
        # Git never reads it, so neither its syntax nor its size may fail a candidate.
        broken = self.common_dir() / 'never-included'
        for content in ('this is not [a config file\n', '# padding\n' * 120000):
            broken.write_text(content)
            git(self.root, 'config', 'includeIf.gitdir:/nonexistent/.path', str(broken))
            for allow_exec in (False, True):
                with self.subTest(size=len(content), allow_exec=allow_exec):
                    self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}],
                                                         allow_exec=allow_exec))
                    git(self.root, 'checkout', '-q', '--', 'value.txt')

    def test_folder_put_in_place_of_an_included_file_is_moved_aside(self):
        included = self.common_dir() / 'user-include'
        included.write_text('[core]\n\teol = lf\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'},
                                     {'tool': 'run', 'argv': ['sh', '-c', f'rm "{included}" && mkdir "{included}" && echo x > "{included}/f"']}])
        self.assertEqual(included.read_text(), '[core]\n\teol = lf\n')
        kept = [Path(change['kept']) for change in self.experiment(report)['config_restored'] if change.get('kept')]
        self.assertEqual([(path / 'f').read_text() for path in kept], ['x\n'])
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    def test_every_failed_restore_blocks_new_runs(self):
        # Not only the checked differences: any error while restoring leaves the marker behind.
        for error in (RuntimeError, OSError):
            with self.subTest(error=error.__name__):
                real, calls = engine.replace_refs, []
                def failing(path):
                    calls.append(path)
                    if len(calls) > 1:  # the baseline is recorded; the first restore fails
                        raise error('cannot read refs')
                    return real(path)
                with patch('soso.engine.replace_refs', failing):
                    report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}])
                    self.assertIn('could not restore' if error is OSError else 'cannot read refs',
                                  self.experiment(report)['error'])
                    marker = json.loads((self.common_dir() / 'soso/unrestored.json').read_text())
                    self.assertEqual(marker['run'], report['run_id'])
                    with self.assertRaisesRegex(ValueError, 'could not restore'):  # the refs still cannot be read
                        Run(self.root, self.config(), 'Improve', allow_exec=True)
                # Once they can, nothing differs from the record: the next run finds it repaired.
                Run(self.root, self.config(), 'Improve', allow_exec=True)
                self.assertFalse((self.common_dir() / 'soso/unrestored.json').exists())

    def test_oversized_include_is_never_rewritten_from_a_partial_record(self):
        # A record of only its first MiB would put that back over the whole file: the rest would be lost.
        included = self.common_dir() / 'user-include'
        included.write_text('[core]\n\teol = lf\n' + '# padding\n' * 120000)
        git(self.root, 'config', 'include.path', str(included))
        edit = f"p = '{included}'; s = open(p).read(); open(p, 'w').write(s.replace('eol = lf', 'eol = crlf', 1))"
        report = self.run_candidate([{'tool': 'run', 'argv': [sys.executable, '-c', edit]},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertEqual(included.read_text(), '[core]\n\teol = crlf\n' + '# padding\n' * 120000)  # left whole, for the user
        self.assertIn('repair them there', self.experiment(report)['error'])

    def test_replaced_included_file_is_kept_privately_as_evidence(self):
        included = self.common_dir() / 'user-include'
        included.write_text('# the user\'s settings\n')
        git(self.root, 'config', 'include.path', str(included))
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', self.PLANT]}])
        kept = Path(self.experiment(report)['config_restored'][0]['kept'])
        self.assertIn(self.STUB_FILTER, kept.read_text())
        self.assertEqual(kept.stat().st_mode & 0o777, 0o600)
        self.assertTrue(kept.is_relative_to(self.common_dir() / 'soso' / report['run_id']))

    def test_runner_commits_ignore_repository_hooks(self):
        # A pre-commit hook would refuse the commit, and a hook's side effects could feed the evaluator.
        hook = self.common_dir() / 'hooks/pre-commit'
        hook.parent.mkdir(exist_ok=True)
        ran = self.common_dir() / 'hook-ran'
        hook.write_text(f'#!/bin/sh\ntouch "{ran}"\nexit 1\n')
        hook.chmod(0o755)
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}], allow_exec=False))
        self.assertFalse(ran.exists())

    # Symlinks, the reviewer's attributes, untracked files and replace refs.
    def test_symlink_out_of_the_worktree_is_not_scored(self):
        link = 'mkdir -p ../stash && echo 999 > ../stash/v && rm value.txt && ln -s ../stash/v value.txt'
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', link]}])
        self.assert_scored_as_committed(report, score_of(VALUE))
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'symlinks must stay inside the worktree'})

    def test_absolute_symlink_is_not_scored(self):
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'ln -s "$PWD/value.txt" alias.txt']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'symlinks must stay inside the worktree'})

    def two_round_review(self, plans=None, allow_exec=False):
        # Round 1 adds a visible -diff attribute; round 2's change must still reach the reviewer as text.
        plans = iter(plans or [[{'tool': 'write', 'path': '.gitattributes', 'content': 'value.txt -diff\n'},
                                {'tool': 'write', 'path': 'value.txt', 'content': '5'}],
                               [{'tool': 'write', 'path': 'value.txt', 'content': '999'}]])
        steps, prompts = iter(()), []
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            nonlocal steps
            router.budget.claim()
            if role == 'reviewer':
                prompts.append(prompt)
                return {'approve': '+999' in prompt or '+5' in prompt}
            step = next(steps, None)
            if step is None:
                steps = iter(next(plans) + [{'tool': 'done', 'summary': 'done'}])
                step = next(steps)
            return step
        runner = Run(self.root, self.config(parallel=1, rounds=2, patience=2,
                                            routes={'worker': ['test'], 'reviewer': ['test']}), 'Improve', allow_exec=allow_exec)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        return report, prompts

    def test_diff_over_the_output_limit_is_withheld_and_not_approved(self):
        # The full diff exceeds what the runner reads; it must not fall back to the attributes' view.
        big = ("python3 -c \"open('big.py', 'w').write('import os\\n' + 'x = 1  # padding\\n' * 150000 + "
               "'os.system(\\'true\\')\\n')\"")
        report, prompts = self.two_round_review([
            [{'tool': 'write', 'path': '.gitattributes', 'content': 'big.py -diff\n'},
             {'tool': 'write', 'path': 'value.txt', 'content': '5'}],
            [{'tool': 'run', 'argv': ['sh', '-c', big]}, {'tool': 'write', 'path': 'value.txt', 'content': '999'}]],
            allow_exec=True)
        self.assertEqual(len(prompts), 1)
        review = report['rounds'][1]['experiments'][0]['review']
        self.assertFalse(review['approve'])
        self.assertIn('exceeds 2 MB', review['reason'])  # the cause, so the worker can split the change
        self.assertEqual(report['best'], 5)

    def reviewed(self, base, commit='HEAD'):
        """review_diff's (diff, reason); reason is set when the change is withheld from the reviewer."""
        result = engine.review_diff(self.root, base, commit)
        return result if isinstance(result, tuple) else (result, None if result is not None else 'withheld')

    def change(self, name, before, after):
        """Commit before, then after, as name; return the first commit."""
        (self.root / name).write_bytes(before)
        self.commit('before', name)
        base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / name).write_bytes(after)
        self.commit('after', name)
        return base

    def test_text_file_turned_binary_by_a_nul_byte_is_shown_as_text(self):
        # One NUL byte makes Git show only "Binary files differ" for a file the base held as text.
        diff, reason = self.reviewed(self.change('data.txt', b'safe\n', b'gamed\n\0'))
        self.assertIsNone(reason)
        self.assertIn('-safe', diff)
        self.assertIn('+gamed', diff)

    def test_large_binary_files_are_approvable(self):
        blob = bytes(range(256)) * 9000  # 2.3 MB, with a NUL byte at the start
        for name, before, after in (('added.bin', b'', blob), ('changed.bin', blob, blob[::-1])):
            with self.subTest(name=name):
                diff, reason = self.reviewed(self.change(name, before, after))
                self.assertIsNone(reason)
                self.assertIn('Binary files', diff)

    def test_emptied_or_filled_placeholder_is_approvable(self):
        for name, before, after in (('filled.bin', b'', b'\0\1\2 data'), ('emptied.bin', b'\0\1\2 data', b'')):
            with self.subTest(name=name):
                diff, reason = self.reviewed(self.change(name, before, after))
                self.assertIsNone(reason)
                self.assertIn(name, diff)

    def test_conversion_to_text_and_a_removed_nul_are_shown_as_text(self):
        for name, before, after, line in (('notes.txt', 'hello\n'.encode('utf-16'), b'hello\n', '+hello'),
                                          ('clean.txt', b'value\n\0', b'value\n', '-\0')):
            with self.subTest(name=name):
                diff, reason = self.reviewed(self.change(name, before, after))
                self.assertIsNone(reason)
                self.assertIn(line, diff)

    def test_old_git_shows_a_committed_lockfile_marked_minus_diff_as_text(self):
        (self.root / '.gitattributes').write_text('package-lock.json -diff\n')
        self.commit('attributes', '.gitattributes')
        base = self.change('package-lock.json', b'{"lockfileVersion": 2}\n', b'{"lockfileVersion": 3}\n')
        with self.old_git():
            diff, reason = self.reviewed(base)
        self.assertIsNone(reason)
        self.assertIn('+{"lockfileVersion": 3}', diff)

    def lockfile_change(self, size=1):
        """A text file marked -diff in the repository, changed: binary-shown on an older Git."""
        (self.root / '.gitattributes').write_text('*.lock -diff\n')
        self.commit('attributes', '.gitattributes')
        return self.change(f'deps{size}.lock', b'version = 1\n' * size, b'version = 2\n' * size)

    def test_side_that_cannot_be_read_is_withheld(self):
        base = self.lockfile_change()
        with self.old_git(), patch('soso.engine.blob_kind', return_value=None):
            diff, reason = self.reviewed(base)
        self.assertIsNone(diff)
        self.assertIn('could not read deps1.lock', reason)

    def test_files_that_cannot_be_matched_are_withheld(self):
        base = self.lockfile_change()
        real = engine.git
        def one_short(root, *args, **kwargs):  # the numstat listing loses its first entry
            output = real(root, *args, **kwargs)
            return output.split('\0', 1)[1] if '--numstat' in args else output
        with self.old_git(), patch('soso.engine.git', one_short):
            self.assertEqual(self.reviewed(base), (None, "could not match the diff's files to show them"))

    def test_text_shown_for_binary_marked_files_counts_toward_the_limit(self):
        # Each file's text is under 2 MB, but together with the rest the reviewer would get more.
        base = git(self.root, 'rev-parse', 'HEAD')
        self.lockfile_change(90000)  # about 1.1 MB as text
        self.change('more.lock', b'a = 1\n' * 200000, b'a = 2\n' * 200000)
        with self.old_git():
            diff, reason = self.reviewed(base)
        self.assertIn('exceeds 2 MB', reason)

    def test_stderr_never_mixes_into_parsed_listings(self):
        base = self.lockfile_change()
        with self.old_git(), patch.dict(os.environ, {'GIT_TRACE': '1'}):  # Git then writes to stderr
            diff, reason = self.reviewed(base)
        self.assertIsNone(reason)
        self.assertIn('+version = 2', diff)

    def test_file_name_that_is_not_utf8_is_never_shown_only_as_binary(self):
        # Built from raw ids, so no checkout is needed (macOS cannot write this name).
        (self.common_dir() / 'info/attributes').write_text('*.py -diff\n')  # applies whatever --attr-source says
        def commit_with(content, parent):
            blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.root, input=content,
                                  capture_output=True, check=True).stdout.strip()
            tree = subprocess.run(['git', 'mktree'], cwd=self.root, input=b'100644 blob ' + blob + b'\t\xff.py\n',
                                  capture_output=True, check=True).stdout.decode().strip()
            return git(self.root, '-c', 'commit.gpgsign=false', 'commit-tree', tree, '-p', parent, '-m', 'raw')
        base = commit_with(b'x = 0\n', git(self.root, 'rev-parse', 'HEAD'))
        diff, reason = self.reviewed(base, commit_with(b'x = 999\n', base))
        self.assertTrue(reason or '+x = 999' in diff, diff)

    def test_old_git_review_sees_through_quoted_tabbed_and_unprefixed_names(self):
        # Without --attr-source, names come from --numstat -z, not from parsing patch text.
        for name, setting in (('mé.py', None), ('tab\there.py', None), ('plain.py', 'diff.noprefix')):
            with self.subTest(name=name):
                if setting:
                    git(self.root, 'config', setting, 'true')
                quoted = '"' + name.replace('\t', '\\t') + '"' if '\t' in name else name
                (self.root / '.gitattributes').write_text(quoted + ' -diff\n')
                (self.root / name).write_text('x = 0\n')
                self.commit('attributes', '.gitattributes', name)
                base = git(self.root, 'rev-parse', 'HEAD')
                (self.root / name).write_text('x = 999\n')
                self.commit('candidate', name)
                with self.old_git():
                    diff, reason = self.reviewed(base)
                self.assertIsNone(reason)
                self.assertIn('+x = 999', diff)
                if setting:
                    git(self.root, 'config', '--unset', setting)

    def test_attributes_accepted_earlier_do_not_hide_a_later_change_from_the_reviewer(self):
        report, prompts = self.two_round_review()
        self.assertEqual(len(prompts), 2)
        self.assertIn('+999', prompts[1])
        self.assertEqual(report['best'], 999)

    def test_without_attr_source_a_text_file_shown_as_binary_is_shown_as_text(self):
        with self.old_git():
            report, prompts = self.two_round_review()
        self.assertEqual(len(prompts), 2)
        self.assertIn('+999', prompts[1])  # rendered as text despite -diff
        self.assertEqual(report['best'], 999)

    def test_delivery_reviewer_sees_a_text_file_shown_as_binary_as_text(self):
        (self.root / '.gitattributes').write_text('value.txt -diff\n')
        self.commit('attributes', '.gitattributes')
        base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'value.txt').write_text('999')
        self.commit('candidate', 'value.txt')
        commit = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'checkout', '-q', '--detach', base)
        asked = []
        def ask(role, prompt, cwd, task=None):
            asked.append(prompt)
            return {'approve': True, 'useful': True}
        epoch = SimpleNamespace(integration=self.root, router=SimpleNamespace(ask=ask),
                                intent='Improve', contract={}, report={'baseline': {}})
        candidate = {'commit': commit, 'evaluation': {}}
        with self.old_git():
            self.assertTrue(DeliveryEpoch.review(epoch, candidate))
        self.assertIn('+999', asked[0])

    def test_delivery_withholds_an_oversized_diff_and_tells_the_worker_why(self):
        base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'big.py').write_text('x = 1  # padding\n' * 150000)
        self.commit('candidate', 'big.py')
        commit = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'checkout', '-q', '--detach', base)
        asked = []
        epoch = SimpleNamespace(integration=self.root, router=SimpleNamespace(ask=lambda *a, **k: asked.append(a)),
                                intent='Improve', contract={}, report={'baseline': {}})
        candidate = {'commit': commit, 'evaluation': {}}
        self.assertFalse(DeliveryEpoch.review(epoch, candidate))
        self.assertEqual(asked, [])
        # useful with a reason: repair_candidate keeps the draft and hands the reason to the worker
        self.assertEqual((candidate['review']['useful'], 'exceeds 2 MB' in candidate['review']['reason']), (True, True))

    def test_untracked_files_are_committed_when_the_worker_hides_them_from_status(self):
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', 'git config status.showUntrackedFiles no']},
                                     {'tool': 'write', 'path': 'extra.txt', 'content': 'new'}])
        self.assertIn('extra.txt', git(self.root, 'ls-tree', '--name-only', self.experiment(report)['commit']))

    def test_info_exclude_is_restored(self):
        exclude = self.common_dir() / 'info/exclude'
        before = exclude.read_bytes()
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'echo "*.txt" >> "{exclude}"']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertEqual(exclude.read_bytes(), before)
        self.assertIn('info/exclude', self.restored_keys(report))

    def test_replace_refs_the_worker_adds_are_removed(self):
        replace = 'git replace "$(git hash-object -w --stdin < /dev/null)" "$(git rev-parse HEAD:value.txt)"'
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', replace]},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertEqual(git(self.root, 'for-each-ref', 'refs/replace/'), '')
        self.assertTrue(any(key.startswith('refs/replace/') for key in self.restored_keys(report)), report)
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    # Checks around the checkout itself.
    def test_attributes_that_change_a_protected_file_at_checkout_are_invalid(self):
        report = self.run_candidate([{'tool': 'write', 'path': '.gitattributes', 'content': 'evaluate.py text eol=crlf\n'},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}], allow_exec=False)
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'checkout changed protected files'})

    def test_config_change_during_checkout_is_invalid(self):
        checkout = engine.checkout
        def sibling_changes_config(*args):
            left = checkout(*args)
            git(self.root, 'config', 'core.safecrlf', 'warn')  # e.g. a sibling worker
            return left
        with patch('soso.engine.checkout', sibling_changes_config):
            report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertEqual(self.experiment(report)['evaluation'],
                         {'valid': False, 'reason': 'repository configuration changed during checkout'})
        self.assertEqual(git(self.root, 'config', '--get', 'core.safecrlf', ok=(0, 1)), '')

    def test_files_that_differ_from_the_commit_after_checkout_are_invalid(self):
        # e.g. paths that collide on a case-insensitive file system; one write then replaces another.
        checkout = engine.checkout
        def colliding(path, *args):
            left = checkout(path, *args)
            (Path(path) / 'value.txt').write_text('999')
            return left
        with patch('soso.engine.checkout', colliding):
            report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertEqual(self.experiment(report)['evaluation'], {'valid': False, 'reason': 'checked-out files differ from the commit'})
        self.assertNotIn('error', self.experiment(report))

    # Legitimate candidates in repositories that use conversions, links and sparse checkouts stay valid.
    def assert_valid(self, report, score=5):
        experiment = self.experiment(report)
        self.assertEqual(experiment['evaluation'].get('score'), score, experiment)
        self.assertNotIn('config_restored', experiment)
        self.assertEqual(report['best'], score)

    def test_ident_file_in_the_base_stays_valid(self):
        (self.root / '.gitattributes').write_text('id.txt ident\n')
        (self.root / 'id.txt').write_text('$Id$\n')
        self.commit('ident', '.gitattributes', 'id.txt')
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}]))

    def test_working_tree_encoding_file_in_the_base_stays_valid(self):
        (self.root / '.gitattributes').write_text('utf16.txt working-tree-encoding=UTF-16\n')
        (self.root / 'utf16.txt').write_bytes('café\n'.encode('utf-16'))
        self.commit('encoding', '.gitattributes', 'utf16.txt')
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}]))

    def test_users_round_tripping_filter_stays_valid(self):
        rot13 = "tr 'A-Za-z' 'N-ZA-Mn-za-m'"
        git(self.root, 'config', 'filter.rot.clean', rot13)
        git(self.root, 'config', 'filter.rot.smudge', rot13)
        (self.root / '.gitattributes').write_text('secret.txt filter=rot\n')
        (self.root / 'secret.txt').write_text('hello\n')
        self.commit('filter', '.gitattributes', 'secret.txt')
        report = self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}],
                                    score_of("5 if open('secret.txt').read() == 'hello\\n' else 0"))
        self.assert_valid(report)

    def test_symlink_and_submodule_in_the_base_stay_valid(self):
        (self.root / 'link.txt').symlink_to('value.txt')
        self.commit('link', 'link.txt')
        (self.root / 'sub').mkdir()
        identity = ('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false')
        git(self.root / 'sub', 'init', '-q')
        (self.root / 'sub/f.txt').write_text('x')
        git(self.root / 'sub', 'add', 'f.txt')
        git(self.root / 'sub', *identity, 'commit', '-qm', 'nested')
        self.commit('submodule', 'sub')
        evaluate = score_of("int(open('link.txt').read()) if __import__('os').path.isdir('sub') else -1")
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}], evaluate))

    def test_sparse_cone_with_sparse_index_stays_valid(self):
        self.sparse_repository('--sparse-index')
        evaluate = score_of(VALUE + " + (1000 if __import__('os').path.exists('extra') else 0)")
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}], evaluate))

    def test_users_symlinked_info_exclude_stays_valid(self):
        # e.g. info/exclude linked to a shared ignore file: recorded and restored at its target.
        target, exclude = self.common_dir() / 'shared-ignore', self.common_dir() / 'info/exclude'
        target.write_text('*.log\n')
        exclude.unlink()
        exclude.symlink_to(target)
        for allow_exec in (False, True):
            with self.subTest(allow_exec=allow_exec):
                self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}],
                                                     allow_exec=allow_exec))
                self.assertTrue(exclude.is_symlink())
                git(self.root, 'checkout', '-q', '--', 'value.txt')

    def test_symlinked_info_exclude_is_restored_at_its_target(self):
        target, exclude = self.common_dir() / 'shared-ignore', self.common_dir() / 'info/exclude'
        target.write_text('*.log\n')
        exclude.unlink()
        exclude.symlink_to(target)
        report = self.run_candidate([{'tool': 'run', 'argv': ['sh', '-c', f'echo "*.txt" >> "{exclude}"']},
                                     {'tool': 'write', 'path': 'value.txt', 'content': '5'}])
        self.assertTrue(exclude.is_symlink())
        self.assertEqual(target.read_text(), '*.log\n')
        self.assertIn('info/exclude', self.restored_keys(report))

    def test_device_files_named_by_the_users_config_stay_valid(self):
        # Nothing can be put back into /dev/null, so it is not recorded, and it does not stop a run.
        exclude = self.common_dir() / 'info/exclude'
        exclude.unlink()
        exclude.symlink_to('/dev/null')
        git(self.root, 'config', 'include.path', '/dev/null')
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}]))

    def test_users_file_mode_and_symlinks_off_stay_valid(self):
        (self.root / 'link.txt').symlink_to('value.txt')
        self.commit('link', 'link.txt')
        git(self.root, 'config', 'core.fileMode', 'false')
        git(self.root, 'config', 'core.symlinks', 'false')
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}]))


if __name__ == '__main__':
    import unittest
    unittest.main()
