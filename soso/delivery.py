"""Persistent goal delivery. Each epoch has a fresh, explicitly bounded budget.

Generated acceptance contracts are operational oracles, not human correctness proofs.
Local model code and requested host commands execute with the user's permissions.
"""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import uuid
from urllib.parse import urlsplit

from .engine import DEFAULTS, Run, atomic_json, evaluate, fingerprint, load_config, resources
from .models import Budget, BudgetExceeded, HTTP_TRANSPORT, ModelUnavailable, Router, redact
from .process import execute, git
from .workspace import Workspace


POLICY = '.soso-delivery'


def ollama_models(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Ollama URL must be an HTTP endpoint without credentials or query')
    names = []
    for endpoint in ('/api/ps', '/api/tags'):
        result = execute([sys.executable, '-I', '-c', HTTP_TRANSPORT], Path.cwd(), 3,
                         stdin=json.dumps({'url': url.rstrip('/') + endpoint, 'timeout': 2}), limit=1_000_000)
        try:
            envelope = json.loads(result['output'])
            if result['timed_out'] or envelope['status'] != 200:
                continue
            for entry in envelope['data'].get('models', []):
                name = entry.get('name') or entry.get('model')
                if isinstance(name, str) and name not in names:
                    names.append(name)
        except (ValueError, KeyError, TypeError):
            continue
    return names


def load_model_pool(path):
    """Read evaluator-independent model settings and derive ordered role routes."""
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get('models'), dict) or not value['models']:
        raise ValueError('model pool requires a nonempty models object')
    models = value['models']
    roles_by_model = {}
    for name, model in models.items():
        if not isinstance(name, str) or not name or not isinstance(model, dict) or model.get('kind') not in ('ollama', 'openai', 'codex', 'command', 'kimi'):
            raise ValueError('model pool entries require a name and supported kind')
        for key in ('priority', 'cost'):
            number = model.get(key, 0)
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError('model ' + key + ' must be finite numeric')
        roles = model.get('roles', ['worker', 'planner', 'reviewer'])
        if roles == 'all':
            roles = ['worker', 'planner', 'reviewer']
        if not isinstance(roles, list) or any(not isinstance(role, str) or not role for role in roles):
            raise ValueError('model roles must be a list of role names')
        roles_by_model[name] = roles
    if 'routes' in value:
        routes = value['routes']
        if not isinstance(routes, dict):
            raise ValueError('model pool routes must be an object')
    else:
        ordered = sorted(models, key=lambda name: (models[name].get('priority', 0), models[name].get('cost', 0)))
        roles = list(dict.fromkeys(role for names in roles_by_model.values() for role in names))
        routes = {role: [name for name in ordered if role in roles_by_model[name]] for role in roles}
    if not routes.get('worker'):
        raise ValueError('model pool requires a nonempty worker route')
    for role, names in routes.items():
        if not isinstance(role, str) or not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in models for name in names):
            raise ValueError('model pool routes must list configured model names')
    result = {'models': models, 'routes': routes}
    if 'routing' in value:
        if not isinstance(value['routing'], dict):
            raise ValueError('model pool routing must be an object')
        result['routing'] = value['routing']
    return result


def configure_models(config, backend='auto', model=None):
    """Return a config copy; explicit backend/model replace configured routes."""
    config = dict(config)
    env_local = os.environ.get('SOSO_LOCAL_ONLY', '').lower() in ('1', 'true', 'yes')
    if backend == 'auto' and model is None and config.get('models'):
        if not env_local:
            return config
        local = {name: value for name, value in config['models'].items() if value.get('kind') == 'ollama'}
        if local:
            config['models'] = local
            config.pop('routing', None)  # local-only diagnostics must not retain external destinations
            config['routes'] = {role: [n for n in route if n in local] or list(local)
                                for role, route in config['routes'].items()}
            return config
    if backend not in ('auto', 'ollama', 'codex', 'claude'):
        raise ValueError('backend must be auto, ollama, codex or claude')
    url = os.environ.get('SOSO_OLLAMA_URL', 'http://127.0.0.1:11434')
    choices = {}
    local_only = backend == 'ollama' or env_local or (backend == 'auto' and model is not None)
    if backend in ('auto', 'ollama'):
        names = ollama_models(url)
        if model is not None:
            if model in names:
                choices['local'] = {'kind': 'ollama', 'url': url, 'model': model}
            elif backend == 'ollama' or backend == 'auto':
                # An explicit model must never silently select another provider/model.
                raise ModelUnavailable(reason='requested local model is not installed; no download attempted')
        elif names:
            for index, name in enumerate(names):
                alias = 'local' if index == 0 else f'local-{index + 1}'
                choices[alias] = {'kind': 'ollama', 'url': url, 'model': name}
        elif backend == 'ollama':
            raise ModelUnavailable(reason='no installed Ollama model available')
    if not local_only and backend in ('auto', 'codex') and shutil.which('codex'):
        choices['codex'] = {'kind': 'codex', **({'model': model} if backend == 'codex' and model else {})}
    if not local_only and backend in ('auto', 'claude') and shutil.which('claude'):
        choices['claude'] = {'kind': 'command', 'command': ['claude', '-p', '--output-format', 'text'] +
                            (['--model', model] if backend == 'claude' and model else [])}
    if not choices:
        raise ModelUnavailable(reason='no available local model or installed command adapter')
    config['models'] = choices
    config.pop('routing', None)  # explicit provider/model overrides replace pool destinations
    config['routes'] = {'worker': list(choices), 'planner': list(choices), 'reviewer': list(choices)}
    if backend == 'auto' and len(choices) > 1:
        strong = [n for n in choices if choices[n]['kind'] != 'ollama'] + [n for n in choices if choices[n]['kind'] == 'ollama']
        config['routes'].update(planner=strong, reviewer=strong)
    return config


def _identity(repo):
    for key, value in (('user.name', 'Local Builder'), ('user.email', 'builder@localhost')):
        result = execute(['git', 'config', '--get', key], repo)
        if result['returncode'] or not result['output'].strip():
            git(repo, 'config', '--local', key, value)


def _storage(repo):
    repo.mkdir(parents=True, exist_ok=True)
    probe = execute(['git', 'rev-parse', '--show-toplevel'], repo)
    if probe['returncode']:
        initial_files = list(repo.iterdir())
        safe = {'INTENT.md', 'AGENTS.md', '.gitignore'}
        if any(p.name not in safe or not p.is_file() or p.is_symlink() for p in initial_files):
            raise ValueError('existing non-Git project: initialize Git and commit the intended source files before starting')
        git(repo, 'init', '-b', 'main')
        _identity(repo)
        if initial_files:
            git(repo, 'add', '--', *sorted(p.name for p in initial_files))
        git(repo, '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'Initial project')
    elif Path(probe['output'].strip()).resolve() != repo:
        raise ValueError('--repo must name the Git repository root')
    _identity(repo)
    common = Path(git(repo, 'rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = (repo / common).resolve()
    storage = common / 'soso'
    storage.mkdir(exist_ok=True)
    return storage


def _config(repo, config_path):
    path = repo / config_path
    if path.exists():
        return load_config(path)
    # Preserve pre-existing tests/CI, while letting a new goal add its own tests.
    baseline = Workspace(repo, ['tests/*', '.github/*'])
    protected = [p for p in DEFAULTS['protected'] if p not in ('tests/*', '.github/*')]
    protected += [name for name in baseline.files() if baseline.is_protected(name)]
    return {**DEFAULTS, 'rounds': 4, 'patience': 2, 'parallel': 1, 'max_steps': 24,
            'max_calls': 80, 'max_seconds': 900, 'protected': protected}


def _save(folder, state):
    atomic_json(folder / 'state.json', json.loads(redact(json.dumps(state))))


def _source_snapshot(root):
    """Hash source contents and link text without dereferencing symlinks."""
    values = {}
    for name in Workspace(root, []).files():
        path = root / name
        if path.is_symlink():
            data = b'link\0' + os.readlink(path).encode()
        else:
            path = Workspace(root, []).path(name)
            data = b'file\0' + path.read_bytes()
        values[name] = hashlib.sha256(data).hexdigest()
    return values


def _bound(root, config, budget):
    head = git(root, 'rev-parse', 'HEAD')
    before = _source_snapshot(root)
    status = git(root, 'status', '--porcelain')
    result = evaluate(root, config, budget)
    if (git(root, 'rev-parse', 'HEAD') != head or _source_snapshot(root) != before
            or git(root, 'status', '--porcelain') != status):
        raise ValueError('evaluation modified source; inspect the goal worktree before resuming')
    return {**result, 'head': head}


def _contract(root, config, intent, router, folder):
    policy = root / POLICY
    policy.mkdir(exist_ok=True)
    supplied_evaluate = config.get('evaluate')
    protected_commands = [str(Path(arg)) for cmd in [supplied_evaluate] + config.get('checks', []) if cmd
                          for arg in cmd if not Path(arg).is_absolute() and (root / arg).is_file()]
    config.update(delivery=True, direction='max', evaluate=supplied_evaluate or [sys.executable, '-B', f'{POLICY}/evaluate.py'],
                  protected=list(dict.fromkeys(config['protected'] + [f'{POLICY}/*'] + protected_commands)))
    inventory = Workspace(root, []).files()[:120]
    context = ('Create a small acceptance contract for this goal. Return JSON with "milestones" (nonempty list of '
               'small steps), "assumptions" (list), and "evaluator" (complete Python 3 stdlib source). '
               'The evaluator must print JSON {"score":0..100,"complete":boolean,"checks":...}. '
               'Missing implementation is score 0 and complete false, never an exception. '
               'Complete means every required acceptance check passes. Test observable behavior, not file presence alone. '
               'No network, install, mutations, credential access or deployment in the evaluator. '
               'Use bounded subprocess timeouts where required. This contract will be frozen before implementation. '
               'Never read, print or export credentials. Honor existing instructions; do not lower requirements for convenience.\nGOAL:\n' + intent +
               '\nFILES:\n' + json.dumps(inventory) + '\nROOT INSTRUCTIONS:\n' + Workspace(root, []).instructions() +
               '\nHOST TOOL AVAILABILITY:\n' + json.dumps(resources()))
    if supplied_evaluate:
        context += ('\nExisting fixed acceptance command must be preserved: ' + json.dumps(supplied_evaluate) +
                    '. Its successful output must include score 0..100 and boolean complete. Return milestones and assumptions; evaluator text is optional and will not replace this command.')
    feedback = ''
    for attempt in range(3):
        response = router.ask('planner', context + feedback, root, task=intent)
        milestones, evaluator = response.get('milestones'), response.get('evaluator')
        if not isinstance(milestones, list) or not milestones or not all(isinstance(x, str) and x.strip() for x in milestones):
            feedback = '\nREPAIR: milestones must be a nonempty list of small textual steps.'
            continue
        if not supplied_evaluate and (not isinstance(evaluator, str) or len(evaluator.encode()) > 48000):
            feedback = '\nREPAIR: provide bounded Python evaluator source.'
            continue
        if not supplied_evaluate:
            try:
                compile(evaluator, 'evaluate.py', 'exec')
            except SyntaxError:
                feedback = '\nREPAIR: evaluator has Python syntax errors.'
                continue
            (policy / 'evaluate.py').write_text(evaluator)
        result = _bound(root, config, router.budget)
        if not result['valid'] and supplied_evaluate:
            raise ValueError('configured evaluator must succeed and emit score 0..100 plus boolean complete')
        if not result['valid']:
            feedback = '\nREPAIR: evaluator must run successfully even on missing implementation; previous result: ' + result['reason']
            continue
        contract = {'intent': intent, 'milestones': milestones, 'assumptions': response.get('assumptions', []),
                    'evaluator_sha256': hashlib.sha256((evaluator if not supplied_evaluate else json.dumps(supplied_evaluate)).encode()).hexdigest(),
                    'evaluate': config['evaluate'],
                    'provenance': {'routes': config['routes'], 'models': list(config['models']), 'calls': router.budget.calls, 'generated_at': time.time()},
                    'limitation': 'Generated operational oracle; not proof of human correctness.'}
        atomic_json(policy / 'contract.json', json.loads(redact(json.dumps(contract))))
        atomic_json(folder / 'config.json', config)  # prepared transaction, before moving HEAD
        (root / 'INTENT.md').write_text(intent + '\n')
        if not (root / '.gitignore').exists():
            (root / '.gitignore').write_text('__pycache__/\n*.pyc\nnode_modules/\n.venv/\n.DS_Store\n')
        git(root, 'add', '--', POLICY, 'INTENT.md', '.gitignore')
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Freeze goal acceptance contract')
        return contract
    raise ValueError('planner could not produce a valid acceptance contract in three attempts')


class DeliveryEpoch(Run):
    """Reuse isolated candidates and commit guards, with nonregressing acceptance."""
    def __init__(self, root, config, intent, contract, feedback, allow_exec):
        super().__init__(root, config, intent, allow_exec)
        self.integration = root
        self.contract = contract
        self.feedback = feedback
        self.pinned = fingerprint(root, config['protected'])
        self.report = {'status': 'running', 'rounds': [], 'plateau': 0, 'branch': git(root, 'branch', '--show-current'),
                       'deadline_unix': time.time() + config['max_seconds']}

    def plan(self):
        prompt = ('Plan one small useful next milestone step. Known baseline defects need not all be fixed in one step. '
                  'Return JSON {"experiments":[{"task":"small specific next increment"}]}. Avoid large rewrites. '
                  'Build and test locally. External operations are single-worker milestones: inspect existing state, '
                  'use stable goal identifiers and idempotent/declarative CLI operations. Git rollback does not undo external state.\nGOAL:\n' +
                  self.intent + '\nCONTRACT:\n' + json.dumps(self.contract) + '\nRECENT FEEDBACK:\n' +
                  json.dumps(self.feedback + self.report['rounds'][-2:])[-7000:] + '\nFILES:\n' +
                  json.dumps(Workspace(self.integration, []).files()[:100]))
        reply = self.router.ask('planner', prompt, self.integration, task=self.intent)
        task = reply.get('experiments', [{}])[0].get('task')
        if not isinstance(task, str) or not task.strip():
            raise ValueError('planner returned no next step')
        return [self.intent + '\nMILESTONES:\n' + json.dumps(self.contract['milestones']) + '\nNEXT STEP:\n' + task[:2500]]

    def review(self, candidate):
        base = git(self.integration, 'rev-parse', 'HEAD')
        reply = self.router.ask('reviewer', 'Independently review this increment. Reject new regressions, gaming, scope '
                    'violations and useless changes. Known baseline defects may remain for later milestones. '
                    'Return JSON {"approve":boolean,"useful":boolean,"reason":"..."}.\nGOAL:\n' + self.intent +
                    '\nCONTRACT:\n' + json.dumps(self.contract) + '\nBASELINE:\n' + json.dumps(self.report['baseline']) +
                    '\nDIFF:\n' + git(self.integration, 'diff', base, candidate['commit']) + '\nEVALUATION:\n' +
                    json.dumps(candidate['evaluation']), self.integration, task=self.intent)
        if git(self.integration, 'rev-parse', 'HEAD') != base:
            raise ValueError('review changed accepted HEAD')
        candidate['review'] = {**reply, 'head': candidate['commit']}
        return reply.get('approve') is True and reply.get('useful') is True

    def completion(self, evaluation):
        if not evaluation.get('valid') or evaluation.get('complete') is not True:
            return False
        head = git(self.integration, 'rev-parse', 'HEAD')
        if evaluation.get('head') != head:
            raise ValueError('completion evidence does not match HEAD')
        ws = Workspace(self.integration, [])
        files = []
        size = 0
        for name in ws.files():
            if ws.is_protected(name) or name.startswith(POLICY + '/'):
                continue
            try:
                source = ws.path(name).read_text()
                if len(source) < 8000 and size + len(source) < 10000:
                    files.append({'path': name, 'content': source})
                    size += len(source)
            except (OSError, UnicodeError, ValueError):
                pass
        reply = self.router.ask('reviewer', 'Perform independent final acceptance review. Return JSON '
                '{"approve":boolean,"complete":boolean,"reason":"..."}. Approve completion only when all '
                'required goal behaviors are demonstrated. Reject vacuous/gamed evaluator evidence, missing coverage '
                'and unsupported claims. Source excerpts may be incomplete; uncertainty means complete false.\nGOAL:\n' +
                self.intent + '\nCONTRACT:\n' + json.dumps(self.contract) + '\nEVIDENCE:\n' + json.dumps(evaluation) +
                '\nFILES:\n' + json.dumps(ws.files()) + '\nSOURCE:\n' + json.dumps(files), self.integration, task=self.intent)
        self.report['completion_review'] = {**reply, 'head': head}
        if fingerprint(self.integration, self.config['protected']) != self.pinned or git(self.integration, 'rev-parse', 'HEAD') != head or git(self.integration, 'status', '--porcelain'):
            raise ValueError('completion review modified source')
        return reply.get('approve') is True and reply.get('complete') is True

    def run_epoch(self):
        try:
            baseline = _bound(self.integration, self.config, self.budget)
            self.report.update(baseline=baseline, best=baseline.get('score'))
            if not baseline['valid']:
                raise ValueError('invalid delivery baseline')
            if self.completion(baseline):
                self.report['status'] = 'complete'
                return self.report
            for number in range(1, self.config['rounds'] + 1):
                self.budget.remaining()
                task = self.plan()[0]
                candidate = self.experiment(task, number, 0)
                round_result = {'round': number, 'experiments': [candidate], 'accepted': None}
                self.report['rounds'].append(round_result)
                if candidate.get('unavailable') is not None:
                    raise ModelUnavailable(candidate['unavailable']['retry_at'])
                if candidate.get('budget_exhausted'):
                    raise BudgetExceeded(candidate['error'])
                evaluation = candidate.get('evaluation', {})
                if evaluation.get('valid') and evaluation['score'] >= self.report['best'] and self.review(candidate):
                    if fingerprint(self.integration, self.config['protected']) != self.pinned or git(self.integration, 'status', '--porcelain'):
                        raise ValueError('review modified accepted source')
                    round_result['accepted'] = candidate['commit']
                    self.checkpoint()  # durable approval before the integration commit moves
                    git(self.integration, 'merge', '--ff-only', candidate['commit'])
                    self.report['best'] = evaluation['score']
                    round_result['accepted'] = candidate['commit']
                    self.report['plateau'] = 0
                    evaluation['head'] = candidate['commit']
                    if self.completion(evaluation):
                        self.report['status'] = 'complete'
                        break
                else:
                    self.report['plateau'] += 1
                self.checkpoint()
                if self.report['plateau'] >= self.config['patience']:
                    self.report['status'] = 'plateau'
                    break
            else:
                self.report['status'] = 'round_limit'
        except ModelUnavailable as exc:
            self.report.update(status='waiting', next_retry_at=exc.retry_at)
        except BudgetExceeded as exc:
            self.report.update(status='budget_exhausted', reason=str(exc))
        finally:
            self.checkpoint()
        return self.report


def _wait(until):
    while time.time() < until:
        time.sleep(min(1, max(0, until - time.time())))


def start_goal(repo: Path, intent: str | None = None, *, backend='auto', model=None, once=False,
               resume_goal=None, allow_exec=True, config_path='soso.json', state_dir=None, models_path=None):
    """Deliver persistently; --once returns after one bounded epoch or provider wait.

    Source must be clean. State lives in Git common-dir/soso/goals unless supplied.
    An explicit resume validates the immutable policy and accepted HEAD before work.
    """
    repo = Path(repo).resolve()
    storage = _storage(repo)
    goals = Path(state_dir).resolve() if state_dir else storage / 'goals'
    goals.mkdir(parents=True, exist_ok=True)
    goal_id = resume_goal or time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    if not goal_id.replace('-', '').isalnum():
        raise ValueError('invalid goal id')
    folder = goals / goal_id
    if resume_goal:
        state = json.loads((folder / 'state.json').read_text())
        if state['source'] != str(repo):
            raise ValueError('resume goal belongs to another source repository')
        state['allow_exec'] = state.get('allow_exec', True) and allow_exec
    else:
        if (repo / POLICY).exists():
            raise ValueError('reserved .soso-delivery directory already exists in source')
        if git(repo, 'status', '--porcelain'):
            raise ValueError('source is dirty; commit or move pending changes before starting a goal')
        if intent is None or intent.strip().lower() == 'read intent.md':
            intent = (repo / 'INTENT.md').read_text()
        if not intent.strip() or len(intent.encode()) > 8000:
            raise ValueError('intent must contain 1..8000 bytes')
        requested_pool = models_path or os.environ.get('SOSO_MODELS_FILE')
        model_pool = load_model_pool(requested_pool) if requested_pool else None
        folder.mkdir()
        if model_pool is not None:
            atomic_json(folder / 'model-pool.json', model_pool)
        state = {'goal_id': goal_id, 'source': str(repo), 'intent': redact(intent), 'status': 'starting',
                 'branch': 'soso/goal-' + goal_id, 'worktree': str(folder / 'worktree'), 'epochs': [],
                 'next_retry_at': None, 'failures': 0, 'backend': backend, 'model': model,
                 'allow_exec': allow_exec, 'config_path': config_path,
                 'models_path': str(Path(requested_pool).resolve()) if requested_pool else None}
        git(repo, 'worktree', 'add', '-b', state['branch'], state['worktree'], 'HEAD')
        state['head'] = git(Path(state['worktree']), 'rev-parse', 'HEAD')
        _save(folder, state)
    root = Path(state['worktree'])
    with (storage / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another soso controller owns this repository') from None
        if not state.get('policy') and (folder / 'config.json').exists() and (root / POLICY / 'contract.json').exists():
            recovered = json.loads((folder / 'config.json').read_text())
            contract = json.loads((root / POLICY / 'contract.json').read_text())
            generated = root / POLICY / 'evaluate.py'
            if generated.exists() and hashlib.sha256(generated.read_bytes()).hexdigest() != contract['evaluator_sha256']:
                raise ValueError('prepared acceptance evaluator changed')
            pending = git(root, 'status', '--porcelain')
            if pending:
                names = git(root, 'diff', '--name-only', 'HEAD').splitlines()
                names += git(root, 'ls-files', '--others', '--exclude-standard').splitlines()
                if any(n not in ('INTENT.md', '.gitignore') and not n.startswith(POLICY + '/') for n in names):
                    raise ValueError('source changed during acceptance contract preparation')
                (root / 'INTENT.md').write_text(state['intent'] + '\n')
                if not (root / '.gitignore').exists():
                    (root / '.gitignore').write_text('__pycache__/\n*.pyc\nnode_modules/\n.venv/\n.DS_Store\n')
                git(root, 'add', '--', POLICY, 'INTENT.md', '.gitignore')
                git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Freeze goal acceptance contract')
            state.update(contract=contract, protected=recovered['protected'],
                         policy=fingerprint(root, recovered['protected']), head=git(root, 'rev-parse', 'HEAD'),
                         config_sha256=hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest())
        pending = git(root, 'status', '--porcelain')
        if not state.get('policy'):
            # Interrupted contract generation may leave only its own policy file.
            pending = '\n'.join(line for line in pending.splitlines() if not line[3:].startswith(POLICY + '/'))
        active_report = state.get('active_epoch')
        if active_report and Path(active_report).is_file():
            active = json.loads(Path(active_report).read_text())
            current = git(root, 'rev-parse', 'HEAD')
            accepted = [r['accepted'] for r in active.get('rounds', []) if r.get('accepted')]
            if current == active.get('head') or current in accepted:
                state['head'] = current
                if not any(e['report'] == active_report for e in state['epochs']):
                    state['epochs'].append({'status': 'interrupted', 'head': current, 'best': active.get('best'),
                                            'report': active_report, 'feedback': active.get('rounds', [])[-2:]})
            state.pop('active_epoch', None)
        if pending or git(root, 'rev-parse', 'HEAD') != state['head']:
            raise ValueError('goal worktree or HEAD changed; inspect before resuming')
        if state.get('policy') and fingerprint(root, state['protected']) != state['policy']:
            raise ValueError('goal acceptance policy changed')
        if state['status'] == 'complete':
            return {**state, 'report': str(folder / 'state.json')}
        while True:
            try:
                if state.get('next_retry_at') and state['next_retry_at'] > time.time():
                    if once:
                        break
                    _wait(state['next_retry_at'])
                state.update(status='running', next_retry_at=None)
                _save(folder, state)
                if state.get('policy') and hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest() != state.get('config_sha256'):
                    raise ValueError('frozen epoch configuration changed')
                config = json.loads((folder / 'config.json').read_text()) if state.get('policy') else _config(repo, state['config_path'])
                if not state.get('policy') and (folder / 'model-pool.json').exists():
                    config.update(json.loads((folder / 'model-pool.json').read_text()))
                # Resume preserves the original route unless explicitly overridden.
                selected_backend = backend if backend != 'auto' or model is not None else state['backend']
                selected_model = model if model is not None else state['model']
                config = configure_models(config, selected_backend, selected_model)
                config['_router_state'] = str(folder / 'router-state.json')
                config['routes'] = {**config['routes'], 'planner': config['routes'].get('planner', config['routes']['worker']),
                                    'reviewer': config['routes'].get('reviewer', config['routes']['worker'])}
                config['protected'] = list(dict.fromkeys(config['protected'] + [state['config_path']]))
                config['delivery'] = True
                config['direction'] = 'max'
                if not state.get('policy'):
                    router = Router(config, Budget(min(config['max_calls'], 6), min(config['max_seconds'], 240)), lambda *a, **k: None)
                    contract = _contract(root, config, state['intent'], router, folder)
                    state.update(contract=contract, protected=config['protected'], policy=fingerprint(root, config['protected']),
                                 head=git(root, 'rev-parse', 'HEAD'),
                                 config_sha256=hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest())
                    _save(folder, state)
                epoch = DeliveryEpoch(root, config, state['intent'], state['contract'], state['epochs'][-2:], state['allow_exec'])
                state['active_epoch'] = str(epoch.report_path)
                _save(folder, state)
                report = epoch.run_epoch()
                state.pop('active_epoch', None)
                state['epochs'].append({'status': report['status'], 'head': report['head'], 'best': report.get('best'),
                                        'report': str(epoch.report_path), 'feedback': report.get('rounds', [])[-2:]})
                state['head'] = report['head']
                if report['status'] == 'complete':
                    state.update(status='complete', next_retry_at=None)
                elif report['status'] == 'waiting':
                    raise ModelUnavailable(report.get('next_retry_at'))
                else:
                    state.update(status='incomplete', next_retry_at=time.time() + 2)
                state['failures'] = 0
                _save(folder, state)
                if once or state['status'] == 'complete':
                    break
            except ModelUnavailable as exc:
                state['failures'] += 1
                state.update(status='waiting', next_retry_at=max(time.time() + 1, exc.retry_at) if exc.retry_at is not None else
                             time.time() + min(900, 30 * 2 ** min(state['failures'] - 1, 5)))
                _save(folder, state)
                if once:
                    break
            except BudgetExceeded:
                state.update(status='incomplete', next_retry_at=time.time() + 2)
                _save(folder, state)
                if once:
                    break
            except KeyboardInterrupt:
                state['status'] = 'interrupted'
                _save(folder, state)
                break
            except Exception:
                state['status'] = 'blocked'
                _save(folder, state)
                raise
    return {**state, 'report': str(folder / 'state.json')}
