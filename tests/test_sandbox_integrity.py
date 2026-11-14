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
from soso.engine import Run, evaluate, fingerprint, local_programs, watched_config
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
        report, _ = self.run_steps([{'tool': 'write', 'path': 'data/.gitignore', 'content': '*\n'},
                                    {'tool': 'write', 'path': 'data/bonus.txt', 'content': '999'},
                                    {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                                    {'tool': 'done', 'summary': 'done'}], evaluate=[sys.executable, '-c', code])
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
        report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', nested]}, {'tool': 'done', 'summary': 'done'}],
                                   allow_exec=True, evaluate=[sys.executable, '-c', code])
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
        report, runner = self.run_steps([{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
        self.assertIn('S extra/x.txt', git(runner.integration, 'ls-files', '-v'))
        self.assertEqual(report['rounds'][0]['experiments'][0]['evaluation'].get('score'), 5, report['rounds'][0]['experiments'][0]['evaluation'])
        self.assertEqual(report['best'], 5)

    def test_candidate_hiding_worktree_changes_with_index_flags_is_invalid(self):
        # The flagged edit stays on disk but out of the commit, so the score would not match the tree.
        for flag in ('--skip-worktree', '--assume-unchanged'):
            with self.subTest(flag=flag):
                hide = f'git update-index {flag} value.txt && echo 999 > value.txt'
                report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', hide]},
                                            {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                                            {'tool': 'done', 'summary': 'done'}], allow_exec=True)
                experiment = report['rounds'][0]['experiments'][0]
                self.assertTrue(experiment.get('commit'), experiment)
                self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'index flags hide worktree changes'})
                self.assertEqual(report['best'], 0)

    def run_steps(self, steps, allow_exec=False, **config):
        """One candidate taking the given worker steps; returns (report, runner)."""
        steps = iter(steps)
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            return next(steps)
        runner = Run(self.root, self.config(**{'parallel': 1, 'rounds': 1, **config}), 'Improve', allow_exec=allow_exec)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            return runner.run(), runner

    def hidden_score_run(self, command, evaluate=None):
        # Run one exec-enabled candidate that also makes a tracked change; return its evaluation.
        report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', command]},
                                    {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                                    {'tool': 'done', 'summary': 'done'}],
                                   allow_exec=True, **({'evaluate': evaluate} if evaluate else {}))
        self.assertEqual(report['best'], 0)
        return report['rounds'][0]['experiments'][0]['evaluation']

    def test_candidate_hiding_files_in_a_nested_git_entry_is_invalid(self):
        # Git skips every path named .git, so pkg/.git/bonus.txt is neither committed nor cleaned.
        code = ("import json\nfrom pathlib import Path\nbonus=Path('pkg/.git/bonus.txt')\n"
                "print(json.dumps({'score':int(Path('value.txt').read_text())+(int(bonus.read_text()) if bonus.exists() else 0)}))\n")
        evaluation = self.hidden_score_run('mkdir -p pkg/.git && echo 999 > pkg/.git/bonus.txt', [sys.executable, '-c', code])
        self.assertEqual(evaluation, {'valid': False, 'reason': 'nested .git entries hide files from Git'})

    def test_candidate_hiding_edits_with_an_fsmonitor_hook_is_invalid(self):
        # A hook that reports no changes plus fsmonitor-valid bits hides the edit from status and staging.
        hide = r"""h="$(git rev-parse --absolute-git-dir)/stub" && printf "#!/bin/sh\nprintf 'tok\\0'\n" > "$h" && chmod +x "$h" && git config core.fsmonitor "$h" && git status --porcelain > /dev/null && git update-index --fsmonitor-valid value.txt && echo 999 > value.txt"""
        evaluation = self.hidden_score_run(hide)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'worker changed repository configuration'})

    def config_value(self, key):
        return git(self.root, 'config', '--get', key, ok=(0, 1))

    def events(self, name):
        return [json.loads(line) for path in sorted(self.common_dir().glob('soso/*/events.jsonl'))
                for line in path.read_text().splitlines() if json.loads(line)['event'] == name]

    @staticmethod
    def restored_keys(experiment):
        return [change['key'] for change in experiment.get('config_restored', [])]

    def improving_run(self, answer=None, allow_exec=True, **config):
        # One candidate that sets value.txt to 5; returns its experiment record.
        steps = iter([{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
        def default(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            return next(steps)
        runner = Run(self.root, self.config(**{'parallel': 1, 'rounds': 1, **config}), 'Improve', allow_exec=allow_exec)
        with patch.object(Router, 'ask', answer or default), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        return report['rounds'][0]['experiments'][0]

    def test_candidate_changing_repository_config_is_invalid_and_config_is_restored(self):
        # A clean filter in the shared config commits other content than the evaluated value.txt.
        attributes = self.common_dir() / 'info/attributes'
        self.assertFalse(attributes.exists())
        change = ('git config filter.shadow.clean "sed s/1/999/" && '
                  'printf "value.txt filter=shadow\\n" > "$(git rev-parse --git-common-dir)/info/attributes" && '
                  'echo 1 > value.txt')
        evaluation = self.hidden_score_run(change)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(self.config_value('filter.shadow.clean'), '')
        self.assertFalse(attributes.exists())
        restored = self.events('repository_config_restored')
        self.assertEqual(restored[0]['keys'], ['local:filter.shadow.clean', 'info/attributes'])
        self.assertNotIn('sed', json.dumps(restored))  # names only, never values
        evidence = self.events('experiment')[0]['config_restored']
        self.assertEqual(evidence[0], {'key': 'local:filter.shadow.clean', 'restored': [], 'found': ['sed s/1/999/']})

    def test_watched_config_change_without_source_changes_is_restored(self):
        # The candidate returns early ("no changes"); its changes must not leak into later candidates.
        git(self.root, 'config', 'core.autocrlf', 'false')
        attributes = self.common_dir() / 'info/attributes'
        attributes.write_text('*.bin binary\n')
        change = 'git config core.autocrlf true && echo "* text" >> "$(git rev-parse --git-common-dir)/info/attributes"'
        report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', change]}, {'tool': 'done', 'summary': 'done'}],
                                   allow_exec=True)
        experiment = report['rounds'][0]['experiments'][0]
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'no changes'})
        self.assertEqual(self.restored_keys(experiment), ['local:core.autocrlf', 'info/attributes'])
        self.assertEqual(self.config_value('core.autocrlf'), 'false')
        self.assertEqual(attributes.read_text(), '*.bin binary\n')

    def test_watched_config_change_is_restored_when_the_worker_fails(self):
        from soso.models import ModelUnavailable
        steps = iter([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.autocrlf true']}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            for step in steps:
                return step
            raise ModelUnavailable(reason='provider paused')
        runner = Run(self.root, self.config(parallel=1, rounds=1), 'Improve', allow_exec=True)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        self.assertEqual(report['status'], 'waiting')
        self.assertEqual(self.config_value('core.autocrlf'), '')

    def test_crlf_file_edits_stay_valid_under_line_ending_settings(self):
        # A file stored with CRLF is not renormalised, so its edit is committed as written.
        (self.root / 'crlf.txt').write_bytes(b'a\r\nb\r\n')
        git(self.root, 'add', 'crlf.txt')
        git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'crlf file')
        variants = [('attributes', None), ('autocrlf', 'true'), ('autocrlf', 'input')]
        for kind, value in variants:
            with self.subTest(kind=kind, value=value):
                if kind == 'attributes':
                    (self.root / '.gitattributes').write_text('* text=auto\n')
                    git(self.root, 'add', '.gitattributes')
                    git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'text=auto')
                else:
                    git(self.root, 'config', 'core.autocrlf', value)
                steps = iter([{'tool': 'write', 'path': 'crlf.txt', 'content': 'a\r\nc\r\n'},
                              {'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
                def answer(router, role, prompt, cwd, attempt=0, task=None):
                    router.budget.claim()
                    if role == 'reviewer': return {'approve': True}
                    return next(steps)
                experiment = self.improving_run(answer)
                self.assertEqual(experiment['evaluation'].get('score'), 5, experiment['evaluation'])
                self.assertNotIn('config_restored', experiment)

    def test_relative_worktree_paths_setting_keeps_candidates_valid(self):
        # git worktree add then writes extensions.relativeWorktrees, which is not a watched setting.
        git(self.root, 'config', 'worktree.useRelativePaths', 'true')
        experiment = self.improving_run()
        self.assertEqual(experiment['evaluation'].get('score'), 5, experiment['evaluation'])

    def test_concurrent_unwatched_config_change_survives_and_candidate_stays_valid(self):
        # The user adds a remote while the worker runs; only watched settings are ever restored.
        steps = iter([{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            step = next(steps)
            if step['tool'] == 'write':
                git(self.root, 'remote', 'add', 'origin', 'https://example.invalid/project.git')
            return step
        experiment = self.improving_run(answer)
        self.assertEqual(experiment['evaluation'].get('score'), 5, experiment['evaluation'])
        self.assertEqual(self.config_value('remote.origin.url'), 'https://example.invalid/project.git')
        self.assertNotIn('config_restored', experiment)

    # Four ways a worker with command execution made Git stage other content than the files the
    # evaluator then scored (999 on disk, 1 in the commit). The back-dated file keeps Git's stat
    # cache from re-hashing it, as it would for any file not written in the last second.
    STUB_FILTER = 'sed s/999/1/'

    def test_filtered_blob_left_in_the_stat_cache_is_invalid(self):
        # (a) Configure a filter, stage with it, then remove the configuration again.
        change = (f'git config filter.x.clean "{self.STUB_FILTER}" && '
                  'printf "value.txt filter=x\\n" > "$(git rev-parse --git-common-dir)/info/attributes" && '
                  'echo 999 > value.txt && touch -t 200001010000 value.txt && git add value.txt && git config --unset filter.x.clean && '
                  'rm "$(git rev-parse --git-common-dir)/info/attributes"')
        evaluation = self.hidden_score_run(change)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'evaluated files differ from the commit'})

    def test_filter_given_on_the_command_line_is_invalid(self):
        # (b) No configuration is written at all.
        change = ('a="$(git rev-parse --absolute-git-dir)/attrs" && printf "value.txt filter=x\\n" > "$a" && '
                  f'echo 999 > value.txt && touch -t 200001010000 value.txt && git -c filter.x.clean="{self.STUB_FILTER}" -c core.attributesFile="$a" add value.txt')
        evaluation = self.hidden_score_run(change)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'evaluated files differ from the commit'})

    def test_worktree_config_enabled_by_the_worker_is_invalid_and_restored(self):
        # (c) The worktree scope did not exist when the candidate worktree was created.
        change = ('a="$(git rev-parse --absolute-git-dir)/attrs" && printf "value.txt filter=x\\n" > "$a" && '
                  'git config extensions.worktreeConfig true && '
                  f'git config --worktree filter.x.clean "{self.STUB_FILTER}" && git config --worktree core.attributesFile "$a" && '
                  'echo 999 > value.txt && touch -t 200001010000 value.txt && git add value.txt')
        evaluation = self.hidden_score_run(change)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(self.config_value('extensions.worktreeConfig'), '')
        keys = [change['key'] for change in self.events('experiment')[0]['config_restored']]
        self.assertEqual(keys, ['local:extensions.worktreeconfig', 'worktree:filter.x.clean', 'worktree:core.attributesfile'])

    def test_filter_behind_an_include_is_invalid_and_restored(self):
        # (d) The filter lives in an included file; include.path is the only key in .git/config.
        change = ('d="$(git rev-parse --absolute-git-dir)" && printf "value.txt filter=x\\n" > "$d/attrs" && '
                  f'printf "[filter \\"x\\"]\\n\\tclean = {self.STUB_FILTER}\\n[core]\\n\\tattributesFile = $d/attrs\\n" > "$d/included" && '
                  'git config include.path "$d/included" && echo 999 > value.txt && touch -t 200001010000 value.txt && git add value.txt')
        evaluation = self.hidden_score_run(change)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(self.config_value('include.path'), '')

    def test_change_inside_an_existing_included_file_is_not_accepted(self):
        # The user's include.path is unchanged, so no watched key in .git/config changes; the included
        # file's recorded bytes are put back, and the candidate that changed it is not scored.
        included = self.common_dir() / 'user-include'
        included.write_text('')
        git(self.root, 'config', 'include.path', str(included))
        change = ('c="$(git rev-parse --git-common-dir)" && printf "value.txt filter=x\\n" > "$c/attrs" && '
                  f'printf "[filter \\"x\\"]\\n\\tclean = {self.STUB_FILTER}\\n[core]\\n\\tattributesFile = $(cd "$c" && pwd)/attrs\\n" >> "$c/user-include" && '
                  'echo 999 > value.txt')
        evaluation = self.hidden_score_run(change)
        self.assertEqual(evaluation, {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(included.read_text(), '')
        self.assertEqual(self.restored_keys(self.events('experiment')[0]), ['included:' + str(included)])

    def test_failed_restore_of_an_included_setting_leaves_repository_config_untouched(self):
        # The user's includes supply core.eol, one only once a remote matches. The worker adds such a remote
        # (unwatched) and writes info/attributes. The included files are unchanged, so no recorded bytes can
        # put core.eol back: no override may be written into .git/config; the attributes are still restored.
        included, conditional = self.common_dir() / 'user-include', self.common_dir() / 'conditional'
        included.write_text('[core]\n\teol = lf\n')
        conditional.write_text('[core]\n\teol = crlf\n')
        git(self.root, 'config', 'include.path', str(included))
        git(self.root, 'config', 'includeIf.hasconfig:remote.*.url:https://example.invalid/**.path', str(conditional))
        change = ('git remote add origin https://example.invalid/p.git && '
                  'printf "*.bin binary\n" > "$(git rev-parse --git-common-dir)/info/attributes" && echo 5 > value.txt')
        evaluation = self.hidden_score_run(change)
        self.assertFalse(evaluation['valid'])
        experiment = self.events('experiment')[0]
        self.assertIn(str(conditional.resolve()), experiment['error'])
        self.assertIn('core.eol', experiment['error'])
        self.assertEqual(git(self.root, 'config', '--file', str(self.common_dir() / 'config'), '--get-all', 'core.eol', ok=(0, 1)), '')
        self.assertFalse((self.common_dir() / 'info/attributes').exists())
        self.assertEqual(self.restored_keys(experiment), ['info/attributes'])
        self.assertEqual([e['keys'] for e in self.events('repository_config_restored')], [['info/attributes']])

    def test_commit_comparison_leaves_the_candidate_index_untouched(self):
        # Later checks read the candidate's index (worker-set flags, status), so the comparison must use
        # its own index: read-tree into the real one would drop flags and staged state.
        from soso.engine import differs_from_commit
        (self.root / 'value.txt').write_text('7')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'update-index', '--assume-unchanged', 'evaluate.py')
        before = (git(self.root, 'ls-files', '-v'), git(self.root, 'diff', '--cached', '--name-only'))
        self.assertEqual(differs_from_commit(self.root, 'HEAD', set()), ['value.txt'])
        self.assertEqual((git(self.root, 'ls-files', '-v'), git(self.root, 'diff', '--cached', '--name-only')), before)

    def test_user_setting_during_a_run_without_exec_is_restored_and_blamed(self):
        # Without command execution a worker still runs the checks and evaluator (check), which can change
        # config, so a watched change made while it runs alone is restored and blamed on it, as with exec.
        steps = iter([{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            step = next(steps)
            if step['tool'] == 'write':
                git(self.root, 'config', 'core.autocrlf', 'input')
            return step
        experiment = self.improving_run(answer, allow_exec=False)
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(self.config_value('core.autocrlf'), '')
        self.assertEqual(self.restored_keys(experiment), ['local:core.autocrlf'])

    @staticmethod
    def planted(*settings, attributes=None):
        """A json.py that evaluate.py imports from its own directory during check: it sets settings and
        info/attributes, then stands in for json.dumps."""
        lines = ["import os, subprocess",
                 "git = lambda *a: subprocess.run(['git', *a], capture_output=True, text=True).stdout.strip()"]
        lines += [f"git('config', {key!r}, {value!r})" for key, value in settings]
        if attributes:
            lines += ["info = os.path.join(git('rev-parse', '--git-common-dir'), 'info')",
                      "os.makedirs(info, exist_ok=True)", f"open(os.path.join(info, 'attributes'), 'w').write({attributes!r})"]
        return "\n".join(lines + ["dumps = lambda value: '{\"score\": %d}' % value['score']"]) + "\n"

    def planted_run(self, module, steps):
        """A candidate without command execution that writes module as json.py, runs check, deletes json.py and
        takes steps; once they run out its provider pauses. Returns the report."""
        from soso.models import ModelUnavailable
        steps = iter([{'tool': 'write', 'path': 'json.py', 'content': module}, {'tool': 'check'},
                      {'tool': 'delete', 'path': 'json.py'}, *steps])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            for step in steps:
                return step
            raise ModelUnavailable(reason='provider paused')
        runner = Run(self.root, self.config(parallel=1, rounds=1), 'Improve', allow_exec=False)
        with patch.object(Router, 'ask', answer), contextlib.redirect_stdout(io.StringIO()):
            return runner.run()

    def test_filter_planted_through_check_without_exec_is_invalid_and_restored(self):
        # No run tool, but check runs evaluate.py, which imports a worker-written json.py from its own
        # directory. That module plants a smudge filter; the worker deletes it and commits a benign change.
        # Scored after a checkout under the filter, the candidate would get 100 for a commit that scores 1.
        common = self.common_dir()
        module = self.planted(('filter.v.smudge', 'sed s/^1$/100/'), ('filter.v.clean', 'sed s/^100$/1/'),
                              attributes='value.txt filter=v\n')
        report = self.planted_run(module, [{'tool': 'write', 'path': 'value.txt', 'content': '1'},
                                           {'tool': 'done', 'summary': 'done'}])
        experiment = report['rounds'][0]['experiments'][0]
        self.assertEqual(git(self.root, 'show', experiment['commit'] + ':value.txt'), '1')
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(report['best'], 0)
        self.assertEqual((self.config_value('filter.v.smudge'), self.config_value('filter.v.clean')), ('', ''))
        self.assertFalse((common / 'info/attributes').exists())
        keys = ['local:filter.v.smudge', 'local:filter.v.clean', 'info/attributes']
        self.assertEqual(self.restored_keys(experiment), keys)
        self.assertEqual([e['keys'] for e in self.events('repository_config_restored')], [keys])
        self.assertFalse((common / 'soso/unrestored.json').exists())  # restored, so new runs may start

    def test_filter_planted_through_check_is_gone_before_the_commit(self):
        # The restore after the worker comes before staging: a planted clean filter must not shape the commit.
        module = self.planted(('filter.v.clean', 'tr 1 7'), attributes='value.txt filter=v\n')
        report = self.planted_run(module, [{'tool': 'write', 'path': 'value.txt', 'content': '1'},
                                           {'tool': 'done', 'summary': 'done'}])
        experiment = report['rounds'][0]['experiments'][0]
        self.assertEqual(git(self.root, 'show', experiment['commit'] + ':value.txt'), '1')
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})

    def test_setting_planted_through_check_is_restored_when_the_worker_fails(self):
        report = self.planted_run(self.planted(('core.autocrlf', 'true')), [])
        self.assertEqual(report['status'], 'waiting')
        self.assertEqual(self.config_value('core.autocrlf'), '')
        self.assertEqual(self.restored_keys(report['rounds'][0]['experiments'][0]), ['local:core.autocrlf'])

    def test_restored_setting_in_the_middle_of_a_section_keeps_runs_going(self):
        # A restore writes core.autocrlf again after core.eol. Compared in file order, the restored settings
        # looked unrestorable: the candidate failed, unrestored.json was written and every later run refused.
        for allow_exec in (False, True):
            with self.subTest(allow_exec=allow_exec):
                git(self.root, 'config', '--unset-all', 'core.autocrlf', ok=(0, 5))
                git(self.root, 'config', '--unset-all', 'core.eol', ok=(0, 5))
                git(self.root, 'config', 'core.autocrlf', 'false')
                git(self.root, 'config', 'core.eol', 'lf')
                steps = iter([{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}])
                def answer(router, role, prompt, cwd, attempt=0, task=None):
                    router.budget.claim()
                    if role == 'reviewer': return {'approve': True}
                    step = next(steps)
                    if step['tool'] == 'write':
                        git(self.root, 'config', 'core.autocrlf', 'input')  # the owner's own edit
                    return step
                experiment = self.improving_run(answer, allow_exec=allow_exec)
                self.assertNotIn('error', experiment)
                self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
                self.assertEqual(self.restored_keys(experiment), ['local:core.autocrlf'])
                self.assertEqual((self.config_value('core.autocrlf'), self.config_value('core.eol')), ('false', 'lf'))
                self.assertFalse((self.common_dir() / 'soso/unrestored.json').exists())

    def test_repaired_settings_are_compared_per_key(self):
        # A setting put back by hand, or by a restore, may sit later in its section than when it was recorded.
        git(self.root, 'config', 'core.autocrlf', 'false')
        git(self.root, 'config', 'core.eol', 'lf')
        runner = Run(self.root, self.config(), 'Improve')
        recorded = runner.shared(self.root)
        git(self.root, 'config', '--unset', 'core.autocrlf')
        git(self.root, 'config', 'core.autocrlf', 'false')
        self.assertTrue(runner.repaired(recorded))
        git(self.root, 'config', 'core.autocrlf', 'true')
        self.assertFalse(runner.repaired(recorded))

    # Code the checks load during the final evaluation turns value.txt from 1 into 9 and hides that from
    # git status; the evaluator itself runs isolated (-I). Scored 9, while the commit holds 1.
    HIDE_WITH_FILTER = ('git config filter.h.clean "tr 9 1" && '
                        'printf "value.txt filter=h\\n" > "$(git rev-parse --git-common-dir)/info/attributes"')
    HIDE_WITH_FLAG = 'git update-index --assume-unchanged value.txt'

    def hiding_lib(self, hide):
        return ("import subprocess\nfrom pathlib import Path\n"
                "if Path('value.txt').read_text() == '1':\n"
                f"    subprocess.run(['sh', '-c', {hide!r}], check=True)\n"
                "    Path('value.txt').write_text('9')\n")

    def checked_config(self):
        (self.root / 'check.py').write_text('try:\n    import lib\nexcept ImportError:\n    pass\n')
        git(self.root, 'add', 'check.py')
        git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'check')
        return {'checks': [[sys.executable, 'check.py']], 'evaluate': [sys.executable, '-I', 'evaluate.py']}

    def hiding_candidate(self, hide):
        report, _ = self.run_steps([{'tool': 'write', 'path': 'lib.py', 'content': self.hiding_lib(hide)},
                                    {'tool': 'write', 'path': 'value.txt', 'content': '1'}, {'tool': 'done', 'summary': 'done'}],
                                   **self.checked_config())
        self.assertEqual(report['best'], 0)
        return report['rounds'][0]['experiments'][0]

    def test_filter_set_during_the_evaluation_is_invalid_and_restored(self):
        experiment = self.hiding_candidate(self.HIDE_WITH_FILTER)
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'repository configuration changed during evaluation'})
        self.assertEqual(self.restored_keys(experiment), ['local:filter.h.clean', 'info/attributes'])
        self.assertEqual(self.config_value('filter.h.clean'), '')

    def test_index_flag_set_during_the_evaluation_is_invalid(self):
        experiment = self.hiding_candidate(self.HIDE_WITH_FLAG)
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'evaluation changed files Git does not show'})

    def hiding_baseline(self, hide):
        # The base already holds the hiding code, as a later optimization epoch's base holds accepted code.
        (self.root / 'lib.py').write_text(self.hiding_lib(hide))
        (self.root / 'value.txt').write_text('1')
        config = self.checked_config()
        git(self.root, 'add', 'lib.py', 'value.txt')
        git(self.root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'accepted earlier')
        self.runner = Run(self.root, self.config(parallel=1, rounds=1, **config), 'Improve')
        with contextlib.redirect_stdout(io.StringIO()):
            report = self.runner.run()
        self.assertEqual(report['status'], 'invalid_baseline')
        return report['baseline']

    def test_filter_set_during_the_baseline_evaluation_blocks_new_runs(self):
        baseline = self.hiding_baseline(self.HIDE_WITH_FILTER)
        self.assertEqual(baseline, {'valid': False, 'reason': 'baseline evaluation changed repository configuration'})
        with self.assertRaises(ValueError) as refused:
            Run(self.root, self.config(), 'Improve')
        # No restore was attempted: the message says what happened, with the usual repair instructions.
        self.assertRegex(str(refused.exception), r"^run [\w-]+'s baseline evaluation changed repository settings; "
                                                 r"put back the settings recorded in .*watched-config\.json, or delete ")

    def test_resume_measures_no_baseline_while_its_settings_are_unrestored(self):
        # soso run --resume would otherwise score a new baseline with the planted filter still in place.
        self.hiding_baseline(self.HIDE_WITH_FILTER)
        resumed = Run(self.root, self.runner.config, 'Improve', resume=self.runner.run_id)
        with patch('soso.engine.evaluate', wraps=evaluate) as measured, \
                self.assertRaisesRegex(ValueError, "baseline evaluation changed repository settings; put back"):
            resumed.run()
        measured.assert_not_called()
        self.assertTrue((self.common_dir() / 'soso/unrestored.json').exists())

    def test_index_flag_set_during_the_baseline_evaluation_is_invalid(self):
        baseline = self.hiding_baseline(self.HIDE_WITH_FLAG)
        self.assertEqual(baseline, {'valid': False, 'reason': 'baseline evaluation modified source'})

    def test_restore_is_logged_when_the_candidate_ends_with_an_error(self):
        report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.autocrlf true && echo "# x" >> evaluate.py']},
                                    {'tool': 'done', 'summary': 'done'}], allow_exec=True)
        experiment = report['rounds'][0]['experiments'][0]
        self.assertEqual(experiment['error'], 'protected files changed')
        self.assertEqual(self.restored_keys(experiment), ['local:core.autocrlf'])
        self.assertEqual([e['keys'] for e in self.events('repository_config_restored')], [['local:core.autocrlf']])
        self.assertEqual(self.config_value('core.autocrlf'), '')

    def test_resumed_run_keeps_the_recorded_baseline(self):
        # A worker change left behind by a hard kill must not become the baseline after resume.
        config = self.config(parallel=1, rounds=1)
        recorded = watched_config(self.root, '--local', os.path.realpath(self.common_dir() / 'config'))[:2]
        self.improving_run()
        run_id = sorted(p.name for p in self.common_dir().glob('soso/*-*') if p.is_dir())[-1]
        git(self.root, 'config', 'core.autocrlf', 'true')
        resumed = Run(self.root, {**config, 'allow_exec': True}, 'Improve', allow_exec=True, resume=run_id)
        self.assertEqual(resumed.watched[0], recorded)  # e.g. core.filemode, which git init writes
        self.assertNotIn(('core.autocrlf', 'true'), resumed.watched[0][1])
        self.assertIsNone(resumed.watched[1])

    def test_failed_restore_ends_the_candidate_with_an_error(self):
        # A restore that leaves a watched key missing must not pass silently.
        git(self.root, 'config', 'core.autocrlf', 'false')
        from soso import engine
        real = engine.git
        def dropping_add(root, *args, **kwargs):
            if args[:1] == ('config',) and '--add' in args:
                return ''
            return real(root, *args, **kwargs)
        steps = iter([{'tool': 'run', 'argv': ['sh', '-c', 'git config core.autocrlf true && echo 5 > value.txt']},
                      {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            return next(steps)
        runner = Run(self.root, self.config(parallel=1, rounds=1), 'Improve', allow_exec=True)
        with patch.object(Router, 'ask', answer), patch('soso.engine.git', dropping_add), \
                contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        experiment = report['rounds'][0]['experiments'][0]
        self.assertIn('could not restore', experiment.get('error', ''))
        self.assertEqual(self.restored_keys(experiment), ['local:core.autocrlf'])  # logged once

    def parallel_run(self, plans, observe=None):
        # Candidates 0 and 1 run together; candidate 0 starts first and finishes after candidate 1's change.
        import threading
        import time
        together, changed, restored = threading.Barrier(2, timeout=30), threading.Event(), threading.Event()
        steps = {}
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer':
                return {'approve': True}
            index = int(Path(cwd).name.split('-c')[1].split('-')[0])
            step = plans[index][steps.setdefault(index, 0)]
            steps[index] += 1
            if steps[index] == 1:
                together.wait()  # both candidates are running
            if step['tool'] == 'done' and index == 1:
                changed.set()  # its config change has run
                restored.wait(30)  # and is still in place when candidate 0 restores
            elif step['tool'] == 'done':
                changed.wait(30)
            return step
        restore = Run.restore_watched
        def observed(self, path, own, restored_list):
            result = restore(self, path, own, restored_list)
            if '-c0-' in str(path):
                restored.set()
            return result
        experiment = Run.experiment
        def staggered(self, task, round_number, index, continuation=None):
            if index == 1:
                time.sleep(0.5)  # candidate 0 starts first, so it alone could be blamed if overlap were ignored
            return experiment(self, task, round_number, index, continuation)
        runner = Run(self.root, self.config(parallel=2, rounds=1), 'Improve', allow_exec=True)
        with patch.object(Router, 'ask', answer), patch.object(Run, 'experiment', staggered), \
                patch.object(Run, 'restore_watched', observed), \
                patch('soso.engine.resources', return_value={'cpus': 2}), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        return report, {e['candidate']: e for e in report['rounds'][0]['experiments']}

    def test_sibling_config_change_does_not_invalidate_an_innocent_candidate(self):
        report, experiments = self.parallel_run({
            0: [{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}],
            1: [{'tool': 'run', 'argv': ['sh', '-c', 'git config core.safecrlf warn']}, {'tool': 'done', 'summary': 'done'}]})
        self.assertEqual(experiments[0]['evaluation'].get('score'), 5, experiments)
        self.assertEqual(self.restored_keys(experiments[0]), ['local:core.safecrlf'])
        self.assertEqual(experiments[1]['evaluation'], {'valid': False, 'reason': 'no changes'})
        self.assertEqual(report['best'], 5)
        self.assertEqual(self.config_value('core.safecrlf'), '')

    def test_own_worktree_config_change_is_invalid_even_beside_siblings(self):
        # A worktree's config.worktree is written only from that worktree, so the change is its worker's.
        git(self.root, 'config', 'extensions.worktreeConfig', 'true')
        report, experiments = self.parallel_run({
            0: [{'tool': 'run', 'argv': ['sh', '-c', 'git config --worktree core.autocrlf input']},
                {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'}, {'tool': 'done', 'summary': 'done'}],
            1: [{'tool': 'write', 'path': 'value.txt', 'content': '5'}, {'tool': 'done', 'summary': 'done'}]})
        self.assertEqual(experiments[0]['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertEqual(self.restored_keys(experiments[0]), ['worktree:core.autocrlf'])
        self.assertEqual(experiments[1]['evaluation'].get('score'), 5)

    def test_sibling_change_after_the_commit_is_restored_before_evaluation(self):
        # A sibling changes a watched setting right after this commit; the evaluator must not see it.
        restore = Run.restore_watched
        calls = []
        def sibling_changes_config(self, path, own, restored):
            changed = restore(self, path, own, restored)
            calls.append(changed)
            if len(calls) == 2:  # the re-check after the commit
                git(self.root, 'config', 'core.safecrlf', 'warn')
            return changed
        code = ("import json, subprocess\nfrom pathlib import Path\n"
                "seen = subprocess.run(['git', 'config', '--get', 'core.safecrlf'], capture_output=True, text=True).stdout\n"
                "print(json.dumps({'score': -1 if seen.strip() else int(Path('value.txt').read_text())}))\n")
        with patch.object(Run, 'restore_watched', sibling_changes_config):
            experiment = self.improving_run(allow_exec=False, evaluate=[sys.executable, '-c', code])
        self.assertEqual(experiment['evaluation'].get('score'), 5, experiment['evaluation'])
        self.assertEqual(self.restored_keys(experiment), ['local:core.safecrlf'])

    def test_commit_made_under_a_sibling_filter_is_invalid(self):
        # Simulate a sibling adding a clean filter while this candidate stages and commits.
        common = self.common_dir()
        restore = Run.restore_watched
        calls = []
        def sibling_changes_config(self, path, own, restored):
            changed = restore(self, path, own, restored)
            if not calls:
                git(self.root, 'config', 'filter.shadow.clean', 'sed s/1/999/')
                (common / 'info/attributes').write_text('value.txt filter=shadow\n')
            calls.append(changed)
            return changed
        steps = iter([{'tool': 'write', 'path': 'value.txt', 'content': '1'}, {'tool': 'done', 'summary': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            router.budget.claim()
            if role == 'reviewer': return {'approve': True}
            return next(steps)
        with patch.object(Run, 'restore_watched', sibling_changes_config):
            experiment = self.improving_run(answer, allow_exec=False)
        self.assertEqual(calls[0], [])  # this candidate changed nothing itself
        self.assertEqual(git(self.root, 'show', experiment['commit'] + ':value.txt'), '999')
        self.assertEqual(experiment['evaluation'], {'valid': False, 'reason': 'worker changed repository configuration'})
        self.assertFalse((common / 'info/attributes').exists())
        self.assertEqual(self.config_value('filter.shadow.clean'), '')

    def test_runner_git_ignores_a_preconfigured_fsmonitor_hook(self):
        # The hook is already configured, so no configuration change is detected; the runner's own
        # Git commands must still see the edit, which then lands in the scored commit.
        stub = Path(tempfile.mkdtemp()) / 'stub'
        self.addCleanup(shutil.rmtree, stub.parent)
        stub.write_text("#!/bin/sh\nprintf 'tok\\0'\n")
        stub.chmod(0o755)
        git(self.root, 'config', 'core.fsmonitor', str(stub))
        report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', 'git status --porcelain > /dev/null && '
                                                             'git update-index --fsmonitor-valid value.txt && echo 7 > value.txt']},
                                    {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                                    {'tool': 'done', 'summary': 'done'}], allow_exec=True)
        experiment = report['rounds'][0]['experiments'][0]
        self.assertEqual(experiment['evaluation'].get('score'), 7, experiment['evaluation'])
        self.assertEqual(git(self.root, 'show', experiment['commit'] + ':value.txt'), '7')

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
        report, _ = self.run_steps([{'tool': 'write', 'path': 'sub/bonus.txt', 'content': '999'},
                                    {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                                    {'tool': 'done', 'summary': 'done'}], evaluate=[sys.executable, '-c', code])
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
        report, _ = self.run_steps([{'tool': 'run', 'argv': ['sh', '-c', 'echo 999 > sub/bonus.txt']},
                                    {'tool': 'write', 'path': 'notes.txt', 'content': 'tracked change'},
                                    {'tool': 'done', 'summary': 'done'}], allow_exec=True, evaluate=[sys.executable, '-c', code])
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

    def test_protection_keeps_positive_character_classes_as_written(self):
        # Casefolding [A-Z] into [a-z] would newly protect value.txt.
        ws = Workspace(self.root, ['[A-Z]*.txt'])
        self.assertTrue(ws.tool({'tool': 'write', 'path': 'value.txt', 'content': '1'})['ok'])
        self.assertNotIn('value.txt', fingerprint(self.root, ['[A-Z]*.txt']))
        self.assertTrue(ws.is_protected('Value.txt'))
        self.assertTrue(Workspace(self.root, ['DATA/[A-Z]*']).is_protected('data/X.json'))
        # On a case-insensitive filesystem Poetry.lock is poetry.lock, which [!A-Z]*.lock protects.
        self.assertTrue(Workspace(self.root, ['[!A-Z]*.lock']).is_protected('Poetry.lock'))

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

    def test_python_m_is_recognised_for_more_interpreters_and_runners(self):
        (self.root / 'a').mkdir()
        (self.root / 'a/b.py').write_text('')
        for command in (['PYTHON3', '-m', 'a.b'], ['pypy3', '-m', 'a.b'], ['coverage', 'run', '-m', 'a.b'],
                        ['uv', 'run', '-m', 'a.b'], ['uv', 'run', '--with', 'python-dateutil', 'python', '-m', 'a.b'],
                        ['uv', 'run', '--python', 'python3.12', 'python', '-m', 'a.b'], ['python3.13t', '-m', 'a.b']):
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
