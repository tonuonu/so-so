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

    # A worker that sets diff.* is rejected and restored (see below); the user's own external diff
    # program or textconv driver, e.g. a structural diff tool, must not shape the reviewer's view either.
    def test_external_diff_does_not_hide_the_change_from_the_reviewer(self):
        git(self.root, 'config', 'diff.external', 'true')
        diff = self.reviewed_diff([{'tool': 'write', 'path': 'value.txt', 'content': '999'}])
        self.assertIn('+999', diff)

    def test_textconv_does_not_hide_the_change_from_the_reviewer(self):
        # The diff reads attributes from the result worktree, so the diff driver comes from the base.
        (self.root / '.gitattributes').write_text('value.txt diff=hide\n')
        self.commit('attributes', '.gitattributes')
        git(self.root, 'config', 'diff.hide.textconv', 'true')
        diff = self.reviewed_diff([{'tool': 'write', 'path': 'value.txt', 'content': '999'}])
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
        self.assertEqual(evidence, [{'key': 'included:' + str(included.resolve()), 'restored': [], 'found': ['core.eol=crlf']}])
        self.assertNotIn('secret-token', json.dumps(report))

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

    def two_round_review(self):
        # Round 1 adds a visible -diff attribute; round 2's change must still reach the reviewer as text.
        plans = iter([[{'tool': 'write', 'path': '.gitattributes', 'content': 'value.txt -diff\n'},
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
                                            routes={'worker': ['test'], 'reviewer': ['test']}), 'Improve', allow_exec=False)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        return report, prompts

    def test_attributes_accepted_earlier_do_not_hide_a_later_change_from_the_reviewer(self):
        report, prompts = self.two_round_review()
        self.assertEqual(len(prompts), 2)
        self.assertIn('+999', prompts[1])
        self.assertEqual(report['best'], 999)

    def test_without_attr_source_a_text_file_shown_as_binary_is_not_approved(self):
        real = engine.git
        def old_git(root, *args, **kwargs):
            if any(str(arg).startswith('--attr-source') for arg in args):
                raise RuntimeError('git: unknown option')
            return real(root, *args, **kwargs)
        with patch('soso.engine.git', old_git):
            report, prompts = self.two_round_review()
        self.assertEqual(len(prompts), 1)  # the second candidate never reaches a model with a blind diff
        self.assertEqual(report['rounds'][1]['experiments'][0]['review']['approve'], False)
        self.assertEqual(report['best'], 5)

    def test_delivery_reviewer_is_not_asked_about_a_text_file_shown_as_binary(self):
        (self.root / '.gitattributes').write_text('value.txt -diff\n')
        self.commit('attributes', '.gitattributes')
        base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'value.txt').write_text('999')
        self.commit('candidate', 'value.txt')
        commit = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'checkout', '-q', '--detach', base)
        real = engine.git
        def old_git(root, *args, **kwargs):
            if any(str(arg).startswith('--attr-source') for arg in args):
                raise RuntimeError('git: unknown option')
            return real(root, *args, **kwargs)
        asked = []
        epoch = SimpleNamespace(integration=self.root, router=SimpleNamespace(ask=lambda *a, **k: asked.append(a)),
                                intent='Improve', contract={}, report={'baseline': {}})
        candidate = {'commit': commit, 'evaluation': {}}
        with patch('soso.engine.git', old_git):
            self.assertFalse(DeliveryEpoch.review(epoch, candidate))
        self.assertEqual(asked, [])
        self.assertFalse(candidate['review']['approve'])

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

    def test_users_file_mode_and_symlinks_off_stay_valid(self):
        (self.root / 'link.txt').symlink_to('value.txt')
        self.commit('link', 'link.txt')
        git(self.root, 'config', 'core.fileMode', 'false')
        git(self.root, 'config', 'core.symlinks', 'false')
        self.assert_valid(self.run_candidate([{'tool': 'write', 'path': 'value.txt', 'content': '5'}]))


if __name__ == '__main__':
    import unittest
    unittest.main()
