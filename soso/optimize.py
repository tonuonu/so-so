"""Bounded optimization epochs for an explicitly prepared, intent-bound assignment."""
import hashlib
import json
from pathlib import Path
import time

from .delivery import configure_models, load_model_pool
from .engine import BudgetExceeded, Run, atomic_json, load_config
from .models import ModelUnavailable, redact
from .process import git


def optimization_requested(project):
    path = Path(project) / 'soso.json'
    try:
        value = json.loads(path.read_text())
        return isinstance(value, dict) and value.get('mode') == 'optimize'
    except (OSError, ValueError):
        return False


def prepared_optimization(project: Path, intent: str):
    if not optimization_requested(project):
        return False
    config = json.loads((Path(project) / 'soso.json').read_text())
    return config.get('prepared_intent_sha256') == hashlib.sha256(intent.encode()).hexdigest()


def _wait(until):
    while time.time() < until:
        time.sleep(min(1, max(0, until - time.time())))


def optimize_project(project: Path, intent: str, folder: Path, models_path=None, save=None):
    """Resume each Run's original budget; only a separate epoch renews its budget."""
    project, folder = Path(project).resolve(), Path(folder).resolve()
    path, config_path = folder / 'optimization.json', folder / 'optimization-config.json'
    intent_sha = hashlib.sha256(intent.encode()).hexdigest()
    state = json.loads(path.read_text()) if path.exists() else {
        'mode': 'optimize', 'source': str(project), 'root': str(project), 'intent_sha256': intent_sha,
        'status': 'starting', 'run_id': None, 'epochs': [], 'next_retry_at': None, 'failures': 0, 'plateau': 0}
    if state['source'] != str(project) or state['intent_sha256'] != intent_sha:
        raise ValueError('optimization assignment changed')
    if 'plateau' not in state:
        # Migrate existing epoch journals without counting a resumed Run twice.
        total = 0
        for epoch in state['epochs']:
            epoch['plateau_before'] = total
            try:
                previous = json.loads(Path(epoch['report']).read_text())
            except (OSError, ValueError):
                continue
            total = previous.get('plateau', 0) + (0 if any(r.get('accepted') for r in previous.get('rounds', [])) else total)
            epoch['plateau'] = total
        state['plateau'] = total
    if state.get('config_sha256') and (not config_path.is_file() or hashlib.sha256(config_path.read_bytes()).hexdigest() != state['config_sha256']):
        raise ValueError('frozen optimization configuration changed')

    def checkpoint():
        atomic_json(path, json.loads(redact(json.dumps(state))))
        if save:
            save(state)

    checkpoint()
    try:
        while True:
            if state['status'] == 'optimized':
                if git(Path(state['worktree']), 'rev-parse', 'HEAD') != state['head'] or git(Path(state['worktree']), 'status', '--porcelain'):
                    raise ValueError('optimized result changed; inspect before continuing')
                return state
            if state.get('next_retry_at') and state['next_retry_at'] > time.time():
                _wait(state['next_retry_at'])
            try:
                if config_path.exists():
                    config_bytes = config_path.read_bytes()
                    digest = hashlib.sha256(config_bytes).hexdigest()
                    if state.get('config_sha256') and state['config_sha256'] != digest:
                        raise ValueError('frozen optimization configuration changed')
                    config = json.loads(config_bytes)
                else:
                    if not prepared_optimization(project, intent):
                        raise ValueError('optimization requires an intent-bound prepared assignment')
                    config = load_config(project / 'soso.json')
                    if models_path:
                        config.update(load_model_pool(models_path))
                    config = configure_models(config)
                    config['routes'] = {**config['routes'], **{role: config['routes'].get(role, config['routes']['worker'])
                                        for role in ('planner', 'reviewer')}}
                    config.update(delivery=False, _router_state=str(folder / 'optimization-router-state.json'))
                    # Freeze every existing local evaluator/check program argument.
                    programs = [arg for command in [config['evaluate']] + config.get('checks', []) for arg in command
                                if not Path(arg).is_absolute() and (project / arg).is_file()]
                    config['protected'] = list(dict.fromkeys(config['protected'] + programs))
                    atomic_json(config_path, config)
                    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
                state['config_sha256'] = digest
                # If killed before the first Run checkpoint, no worker could have
                # accepted a commit. Retain its folder, then start a fresh epoch.
                if state.get('run_id') and not Path(state['report']).exists():
                    state['epochs'][-1]['status'] = 'setup_interrupted'
                    state['run_id'] = None
                runner = Run(Path(state['root']), config, intent, allow_exec=True, resume=state.get('run_id'))
                if not state.get('run_id'):
                    state['epochs'].append({'run_id': runner.run_id, 'root': state['root'],
                                            'report': str(runner.report_path), 'status': 'running',
                                            'plateau_before': state['plateau']})
                state.update(run_id=runner.run_id, report=str(runner.report_path), status='running', next_retry_at=None, reason=None)
                checkpoint()  # Save the ID before evaluation or any model calls.
                try:
                    report = runner.run()
                except BudgetExceeded:
                    state['epochs'][-1]['status'] = 'budget_exhausted'
                    state.update(status='incomplete', run_id=None, next_retry_at=time.time() + 2)
                    checkpoint()
                    continue
                epoch = state['epochs'][-1]
                accepted = any(round_result.get('accepted') for round_result in report.get('rounds', []))
                state['plateau'] = report.get('plateau', 0) + (0 if accepted else epoch['plateau_before'])
                epoch.update(status=report['status'], head=report.get('head'), best=report.get('best'), plateau=state['plateau'])
                state.update(head=report.get('head'), branch=report.get('branch'), best=report.get('best'),
                             worktree=str(runner.integration))
                plateau = report['status'] == 'plateau' or (report['status'] in ('round_limit', 'budget_exhausted', 'waiting', 'interrupted')
                            and state['plateau'] >= config['patience'])
                if plateau:
                    state.update(status='optimized', next_retry_at=None)
                    checkpoint()
                    return state
                if report['status'] == 'waiting':
                    raise ModelUnavailable(report.get('next_retry_at'), report.get('reason', 'provider temporarily unavailable'))
                if report['status'] == 'interrupted':
                    state['status'] = 'interrupted'
                    checkpoint()
                    return state
                if report['status'] in ('budget_exhausted', 'round_limit'):
                    # The accepted result becomes a clean source for a new epoch;
                    # original inputs and the previous Run budget stay unchanged.
                    state.update(status='incomplete', root=str(runner.integration), run_id=None,
                                 next_retry_at=time.time() + 2, failures=0)
                    checkpoint()
                    continue
                state.update(status='blocked', reason=report.get('reason', report['status']))
                checkpoint()
                return state
            except ModelUnavailable as exc:
                state['failures'] += 1
                state.update(status='waiting', reason=str(exc), next_retry_at=max(time.time() + 1, exc.retry_at) if exc.retry_at is not None else
                             time.time() + min(900, 30 * 2 ** min(state['failures'] - 1, 5)))
                print('Waiting: ' + str(exc) + '. Retrying automatically at ' + time.strftime('%H:%M:%S', time.localtime(state['next_retry_at'])) + '. Ctrl+C pauses the run.', flush=True)
                checkpoint()
    except KeyboardInterrupt:
        state['status'] = 'interrupted'
        checkpoint()
        return state
