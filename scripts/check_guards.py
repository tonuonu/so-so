"""Prove each listed guard is tested: disabling it must turn its tests red.

Copies the working tree into a temporary, self-contained Git repository, disables
one guard at a time, and runs only that guard's tests. A new guard adds an entry.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# (file, guard text, disabled replacement, tests that must fail without it)
GUARDS = [
    ('soso/workspace.py', 'any(p.casefold() == ".git" for p in path.parts)', '".git" in path.parts',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_git_dir_is_rejected_in_any_case']),
    ('soso/workspace.py', 'name = PurePosixPath(name).as_posix()', 'name = name',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_matches_normalised_names']),
    ('soso/workspace.py', 'raise PermissionError("writes through symlinks are forbidden")', 'pass',
     ['test_core.WorkspaceTests.test_internal_symlink_is_not_writable']),
    ('soso/workspace.py', 'fnmatch.fnmatchcase(name.casefold(), pattern.casefold())', 'fnmatch.fnmatchcase(name, pattern)',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_ignores_case']),
    ('soso/workspace.py', 'path.name.casefold() == "agents.md"', 'path.name == "AGENTS.md"',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_agents_md_name_check_ignores_case']),
    ('soso/engine.py', 'Path(name).name.casefold() == "agents.md"', 'Path(name).name == "AGENTS.md"',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_agents_md_name_check_ignores_case']),
    ('soso/engine.py', 'if any(line.split()[1] == "160000"', 'if False and any(line.split()[1] == "160000"',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_adding_embedded_repository_is_invalid']),
    ('soso/engine.py', 'git(path, "clean", "-ffdqX")', 'pass',
     ['test_delivery.DeliveryTests.test_candidate_ignored_shadow_files_do_not_raise_accepted_score']),
    ('soso/delivery.py', "git(root, 'clean', '-ffdqX')", 'None',
     ['test_delivery.DeliveryTests.test_baseline_ignores_files_left_by_earlier_evaluator_runs']),
    ('soso/process.py', 'os.killpg(proc.pid, signal.SIGKILL)', 'proc.kill()',
     ['test_core.ProcessTests.test_descendant_cleanup']),
    ('soso/engine.py', 'git(path, "clean", "-ffdqX")', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_is_evaluated_without_ignored_files']),
    ('soso/engine.py', ' and "review" not in x]', ']',
     ['test_run_reliability.IncompleteRoundTests.test_rejected_candidate_is_not_reviewed_again_on_resume']),
    ('soso/engine.py', 'raise ValueError("worker changed Git history")', 'pass',
     ['test_guards.GuardTests.test_worker_cannot_commit_candidate_history']),
    ('soso/engine.py', 'raise ValueError("protected files cannot be symlinks")', 'pass',
     ['test_guards.GuardTests.test_protected_file_replaced_by_identical_symlink_is_rejected']),
    ('soso/engine.py', 'raise ValueError("result HEAD changed since checkpoint; inspect before resuming")', 'pass',
     ['test_guards.GuardTests.test_resume_rejects_result_head_moved_since_checkpoint']),
    ('soso/cli.py', 'config["protected"] += [name for name in dict.fromkeys(programs) if name not in config["protected"]]',
     'pass', ['test_sandbox_integrity.SandboxIntegrityTests.test_run_protects_in_repo_evaluator_and_check_programs']),
    ('soso/cli.py', 'if name not in config["protected"]]', ']',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_already_protected_evaluator_leaves_config_unchanged']),
    ('soso/delivery.py', "raise ValueError('goal acceptance policy changed')", 'pass',
     ['test_delivery.DeliveryTests.test_policy_tampering_with_recorded_head_is_blocked']),
    # The run_epoch check; repair_candidate's copy is indented less.
    ('soso/delivery.py', "                        raise ValueError('review modified accepted source')",
     '                        pass', ['test_delivery.DeliveryTests.test_approving_review_that_leaves_files_is_not_integrated']),
    ('soso/delivery.py', "raise ValueError('prepared acceptance evaluator changed')", 'pass',
     ['test_delivery.DeliveryTests.test_prepared_contract_evaluator_is_verified_before_recovery']),
    ('soso/dashboard.py', "raise ValueError('unknown asset')", 'pass',
     ['test_dashboard.DashboardTests.test_http_requires_token_and_serves_no_arbitrary_files']),
]


# An inherited GIT_DIR or GIT_WORK_TREE would point the snapshot's Git commands at a real repository.
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')}, 'PYTHONDONTWRITEBYTECODE': '1'}


def tests_fail(root, tests):
    env = {**ENV, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root)])}
    result = subprocess.run([sys.executable, '-m', 'unittest', *tests], cwd=root, env=env,
                            capture_output=True, text=True, timeout=900)
    return result.returncode != 0, result.stderr[-3000:]


def snapshot(source, root):
    shutil.copytree(source, root, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '.claude', '.so-so', '__pycache__', '.venv'))
    for argv in (['init', '-q', '-b', 'main'], ['add', '-A'],
                 ['-c', 'user.name=check', '-c', 'user.email=check@example.invalid', '-c', 'commit.gpgsign=false',
                  '-c', 'core.hooksPath=/dev/null', 'commit', '-q', '-m', 'snapshot']):
        subprocess.run(['git', *argv], cwd=root, env=ENV, check=True, capture_output=True)


def main():
    problems = []
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / 'repo'
        snapshot(Path(__file__).resolve().parent.parent, root)
        failed, log = tests_fail(root, sorted({t for *_, tests in GUARDS for t in tests}))
        if failed:
            print('check_guards: guard tests fail with every guard in place\n' + log, file=sys.stderr)
            return 1
        for file, guard, disabled, tests in GUARDS:
            path = root / file
            original = path.read_text()
            if original.count(guard) != 1:
                problems.append(f'{file}: guard text must occur exactly once: {guard}')
                continue
            mutant = original.replace(guard, disabled)
            try:
                compile(mutant, file, 'exec')  # a syntax error would fail the tests for the wrong reason
            except SyntaxError as exc:
                problems.append(f'{file}: disabled guard does not compile ({exc.msg}): {guard.strip()}')
                continue
            path.write_text(mutant)
            try:
                failed, _ = tests_fail(root, tests)
            except subprocess.TimeoutExpired:
                problems.append(f'{file}: tests timed out with this guard disabled: {guard.strip()}')
                continue
            finally:
                path.write_text(original)
            print(('red   ' if failed else 'GREEN ') + file + ': ' + guard.strip(), flush=True)
            if not failed:
                problems.append(f'{file}: tests still pass with this guard disabled: {guard.strip()}')
    for problem in problems:
        print('check_guards: ' + problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
