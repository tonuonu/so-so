"""One-command entry: preserve user inputs, resolve Markdown models, resume work."""
import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid

from .delivery import _identity, start_goal
from .engine import atomic_json
from .history import folder_name, title, update_history
from .models import ModelUnavailable, redact
from .optimize import optimize_project, optimization_requested, prepared_optimization
from .process import execute, git


EXCLUDED_DIRS = {'.git', '.so-so', '.ssh', '.aws', '.gnupg', 'keys', 'secrets',
                 'node_modules', '.venv', '__pycache__'}
EXCLUDED_FILES = {'.env', '.envrc', '.git-credentials', '.netrc', '.npmrc', '.pypirc', 'credentials', 'credentials.json',
                  'service-account.json', 'service_account.json', 'id_rsa', 'id_ed25519'}


def resolve_pool(text, cwd):
    # Kept lazy so bootstrap can locate a runtime before importing model setup.
    from .intent import resolve_pool as resolve
    return resolve(text, cwd)


def _excluded(name):
    path = Path(name)
    return (any(part.lower() in EXCLUDED_DIRS for part in path.parts)
            or path.name.lower() in EXCLUDED_FILES
            or any(fnmatch.fnmatch(path.name.lower(), pattern) for pattern in
                   ('.env.*', '*.pem', '*.key', '*.p12', '*.pfx')))


def _managed(cwd):
    folder = cwd / '.so-so'
    marker = folder / 'owner.json'
    if folder.is_symlink():
        raise ValueError('refusing a symlink at .so-so')
    if folder.is_symlink():
        raise ValueError('managed project must not be a symlink')
    if folder.exists():
        if not marker.is_file() or json.loads(marker.read_text()).get('owner') != 'so-so':
            raise ValueError('existing .so-so directory is not owned by this launcher')
    else:
        folder.mkdir(mode=0o700)
        atomic_json(marker, {'owner': 'so-so', 'version': 1})
        (folder / '.gitignore').write_text('*\n')
    for name in ('projects', 'pools'):
        if (folder / name).is_symlink():
            raise ValueError('managed state directories must not be symlinks')
        (folder / name).mkdir(exist_ok=True)
    return folder


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _inputs(cwd):
    probe = execute(['git', 'rev-parse', '--is-inside-work-tree'], cwd, timeout=10)
    if probe['returncode'] == 0 and probe['output'].strip() == 'true':
        names = git(cwd, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
    else:
        names = []
        for directory, folders, files in os.walk(cwd, followlinks=False):
            folders[:] = [name for name in folders if not _excluded(str((Path(directory) / name).relative_to(cwd)))
                          and not (Path(directory) / name).is_symlink()]
            names.extend(str((Path(directory) / name).relative_to(cwd)) for name in files)
    manifest, excluded = [], []
    for name in sorted(set(names)):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('source inventory contains an unsafe path')
        source = cwd / relative
        if _excluded(name) or source.is_symlink() or not source.resolve().is_relative_to(cwd):
            if not any(part in ('.git', '.so-so') for part in relative.parts):
                excluded.append(name)
            continue
        if not source.exists():
            continue  # A deleted tracked file stays deleted in the snapshot.
        if not source.is_file():
            continue
        info = source.stat()
        manifest.append({'path': name, 'sha256': _digest(source), 'mode': stat.S_IMODE(info.st_mode) & 0o777})
    return manifest, excluded


def _snapshot(cwd, managed, intent):
    manifest, excluded = _inputs(cwd)
    digest = hashlib.sha256(json.dumps({'intent': intent, 'files': manifest}, sort_keys=True).encode()).hexdigest()
    legacy = managed / 'projects' / digest
    folder = legacy if legacy.exists() else managed / 'projects' / folder_name(intent, digest)
    project = folder / 'project'
    if folder.exists():
        if not (folder / 'manifest.json').is_file() or not (project / '.git').is_dir():
            raise ValueError('managed project snapshot is incomplete; inspect before continuing')
        if json.loads((folder / 'manifest.json').read_text()).get('fingerprint') != digest:
            raise ValueError('snapshot name collision; existing snapshot preserved')
        return digest, folder, project, excluded
    temporary = managed / 'projects' / ('.preparing-' + uuid.uuid4().hex)
    temporary.mkdir()
    target = temporary / 'project'
    target.mkdir()
    try:
        for item in manifest:
            source, destination = cwd / item['path'], target / item['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Recheck links: the user may edit inputs while the snapshot is copied.
            if source.is_symlink() or not source.resolve().is_relative_to(cwd):
                raise ValueError('source changed while preparing snapshot; rerun the command')
            shutil.copyfile(source, destination)
            destination.chmod(item['mode'])
            if _digest(destination) != item['sha256']:
                raise ValueError('source changed while preparing snapshot; rerun the command')
        git(target, 'init', '-b', 'main')
        _identity(target)
        git(target, 'add', '--force', '--all')
        git(target, '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'Snapshot project inputs')
        atomic_json(temporary / 'manifest.json', {'fingerprint': digest, 'files': manifest,
                                                'intent_sha256': hashlib.sha256(intent.encode()).hexdigest()})
        (temporary / 'INTENT.md').write_text(intent)
        temporary.rename(folder)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return digest, folder, project, excluded


def _goal_id(project):
    # Covers a crash after goal creation but before the launcher receives its ID.
    goals = project / '.git' / 'soso' / 'goals'
    candidates = []
    if goals.exists():
        for file in goals.glob('*/state.json'):
            try:
                state = json.loads(file.read_text())
                if state.get('source') == str(project.resolve()) and state.get('goal_id'):
                    candidates.append((file.stat().st_mtime_ns, state['goal_id']))
            except (OSError, ValueError):
                continue
    return max(candidates)[1] if candidates else None


def _save(managed, folder, entry):
    entry['updated_at'] = time.time()
    atomic_json(folder / 'entry.json', entry)
    atomic_json(managed / 'active.json', entry)
    update_history(managed, folder, entry)


def _wait(until):
    while time.time() < until:
        time.sleep(min(1, max(0, until - time.time())))


def launch(cwd: Path, goal: str | None = None, intent_file: str | None = None):
    """Read an assignment and resume its owned snapshot without source Git edits."""
    cwd = Path(cwd).resolve()
    intent_path = cwd / 'INTENT.md'
    if intent_file is not None:
        if goal is not None:
            raise ValueError('use an assignment or --intent, not both')
        assignment = cwd / intent_file
        if assignment.is_symlink() or not assignment.is_file():
            raise ValueError('--intent must name a regular Markdown file')
        if assignment.stat().st_size > 8000:
            raise ValueError('intent file must contain 1..8000 bytes')
        goal = assignment.read_text()
    use_file = intent_file is None and (goal is None or goal.strip().lower() == 'read intent.md')
    if intent_path.is_symlink() and use_file:
        raise ValueError('INTENT.md must be a regular file in this project')
    source_intent = intent_path.read_text() if intent_path.is_file() and not intent_path.is_symlink() else None
    if use_file:
        if source_intent is None:
            raise ValueError('write INTENT.md or provide an assignment after ./so-so')
        goal = source_intent
    if not goal.strip() or len(goal.encode()) > 8000 or (source_intent is not None and len(source_intent.encode()) > 8000):
        raise ValueError('INTENT.md or goal must contain 1..8000 bytes')
    settings = goal if use_file or source_intent is None else source_intent + '\n\nCurrent assignment (overrides the task above):\n' + goal
    managed = _managed(cwd)
    with (managed / 'launch.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another so-so process owns this project') from None
        source = (cwd / intent_file).resolve().parent if intent_file is not None else cwd
        fingerprint, folder, project, excluded = _snapshot(source, managed, goal)
        path = folder / 'entry.json'
        entry = json.loads(path.read_text()) if path.exists() else {
            'fingerprint': fingerprint, 'project': str(project), 'source': str(source),
            'goal_id': None, 'status': 'starting', 'next_retry_at': None, 'phase': 'model_setup'}
        entry['title'] = title(goal)
        if not (folder / 'INTENT.md').exists():
            (folder / 'INTENT.md').write_text(goal)
        entry['mode'] = 'optimize' if prepared_optimization(project, goal) else 'delivery'
        if entry['mode'] == 'delivery':
            entry['goal_id'] = entry.get('goal_id') or _goal_id(project)
        _save(managed, folder, entry)
        failures = entry.get('setup_failures', 0)
        try:
            if not entry['goal_id'] and not (entry['mode'] == 'optimize' and (folder / 'optimization.json').exists()):
                while True:
                    if entry.get('next_retry_at') and entry['next_retry_at'] > time.time():
                        _wait(entry['next_retry_at'])
                    try:
                        pool = resolve_pool(settings, cwd)
                        pool_path = managed / 'pools' / (fingerprint + '.json')
                        if pool is not None:
                            atomic_json(pool_path, pool)
                            entry['models_path'] = str(pool_path)
                        else:
                            entry['models_path'] = None
                        entry.update(status='running', phase='delivery', next_retry_at=None, setup_failures=0)
                        _save(managed, folder, entry)
                        break
                    except ModelUnavailable as exc:
                        failures += 1
                        entry.update(status='waiting', phase='model_setup', setup_failures=failures, next_retry_at=
                                     max(time.time() + 1, exc.retry_at) if exc.retry_at is not None else
                                     time.time() + min(900, 30 * 2 ** min(failures - 1, 5)))
                        _save(managed, folder, entry)
            if entry['mode'] == 'optimize':
                def save_optimization(state):
                    entry.update({key: state.get(key) for key in ('run_id', 'status', 'next_retry_at', 'report', 'worktree', 'reason')})
                    entry['phase'] = 'optimize'
                    _save(managed, folder, entry)
                result = optimize_project(project, goal, folder, models_path=entry.get('models_path'), save=save_optimization)
            else:
                options = {'ignore_project_config': True} if optimization_requested(project) else {}
                result = start_goal(project, goal, models_path=entry.get('models_path'), resume_goal=entry['goal_id'], **options)
            entry.update(goal_id=result.get('goal_id'), status=result['status'], phase=entry['mode'],
                         next_retry_at=result.get('next_retry_at'), report=result.get('report'), worktree=result.get('worktree'))
            _save(managed, folder, entry)
            return {**result, 'source': str(source), 'snapshot': str(project), 'excluded': excluded[:100]}
        except KeyboardInterrupt:
            entry.update(status='interrupted', goal_id=entry.get('goal_id') or _goal_id(project))
            _save(managed, folder, entry)
            return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read INTENT.md and carry out its goal; source files are preserved.')
    parser.add_argument('goal', nargs='?', help='optional assignment instead of INTENT.md')
    parser.add_argument('--intent', metavar='PATH', help='read the assignment from a Markdown file')
    args = parser.parse_args(argv)
    if args.intent is not None and args.goal is not None:
        parser.error('use an assignment or --intent, not both')
    try:
        from .dashboard import automatic
        with automatic(Path.cwd()):
            result = launch(Path.cwd(), args.goal, intent_file=args.intent)
        if result['status'] == 'blocked':
            print('Stopped: ' + (result.get('reason') or 'the runner could not continue') + '. This does not mean the goal is unachievable.')
            print('Inspect the dashboard or evidence below before retrying. Completed results are preserved.')
        else:
            print('Optimization reached a plateau.' if result['status'] == 'optimized' else 'Goal ' + result['status'] + '.')
        from .reporting import summary_lines
        for line in summary_lines(result):
            print(line)
        print('Intent history: ' + str(Path.cwd() / '.so-so' / 'README.md'))
        if result.get('worktree'):
            print('Result: ' + result['worktree'])
        if result.get('report'):
            print('Evidence: ' + result['report'])
        return 0 if result['status'] in ('complete', 'optimized') else 1 if result['status'] == 'blocked' else 2
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print('so-so: ' + redact(str(exc)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
