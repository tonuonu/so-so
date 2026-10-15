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


class ContractRepairNeeded(RuntimeError):
    """A bounded acceptance setup epoch ended without an approved oracle."""


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


def _config(repo, config_path, ignore_project_config=False):
    path = repo / config_path
    if path.exists() and not ignore_project_config:
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


def _contract(root, config, intent, router, folder, previous_feedback=''):
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
               'Resource inventories and model-routing preferences describe the controller environment, not application features to test. '
               'Use temporary test fixtures and loopback-only app servers when needed for behavioral acceptance; clean them up. '
               'No external network, installation, source changes, credential access or deployment in the evaluator. '
               'Use bounded subprocess timeouts where required. The generated evaluator is saved at .soso-delivery/evaluate.py '
               'and executed with cwd equal to the project root. Path(__file__).parent is the policy directory, not the project root; '
               'resolve application paths from Path.cwd() or Path(__file__).resolve().parents[1]. '
               'Exercise required behavior independently: file names, keywords, HTML comments, and a worker-owned test script '
               'printing a pass marker cannot prove behavioral acceptance. This contract will be reviewed and frozen before implementation. '
               'Never read, print or export credentials. Honor existing instructions; do not lower requirements for convenience.\nGOAL:\n' + intent +
               '\nFILES:\n' + json.dumps(inventory) + '\nROOT INSTRUCTIONS:\n' + Workspace(root, []).instructions() +
               '\nHOST TOOL AVAILABILITY:\n' + json.dumps(resources()))
    if supplied_evaluate:
        context += ('\nExisting fixed acceptance command must be preserved: ' + json.dumps(supplied_evaluate) +
                    '. Its successful output must include score 0..100 and boolean complete. Return milestones and assumptions; evaluator text is optional and will not replace this command.')
    feedback = previous_feedback[:2000]
    latest_review = folder / 'contract-review.json'
    if not supplied_evaluate and latest_review.exists():
        previous = json.loads(latest_review.read_text())
        if previous.get('review', {}).get('approve') is not True:
            feedback = '\nPREVIOUS ACCEPTANCE REVIEW REJECTION: ' + str(previous.get('review', {}).get('reason', 'repair the acceptance oracle'))[:2000]
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
        acceptance_review = None
        if not supplied_evaluate:
            before = (git(root, 'rev-parse', 'HEAD'), _source_snapshot(root), git(root, 'status', '--porcelain'))
            review = router.ask('reviewer',
                'Independently review this proposed acceptance contract BEFORE it is frozen or implementation starts. '
                'Return JSON {"approve":boolean,"reason":"concrete defects and required repairs, or evidence of sound coverage"}. '
                'Reject incorrect application paths: the evaluator is .soso-delivery/evaluate.py and cwd is the project root. '
                'Path(__file__).parent denotes .soso-delivery; it does not contain the application. '
                'Missing implementation must yield score 0 and complete false without failing the evaluator. '
                'Every required behavioral claim must be exercised by independent assertions against the implementation. '
                'Reject file/keyword presence, HTML comments, worker prose, or trusting a worker-owned test.js/test script '
                'to print a pass marker as proof of app behavior. Such substitutes must never yield complete true. '
                'Require tests of actual required behavior with independently determined expectations, and reject vacuous coverage. '
                'Temporary fixtures and loopback-only app servers are permitted, with bounded timeouts and cleanup. '
                'Reject external network, installation, source changes, credential access or deployment. '
                'A syntactically valid zero baseline is not sufficient; consider whether a correct implementation can pass '
                'and whether a fake implementation could pass. Do not edit files.\nGOAL:\n' + intent +
                '\nMILESTONES:\n' + json.dumps(milestones) + '\nEVALUATOR LOCATION:\n' + str(policy / 'evaluate.py') +
                '\nEXECUTION CWD (PROJECT ROOT):\n' + str(root) + '\nFIXED ARGV:\n' + json.dumps(config['evaluate']) +
                '\nEVALUATOR SOURCE:\n' + evaluator + '\nBASELINE:\n' + json.dumps(result), root, task=intent)
            if (git(root, 'rev-parse', 'HEAD'), _source_snapshot(root), git(root, 'status', '--porcelain')) != before:
                raise ValueError('acceptance review modified source')
            acceptance_review = {'attempt': attempt + 1, 'evaluator_sha256': hashlib.sha256(evaluator.encode()).hexdigest(),
                                 'location': str(policy / 'evaluate.py'), 'cwd': str(root), 'baseline': result,
                                 'review': review, 'time': time.time()}
            safe_review = json.loads(redact(json.dumps(acceptance_review)))
            with (folder / 'contract-reviews.jsonl').open('a') as stream:
                stream.write(json.dumps(safe_review) + '\n')
            atomic_json(latest_review, safe_review)
            if review.get('approve') is not True:
                feedback = '\nREPAIR ACCEPTANCE CONTRACT AFTER INDEPENDENT REVIEW: ' + str(review.get('reason', 'reviewer did not approve; repair behavioral coverage and paths'))[:2000]
                continue
        contract = {'intent': intent, 'milestones': milestones, 'assumptions': response.get('assumptions', []),
                    'evaluator_sha256': hashlib.sha256((evaluator if not supplied_evaluate else json.dumps(supplied_evaluate)).encode()).hexdigest(),
                    'evaluate': config['evaluate'],
                    'provenance': {'routes': config['routes'], 'models': list(config['models']), 'calls': router.budget.calls, 'generated_at': time.time()},
                    'limitation': 'Generated operational oracle; not proof of human correctness.'}
        if acceptance_review is not None:
            contract['acceptance_review'] = acceptance_review
        atomic_json(policy / 'contract.json', json.loads(redact(json.dumps(contract))))
        atomic_json(folder / 'config.json', config)  # prepared transaction, before moving HEAD
        (root / 'INTENT.md').write_text(intent + '\n')
        if not (root / '.gitignore').exists():
            (root / '.gitignore').write_text('__pycache__/\n*.pyc\nnode_modules/\n.venv/\n.DS_Store\n')
        git(root, 'add', '--', POLICY, 'INTENT.md', '.gitignore')
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Freeze goal acceptance contract')
        return contract
    raise ContractRepairNeeded(feedback or 'acceptance contract still needs independent approval')


class DeliveryEpoch(Run):
    """Reuse isolated candidates and commit guards, with nonregressing acceptance."""
    MAX_REPAIRS = 3

    def __init__(self, root, config, intent, contract, feedback, allow_exec, draft=None):
        super().__init__(root, config, intent, allow_exec)
        self.integration = root
        self.contract = contract
        self.feedback = feedback
        self._continuation = None
        self.pinned = fingerprint(root, config['protected'])
        self.report = {'status': 'running', 'rounds': [], 'plateau': 0, 'branch': git(root, 'branch', '--show-current'),
                       'deadline_unix': time.time() + config['max_seconds']}
        if draft:
            self.report['draft'] = draft

    def draft_binding(self):
        return {'config_sha256': hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest(),
                'intent_sha256': hashlib.sha256(self.intent.encode()).hexdigest(), 'policy': self.pinned}

    def pending_candidate(self):
        draft = self.report.get('draft')
        if not draft:
            return None
        base = git(self.integration, 'rev-parse', 'HEAD')
        if (draft.get('base') != base or draft.get('phase') not in ('worker', 'evaluate', 'review')
                or not isinstance(draft.get('task'), str) or not draft['task'].strip()
                or type(draft.get('repairs', 0)) is not int
                or not 0 <= draft.get('repairs', 0) <= self.MAX_REPAIRS
                or not isinstance(draft.get('repair_feedback', ''), str)
                or len(draft.get('repair_feedback', '')) > 2000
                or (draft.get('repairs', 0) and not draft.get('repair_feedback', '').strip())
                or any(draft.get(k) != v for k, v in self.draft_binding().items())):
            raise ValueError('retained candidate base or policy binding changed')
        ref = draft.get('ref', '')
        if (not isinstance(ref, str) or not ref.startswith('refs/soso/')
                or git(self.root, 'rev-parse', '--verify', ref) != draft.get('commit')
                or git(self.root, 'merge-base', base, draft['commit']) != base):
            raise ValueError('retained candidate ref or history changed')
        return draft

    def retain_candidate(self, path, result, base, expected, name, phase):
        # This bounded local checkpoint does not consume an expired inference budget.
        previous = self._continuation or {}
        self.report.pop('draft', None)
        if (git(self.integration, 'rev-parse', 'HEAD') != base
                or git(path, 'rev-parse', 'HEAD') != expected
                or fingerprint(path, self.config['protected']) != self.pinned):
            raise ValueError('cannot retain candidate with changed policy, history or base')
        dirty = git(path, 'status', '--porcelain')
        if phase != 'worker' and dirty:
            raise ValueError('cannot retain candidate modified by evaluation')
        if dirty:
            git(path, 'add', '--all')
            git(path, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Checkpoint unfinished delivery candidate')
        commit = git(path, 'rev-parse', 'HEAD')
        if commit == base:
            return
        ref = f'refs/soso/{self.run_id}/{name}'
        git(self.root, 'update-ref', ref, commit)
        self.report['draft'] = {**self.draft_binding(), 'task': previous.get('task', result['task']), 'base': base,
                                'commit': commit, 'ref': ref, 'phase': phase,
                                'repairs': previous.get('repairs', 0),
                                'repair_feedback': previous.get('repair_feedback', '')}
        self.checkpoint()  # ref and descriptor are durable before the worktree is removed

    def repair_candidate(self, candidate):
        """Keep a useful rejected increment for a bounded repair, never integration."""
        review = candidate.get('review', {})
        evaluation = candidate.get('evaluation', {})
        reason = review.get('reason')
        if (review.get('approve') is not False or review.get('useful') is not True
                or review.get('head') != candidate.get('commit')
                or evaluation.get('valid') is not True or evaluation['score'] < self.report['best']
                or not isinstance(reason, str) or not reason.strip()):
            return False
        draft = self.pending_candidate()
        if not draft or draft['commit'] != candidate['commit'] or draft.get('repairs', 0) >= self.MAX_REPAIRS:
            return False
        if (fingerprint(self.integration, self.config['protected']) != self.pinned
                or git(self.integration, 'status', '--porcelain')):
            raise ValueError('review modified accepted source')
        draft.update(phase='worker', repairs=draft.get('repairs', 0) + 1,
                     repair_feedback=reason.strip()[:2000])
        self.checkpoint()
        return True

    def plan(self):
        prompt = ('Plan one small useful next milestone step. Known baseline defects need not all be fixed in one step. '
                  'Return JSON {"experiments":[{"task":"small specific next increment"}],"monitoring":[{"label":"intent-specific metric","source":"best_score","unit":"optional unit"}]}. Avoid large rewrites. '
                  'Optionally define up to six dashboard metrics for this goal. Sources: best_score, baseline_score, accepted_count, plateau_count, or metric:NAME for numeric evaluator metrics. '
                  'Build and test locally. External operations are single-worker milestones: inspect existing state, '
                  'use stable goal identifiers and idempotent/declarative CLI operations. Git rollback does not undo external state.\nGOAL:\n' +
                  self.intent + '\nCONTRACT:\n' + json.dumps(self.contract) + '\nRECENT FEEDBACK:\n' +
                  json.dumps(self.feedback + self.report['rounds'][-2:])[-7000:] + '\nFILES:\n' +
                  json.dumps(Workspace(self.integration, []).files()[:100]))
        reply = self.router.ask('planner', prompt, self.integration, task=self.intent)
        task = reply.get('experiments', [{}])[0].get('task')
        if not isinstance(task, str) or not task.strip():
            raise ValueError('planner returned no next step')
        self.log('plan', tasks=[task[:2500]], skills=['writing-plans'], monitoring=reply.get('monitoring', []))
        return [self.intent + '\nMILESTONES:\n' + json.dumps(self.contract['milestones']) + '\nNEXT STEP:\n' + task[:2500]]

    def review(self, candidate):
        base = git(self.integration, 'rev-parse', 'HEAD')
        reply = self.router.ask('reviewer', 'Independently review this increment. This is NOT final acceptance. '
                    'Approve a useful nonregressing partial increment even when the goal is incomplete or milestones '
                    'remain unmet. Compare against the baseline and distinguish new defects introduced by this change '
                    'from remaining work. Missing future features or tests alone are not grounds to discard useful '
                    'baseline progress; final acceptance separately requires all behaviors and adequate coverage. '
                    'Reject new regressions, gaming, scope violations and useless changes, including changes that '
                    'break an established interface or invalidate existing evidence. '
                    'Return JSON {"approve":boolean,"useful":boolean,"reason":"...","milestone_statuses":[{"index":0,"status":"complete|in_progress|not_started","evidence":"specific test evidence"}]}. Report zero-based contract milestones; complete requires concrete evidence.\nGOAL:\n' + self.intent +
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
            self.checkpoint()
            self.log('activity', phase='baseline', task='Baseline acceptance checks')
            baseline = _bound(self.integration, self.config, self.budget)
            self.report.update(baseline=baseline, best=baseline.get('score'))
            if not baseline['valid']:
                raise ValueError('invalid delivery baseline')
            if self.completion(baseline):
                self.report.pop('draft', None)
                self.report['status'] = 'complete'
                return self.report
            for number in range(1, self.config['rounds'] + 1):
                self.budget.remaining()
                draft = self.pending_candidate()
                task = draft['task'] if draft else self.plan()[0]
                if draft and draft.get('repair_feedback'):
                    task += ('\nREVIEW REPAIR REQUIRED:\nKeep the useful existing candidate and fix the following '
                             'review findings before requesting acceptance. This draft has not been accepted.\n'
                             + draft['repair_feedback'])
                self._continuation = draft
                self.report.pop('draft', None)
                candidate = self.experiment(task, number, 0, draft)
                round_result = {'round': number, 'experiments': [candidate], 'accepted': None}
                self.report['rounds'].append(round_result)
                if candidate.get('unavailable') is not None:
                    raise ModelUnavailable(candidate['unavailable']['retry_at'], candidate['unavailable'].get('reason', 'provider unavailable'))
                if candidate.get('budget_exhausted'):
                    raise BudgetExceeded(candidate['error'])
                if candidate.get('interrupted'):
                    raise KeyboardInterrupt
                evaluation = candidate.get('evaluation', {})
                approved = False
                if evaluation.get('valid') and evaluation['score'] >= self.report['best']:
                    self.report['draft']['phase'] = 'review'
                    self.checkpoint()
                    approved = self.review(candidate)
                if approved:
                    if fingerprint(self.integration, self.config['protected']) != self.pinned or git(self.integration, 'status', '--porcelain'):
                        raise ValueError('review modified accepted source')
                    round_result['accepted'] = candidate['commit']
                    self.checkpoint()  # durable approval before the integration commit moves
                    git(self.integration, 'merge', '--ff-only', candidate['commit'])
                    self.report.pop('draft', None)
                    self.report['best'] = evaluation['score']
                    round_result['accepted'] = candidate['commit']
                    self.report['plateau'] = 0
                    evaluation['head'] = candidate['commit']
                    self.checkpoint()
                    if self.completion(evaluation):
                        self.report['status'] = 'complete'
                        break
                else:
                    if not self.repair_candidate(candidate):
                        self.report.pop('draft', None)
                    self.report['plateau'] += 1
                self.checkpoint()
                if self.report['plateau'] >= self.config['patience']:
                    self.report['status'] = 'plateau'
                    break
            else:
                self.report['status'] = 'round_limit'
        except ModelUnavailable as exc:
            self.report.update(status='waiting', reason=str(exc), next_retry_at=exc.retry_at)
        except BudgetExceeded as exc:
            self.report.update(status='budget_exhausted', reason=str(exc))
        except KeyboardInterrupt:
            self.report['status'] = 'interrupted'
        finally:
            draft = self.report.get('draft')
            if (draft and git(self.integration, 'rev-parse', 'HEAD') == draft['commit']
                    and any(r.get('accepted') == draft['commit'] for r in self.report['rounds'])):
                self.report.pop('draft', None)
            self.checkpoint()
        return self.report


def _wait(until):
    while time.time() < until:
        time.sleep(min(1, max(0, until - time.time())))


def start_goal(repo: Path, intent: str | None = None, *, backend='auto', model=None, once=False,
               resume_goal=None, allow_exec=True, config_path='soso.json', state_dir=None, models_path=None, ignore_project_config=False):
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
                 'models_path': str(Path(requested_pool).resolve()) if requested_pool else None,
                 'ignore_project_config': ignore_project_config}
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
                draft = active.get('draft')
                state['draft'] = None if draft and draft['commit'] == current and current in accepted else draft
                if not any(e['report'] == active_report for e in state['epochs']):
                    state['epochs'].append({'status': 'interrupted', 'head': current, 'best': active.get('best'),
                                            'report': active_report, 'feedback': active.get('rounds', [])[-2:],
                                            'completion_review': active.get('completion_review')})
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
                state.update(status='running', next_retry_at=None, reason=None)
                _save(folder, state)
                if state.get('policy') and hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest() != state.get('config_sha256'):
                    raise ValueError('frozen epoch configuration changed')
                config = json.loads((folder / 'config.json').read_text()) if state.get('policy') else _config(repo, state['config_path'], state.get('ignore_project_config', False))
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
                    def setup_log(event, **data):
                        with (folder / 'setup-events.jsonl').open('a') as stream:
                            stream.write(json.dumps({'time': time.time(), 'event': event, **data}) + '\n')
                    router = Router(config, Budget(min(config['max_calls'], 6), min(config['max_seconds'], 600)), setup_log)
                    contract = _contract(root, config, state['intent'], router, folder, state.get('setup_repair_reason', ''))
                    state.pop('setup_repair_reason', None)
                    state['setup_repairs'] = 0
                    state.update(contract=contract, protected=config['protected'], policy=fingerprint(root, config['protected']),
                                 head=git(root, 'rev-parse', 'HEAD'),
                                 config_sha256=hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest())
                    _save(folder, state)
                epoch = DeliveryEpoch(root, config, state['intent'], state['contract'], state['epochs'][-2:], state['allow_exec'], state.get('draft'))
                state['active_epoch'] = str(epoch.report_path)
                _save(folder, state)
                report = epoch.run_epoch()
                state.pop('active_epoch', None)
                state['epochs'].append({'status': report['status'], 'head': report['head'], 'best': report.get('best'),
                                        'report': str(epoch.report_path), 'feedback': report.get('rounds', [])[-2:],
                                        'completion_review': report.get('completion_review')})
                state['head'] = report['head']
                state['draft'] = report.get('draft')
                if report['status'] == 'complete':
                    state.update(status='complete', next_retry_at=None)
                elif report['status'] == 'waiting':
                    raise ModelUnavailable(report.get('next_retry_at'), report.get('reason', 'provider temporarily unavailable'))
                elif report['status'] == 'interrupted':
                    state.update(status='interrupted', next_retry_at=None)
                else:
                    state.update(status='incomplete', next_retry_at=time.time() + 2)
                state['failures'] = 0
                _save(folder, state)
                if once or state['status'] in ('complete', 'interrupted'):
                    break
            except ModelUnavailable as exc:
                state['failures'] += 1
                state.update(status='waiting', reason=str(exc), next_retry_at=max(time.time() + 1, exc.retry_at) if exc.retry_at is not None else
                             time.time() + min(900, 30 * 2 ** min(state['failures'] - 1, 5)))
                print('Waiting: ' + str(exc) + '. Retrying automatically at ' + time.strftime('%H:%M:%S', time.localtime(state['next_retry_at'])) + '. Ctrl+C pauses the run.', flush=True)
                _save(folder, state)
                if once:
                    break
            except ContractRepairNeeded as exc:
                state['setup_repairs'] = state.get('setup_repairs', 0) + 1
                state.update(status='incomplete', setup_repair_reason=str(exc)[:2000], next_retry_at=
                             time.time() + min(60, 2 * 2 ** min(state['setup_repairs'] - 1, 5)))
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
