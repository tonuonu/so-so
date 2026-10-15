#!/usr/bin/env python3
"""One host action: obtain the runner, connect a model, and start working.

This file can run from its repository or as a standalone download. It never
asks the language model to access GitHub or install its own tools.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def command(argv, cwd=None, timeout=180):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{Path(argv[0]).name} timed out after {timeout} seconds') from None
    if result.returncode:
        # Git output can contain authenticated remote URLs. Keep error reporting
        # factual and avoid echoing a token-bearing URL or command arguments.
        raise RuntimeError(f'{Path(argv[0]).name} failed (exit {result.returncode}); check repository access and prerequisites')
    return result.stdout.strip()


def runtime(workspace, source=None):
    here = Path(__file__).resolve().parent
    if source is None and (here / 'soso' / '__init__.py').is_file():
        return here
    target = workspace / 'runtime'
    if target.exists():
        if not (target / 'soso' / '__init__.py').is_file():
            raise ValueError('existing runtime directory is not a so-so checkout')
        return target
    url = source or os.environ.get('SOSO_SOURCE_URL', 'git@github.com:tonuonu/so-so.git')
    if source is None and 'SOSO_SOURCE_URL' not in os.environ and shutil.which('gh'):
        command(['gh', 'repo', 'clone', 'tonuonu/so-so', str(target), '--', '--depth', '1'])
    else:
        command(['git', 'clone', '--depth', '1', '--', url, str(target)])
    return target


def install_launcher(checkout, workspace):
    target = workspace / 'soso'
    desired = (checkout / 'bin' / 'soso').resolve()
    if target.is_symlink() and target.resolve() == desired:
        return target
    if target.exists() or target.is_symlink():
        raise ValueError('refusing to replace existing launcher path: ' + str(target))
    target.symlink_to(desired)
    return target


def self_project(checkout, workspace):
    target = workspace / ('self-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
    command(['git', 'clone', '--no-local', '--', str(checkout), str(target)])
    for key, fallback in [('user.name', 'Project Maintainer'), ('user.email', 'maintainer@localhost')]:
        result = subprocess.run(['git', 'config', key], cwd=target, capture_output=True, text=True)
        if result.returncode:
            command(['git', 'config', key, fallback], target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('intent', nargs='?', help='goal text; omit for the runner self-improvement assignment')
    parser.add_argument('--repo', type=Path, help='project directory; omitted goal text reads its INTENT.md')
    parser.add_argument('--workspace', type=Path, default=Path.cwd() / 'soso-work', help='runtime/launcher/results directory')
    parser.add_argument('--backend', default='auto', choices=['auto', 'ollama', 'codex', 'claude'])
    parser.add_argument('--model', default=os.environ.get('SOSO_MODEL'))
    parser.add_argument('--models', type=Path, default=os.environ.get('SOSO_MODELS_FILE'), help='JSON model pool and routing policy')
    parser.add_argument('--once', action='store_true', help='run one delivery epoch, without waiting/retrying')
    parser.add_argument('--no-exec', action='store_true', help='disable arbitrary worker commands')
    parser.add_argument('--install-only', action='store_true', help='create launcher without invoking a model')
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise RuntimeError('Python 3.11 or newer is required')
        if not shutil.which('git'):
            raise RuntimeError('Git must be installed on the host')
        workspace = args.workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        checkout = runtime(workspace)
        launcher = install_launcher(checkout, workspace)
        print('Launcher:', launcher, flush=True)
        if args.install_only:
            return 0
        sys.path.insert(0, str(checkout))
        from soso.delivery import start_goal, configure_models, load_model_pool
        if args.repo is not None or args.intent is not None:
            repo = (args.repo or workspace / 'project').resolve()
            repo.mkdir(parents=True, exist_ok=True)
            result = start_goal(repo, args.intent, backend=args.backend, model=args.model,
                                once=args.once, allow_exec=not args.no_exec, models_path=args.models)
        else:
            # First assignment: measured improvement of a clone of the runner.
            from soso.engine import Run, load_config
            repo = self_project(checkout, workspace)
            config = load_config(repo / 'soso.json')
            config.pop('models', None)
            config.pop('routes', None)
            if args.models:
                config.update(load_model_pool(args.models))
            config = configure_models(config, backend=args.backend, model=args.model)
            # The core tests need only Python and Git. Optional example languages
            # are checked when those host tools are available.
            if not (shutil.which('cc') and shutil.which('node')):
                config['checks'] = [c for c in config.get('checks', []) if 'scripts/check_examples.py' not in c]
            (repo / 'soso.json').write_text(json.dumps(config, indent=2) + '\n')
            command(['git', 'add', 'soso.json'], repo)
            command(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                     'commit', '--allow-empty', '-m', 'Configure first self-improvement assignment'], repo)
            runner = Run(repo, config, (repo / 'INTENT.md').read_text(), allow_exec=not args.no_exec)
            report = runner.run()
            result = {'status': report['status'], 'project': str(repo), 'branch': report.get('branch'),
                      'report': str(runner.report_path), 'baseline': report['baseline'].get('score'),
                      'best': report.get('best')}
        print(json.dumps(result, indent=2))
        if result['status'] in ('waiting', 'incomplete', 'interrupted'):
            return 2
        return 1 if result['status'] in ('error', 'invalid_baseline', 'failed', 'blocked') else 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print('soso bootstrap:', str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
