"""Read-only run dashboard. No model calls or execution endpoints."""
import argparse
import collections
import contextlib
import fcntl
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import stat
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
from .models import redact

LIMIT = 2_000_000
IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
IMAGE_LIMIT = 10_000_000
CHUNK = 65536  # bytes per send; a slow client must take each within Dashboard.REQUEST_SECONDS
SCALAR = (str, int, float, bool, type(None))


def field(record, key, kind, default):
    """record[key] when it has the recorded type, else the default: a hand-edited record never stops the monitor."""
    value = record.get(key, default)
    return value if isinstance(value, kind) else default


def wellformed(event):
    """Keys the snapshot indexes by are hashable and measurements are numbers; other events are skipped."""
    return (all(isinstance(event.get(key), SCALAR) for key in ('event', 'call_id', 'model', 'role', 'tool', 'phase'))
            and all(type(event.get(key, 0)) in (int, float) for key in ('time', 'seconds', 'prompt_bytes')))


def read_image(path, limit=IMAGE_LIMIT):
    """At most limit bytes of a regular file; never waits on a FIFO or device, nor follows a final symlink."""
    with open(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('not a regular file')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('asset too large')
    return data


def wildcard(address):
    """Whether an IP address names every interface, also as IPv4-mapped IPv6 ('::ffff:0.0.0.0')."""
    address = ipaddress.ip_address(address)
    return int(getattr(address, 'ipv4_mapped', None) or address) == 0  # not is_unspecified: mapped is False on 3.11


def unspecified(host):
    """Whether a numeric host names every interface, in any spelling ('0', '0x0', '0.0.0.0.', '::0')."""
    try:
        return any(wildcard(info[4][0])
                   for info in socket.getaddrinfo(host.rstrip('.'), None, flags=socket.AI_NUMERICHOST))
    except (OSError, ValueError):
        return False  # a hostname


def roadmap(events, report, status, live, calls):
    """Observed stages of this epoch; never infer overall completion from a phase."""
    labels = [('prepare', 'Preparation', 'Read intent and prepare the workspace'),
              ('baseline', 'Baseline checks', 'Check the starting state before implementation'),
              ('plan', 'Planning', 'Decide the next tasks and acceptance criteria'),
              ('implement', 'Implementation', 'Write and revise the work'),
              ('verify', 'Validation & review', 'Check a candidate; revisions may follow'),
              ('outcome', 'Outcome', 'Accepted completion or an explicit stopping condition')]
    observed = {'prepare'}
    current = 'prepare'
    roles = {'architect': 'plan', 'coordinator': 'plan', 'planner': 'plan', 'worker': 'implement', 'reviewer': 'verify'}
    for event in events:
        stage = None
        kind, phase = event.get('event'), event.get('phase')
        if kind == 'activity':
            if phase == 'baseline' or str(event.get('task', '')).startswith('Baseline'):
                stage = 'baseline'
            else:
                stage = {'writing': 'implement', 'testing': 'verify', 'final_acceptance': 'verify'}.get(phase)
        elif kind in ('call_started', 'model_request'):
            stage = 'plan' if event.get('phase') == 'contract_setup' else roles.get(event.get('role'))
        elif kind == 'plan':
            stage = 'plan'
        elif kind == 'tool_started':
            stage = 'verify' if event.get('tool') == 'check' else 'implement'
        if stage:
            observed.add(stage)
            current = stage
    if field(report, 'baseline', dict, {}).get('valid'):
        observed.add('baseline')
    active = {'plan' if c.get('phase') == 'contract_setup' else roles[c['role']] for c in calls if c.get('live') and c.get('role') in roles}
    if status in ('complete', 'optimized'):
        current, active = 'outcome', {'outcome'}
        observed.add('outcome')
    elif not active:
        active = {current}
    stopped = not live or status in ('blocked', 'waiting', 'interrupted')
    return [{'id': key, 'label': label, 'description': description,
             'state': ('complete' if key == 'outcome' and status in ('complete', 'optimized') else
                       ('paused' if stopped else 'active') if key in active else
                       'observed' if key in observed else 'upcoming')}
            for key, label, description in labels]


class Monitor:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / '.so-so'

    def path(self, value):
        path = Path(value).resolve()
        if self.root.is_symlink() or not path.is_relative_to(self.root):
            raise ValueError('outside managed run storage')
        return path

    def read(self, value):
        try:
            path = self.path(value)
            if path.stat().st_size > LIMIT:
                return {}
            result = json.loads(path.read_text())
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def events(self, value):
        try:
            with self.path(value).open('rb') as stream:
                stream.seek(0, 2)
                offset = max(0, stream.tell() - LIMIT)
                stream.seek(offset)
                if offset:
                    stream.readline()
                lines = stream.read(LIMIT).splitlines()
            result = []
            for line in lines[-2000:]:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and wellformed(event):
                        result.append(event)
                except ValueError:
                    pass  # A writer may be midway through its final line.
            return result
        except (OSError, ValueError):
            return []

    def running(self):
        try:
            with (self.root / 'launch.lock').open('r') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        return False

    def snapshot(self):
        active = self.read(self.root / 'active.json')
        project = field(active, 'project', str, '')
        folder = Path(project).parent if project else self.root
        journal = self.read(folder / 'optimization.json')
        goal = {}
        goal_folder = None
        if project:
            goals = Path(project) / '.git/soso/goals'
            if active.get('goal_id'):
                goal_folder = goals / str(active['goal_id'])
                goal = self.read(goal_folder / 'state.json')
            else:
                # Setup logs exist before the synchronous launcher receives a goal ID.
                candidates = []
                for path in goals.glob('*/state.json'):
                    state = self.read(path)
                    if state.get('source') == project and state.get('goal_id') == path.parent.name:
                        candidates.append((path.parent.name, path.parent, state))
                if candidates:
                    _, goal_folder, goal = max(candidates, key=lambda item: item[0])
        report_name = field(goal, 'active_epoch', str, None) or field(active, 'report', str, None)
        if goal and (not report_name or Path(report_name).name == 'state.json'):
            report_name = next((epoch['report'] for epoch in reversed(field(goal, 'epochs', list, []))
                                if isinstance(epoch, dict) and field(epoch, 'report', str, '')), None)
        report = self.read(report_name) if report_name else {}
        paths = [epoch['report'] for epoch in field(journal or goal, 'epochs', list, [])[-20:]
                 if isinstance(epoch, dict) and field(epoch, 'report', str, '')]
        if report_name:
            paths.append(report_name)
        paths = list(dict.fromkeys(paths))
        events = []
        reports = []
        current_events = []
        for path in paths:
            reports.append(self.read(path))
            epoch_events = self.events(Path(path).parent / 'events.jsonl')
            events += epoch_events
            if path == report_name:
                current_events = epoch_events
        if goal and goal_folder:
            setup_events = [dict(event, phase='contract_setup') for event in self.events(goal_folder / 'setup-events.jsonl')]
            events += setup_events
            if not report_name:
                current_events = setup_events
        events.sort(key=lambda e: e.get('time', 0) if isinstance(e.get('time', 0), (int, float)) else 0)
        events = events[-4000:]
        pending, models = {}, {}
        for e in events:
            name = e.get('model')
            if e.get('event') in ('call_started', 'call_finished'):
                m = models.setdefault(name, {'model': name, 'calls': 0, 'seconds': 0, 'errors': 0,
                                             'input_tokens': None, 'output_tokens': None, 'peak_prompt_bytes': 0, 'durations': []})
            if e.get('event') == 'call_started':
                if e.get('call_id') is not None:
                    pending[e['call_id']] = e
                m['calls'] += 1
                m['peak_prompt_bytes'] = max(m['peak_prompt_bytes'], e.get('prompt_bytes', 0))
            if e.get('event') == 'call_finished':
                pending.pop(e.get('call_id'), None)
                m['last_status'] = e.get('status')
                m['last_error'] = e.get('error_kind')
                m['seconds'] += e.get('seconds', 0)
                m['errors'] += e.get('status') != 'returned'
                if e.get('status') == 'returned':
                    m['durations'].append(e.get('seconds', 0))
                    if isinstance(e.get('output_tokens'), int) and e.get('seconds', 0) > 0:
                        m['rated_tokens'] = m.get('rated_tokens', 0) + e['output_tokens']
                        m['rated_seconds'] = m.get('rated_seconds', 0) + e['seconds']
                for key in ('input_tokens', 'output_tokens'):
                    if isinstance(e.get(key), int):
                        m[key] = (m[key] or 0) + e[key]
        for event in events:
            if event.get('event') == 'model_protocol_error' and event.get('model') in models:
                model = models[event['model']]
                model['invalid_replies'] = model.get('invalid_replies', 0) + 1
        for m in models.values():
            durations = m.pop('durations')
            m['typical_seconds'] = statistics.median(durations) if durations else None
            m['seconds'] = round(m['seconds'], 2)
            m['completed_calls'] = len(durations)
            m['output_tokens_per_second'] = round(m.pop('rated_tokens', 0) / m.pop('rated_seconds'), 2) if m.get('rated_seconds') else None
        # Older versions emitted request/completion events without call IDs.
        if not models:
            legacy = {}
            for e in current_events:
                if e.get('event') == 'model_request':
                    legacy[(e.get('role'), e.get('model'))] = e
                elif e.get('event') in ('model', 'model_error', 'model_waiting'):
                    legacy.pop((e.get('role'), e.get('model')), None)
            pending = {str(k): v for k, v in legacy.items()}
        if models:
            current_ids = {e.get('call_id') for e in current_events if e.get('event') == 'call_started'}
            pending = {key: value for key, value in pending.items() if key in current_ids}
        live = self.running()
        now = time.time()
        calls = [{k: e.get(k) for k in ('role', 'model', 'phase', 'prompt_bytes', 'context_tokens', 'context_bytes', 'timeout')}
                 | {'elapsed_seconds': max(0, now - e.get('time', now)) if live else None, 'live': live} for e in pending.values()]
        for model in models.values():
            model['active_seconds'] = sum(c['elapsed_seconds'] for c in calls if c['model'] == model['model'] and c['live'])
            model['elapsed_seconds'] = model['seconds'] + model['active_seconds']
        pool = self.read(active.get('models_path') or goal.get('models_path', ''))
        definitions = field(pool, 'models', dict, {})
        # Live display labels may change without altering the frozen execution policy.
        labels = {}
        label_path = self.project / 'soso.json'
        try:
            if not label_path.is_symlink() and label_path.stat().st_size <= LIMIT:
                labels = field(json.loads(label_path.read_text()), 'models', dict, {})
        except (OSError, ValueError, AttributeError):
            pass
        for item in [*calls, *models.values()]:
            definition = field(definitions, item['model'], dict, {})
            display = field(labels, item['model'], dict, {})
            model_name = str(definition.get('model') or definition.get('kind') or item['model'] or 'unknown model')
            item['model_name'] = Path(model_name).name if model_name.startswith('/') else model_name
            item['display_name'] = str(display.get('display_name') or item['model_name'])
            item['host_label'] = str(display.get('host_label') or '')
            try:  # a hand-edited URL, such as one with port 99999, hides only this host
                endpoint = urlsplit(definition.get('url', ''))
                item['host'] = (endpoint.hostname or 'Local CLI') + (':' + str(endpoint.port) if endpoint.port else '')
            except (ValueError, TypeError, AttributeError):
                item['host'] = 'Unknown host'
            if item in calls:
                setup = item.get('phase') == 'contract_setup'
                item['purpose'] = ({'architect': 'Define the overall strategy', 'coordinator': 'Prepare acceptance criteria and checks',
                                    'planner': 'Prepare acceptance criteria and checks', 'reviewer': 'Review acceptance criteria'} if setup else
                                   {'architect': 'Revise the overall strategy', 'coordinator': 'Choose the next implementation step',
                                    'planner': 'Plan the next step', 'worker': 'Implement the task', 'reviewer': 'Review candidate changes'}).get(item.get('role'), 'Model request')
                usage = models.get(item['model'], {})
                item['typical_seconds'] = usage.get('typical_seconds')
                item['output_tokens_per_second'] = usage.get('output_tokens_per_second')
        rounds = []
        for r in reports:
            for row in field(r, 'rounds', list, []):
                if not isinstance(row, dict):
                    continue
                rounds.append({'round': row.get('round'), 'accepted': row.get('accepted'), 'outcome': row.get('outcome'),
                               'experiments': [{k: x.get(k) for k in ('task', 'evaluation', 'error', 'error_kind', 'review', 'candidate', 'commit')}
                                               for x in field(row, 'experiments', list, []) if isinstance(x, dict)]})
        contract = field(goal, 'contract', dict, {})
        if not contract and project:
            contract = self.read(Path(project) / '.soso-delivery/contract.json')
        milestones = [{'title': title, 'status': 'not_reported', 'evidence': ''}
                      for title in field(contract, 'milestones', list, []) if isinstance(title, str)]
        for row in rounds:
            for exp in row['experiments']:
                review = exp.get('review') if isinstance(exp.get('review'), dict) else {}
                marks = review.get('milestone_statuses')
                if not row.get('accepted') or exp.get('commit') != row['accepted'] or review.get('approve') is not True:
                    continue
                for mark in marks if isinstance(marks, list) else []:
                    if not isinstance(mark, dict):
                        continue
                    i, status = mark.get('index'), mark.get('status')
                    if type(i) is int and 0 <= i < len(milestones) and status in ('complete', 'in_progress', 'not_started'):
                        evidence = mark.get('evidence', '')
                        if status != 'complete' or (isinstance(evidence, str) and evidence.strip()):
                            milestones[i].update(status=status, evidence=str(evidence)[:1000])
        status = goal.get('status') or active.get('status', 'not_started')
        if status == 'complete':
            for m in milestones:
                m.update(status='complete', evidence='Final acceptance completed')
        done = sum(m['status'] == 'complete' for m in milestones)
        percent = round(100 * done / len(milestones)) if milestones and all(m['status'] != 'not_reported' for m in milestones) else None
        latest = events[-1] if events else {}
        plan = next((field(e, 'tasks', list, []) for e in reversed(events) if e.get('event') == 'plan'), [])
        if not plan:
            strategy = next((field(e, 'plan', dict, {}) for e in reversed(events) if e.get('event') == 'architecture'), {})
            plan = ['Proposed: ' + step for step in field(strategy, 'milestones', list, []) if isinstance(step, str)]
        schema = next((e.get('monitoring') for e in reversed(events)
                       if e.get('event') == 'plan' and isinstance(e.get('monitoring'), list) and e['monitoring']), [])
        values = {'best_score': report.get('best'), 'baseline_score': field(report, 'baseline', dict, {}).get('score'),
                  'accepted_count': sum(bool(r.get('accepted')) for r in rounds),
                  'plateau_count': journal.get('plateau', report.get('plateau'))}
        measured = field(report, 'baseline', dict, {}).get('metrics', {})
        for row in rounds:
            for exp in row['experiments']:
                if row.get('accepted') and exp.get('commit') == row['accepted']:
                    measured = field(exp, 'evaluation', dict, {}).get('metrics', {})
        if isinstance(measured, dict):
            values.update({'metric:' + k: v for k, v in measured.items() if isinstance(k, str)})
        insights = []
        for spec in schema[:6]:
            if not isinstance(spec, dict) or not isinstance(spec.get('label'), str):
                continue
            source = spec.get('source')
            if not isinstance(source, str) or (source not in values and not source.startswith('metric:')):
                continue
            value = values.get(source)
            insights.append({'label': spec['label'][:100], 'unit': str(spec.get('unit', ''))[:30],
                             'source': source, 'value': value if type(value) in (int, float) else None})
        tools = [e for e in events if e.get('event') == 'tool']
        timeline = [{k: e[k] for k in ('time', 'event', 'role', 'model', 'phase', 'task', 'tool', 'path', 'seconds', 'status', 'error', 'retry_at', 'round', 'best', 'prompt_bytes', 'input_tokens', 'output_tokens', 'error_kind', 'problem', 'attempt', 'retrying') if k in e} for e in events[-80:]]
        images = []
        worktree = field(active, 'worktree', str, '') or field(goal, 'worktree', str, '') or (str(Path(report_name).parent / 'result') if report_name else '')
        try:
            base = self.path(worktree)
            visited = 0
            for directory, folders, files in os.walk(base, followlinks=False):
                folders[:] = [n for n in folders if n not in ('.git', '.so-so', 'node_modules', '.venv') and not (Path(directory) / n).is_symlink()]
                visited += 1
                if visited > 150 or len(images) >= 24:
                    break
                for name in files:
                    path = Path(directory) / name
                    if path.suffix.lower() in IMAGES and not path.is_symlink():
                        path = self.path(path)
                        info = path.stat()
                        if stat.S_ISREG(info.st_mode) and info.st_size <= IMAGE_LIMIT:  # never a FIFO or device
                            images.append({'path': str(path.relative_to(self.root)), 'name': str(path.relative_to(base))})
        except (OSError, ValueError):
            pass
        return {'project': self.project.name, 'intent_title': redact(str(active.get('title') or field(goal, 'intent', str, '').split('\n')[0])), 'mode': active.get('mode'), 'status': status,
                'runner_active': live, 'phase': active.get('phase'), 'updated_at': active.get('updated_at'),
                'last_event_at': latest.get('time'), 'next_retry_at': goal.get('next_retry_at') or active.get('next_retry_at'),
                'reason': (journal.get('reason') or goal.get('reason') or report.get('reason')) if status != 'running' else None,
                'roadmap': roadmap(current_events, report, status, live, calls),
                'calls': calls, 'request_count': sum(e.get('event') == 'model_request' for e in events), 'models': list(models.values()), 'telemetry_available': bool(models),
                'usage_scope': 'Recent retained events: at most 21 epochs and 4,000 events; not lifetime totals.',
                'plan': plan, 'insights': insights, 'milestones': milestones, 'percent': percent, 'eta_seconds': None,
                'eta_reason': 'No reliable total-work estimate. Model call times are not a completion ETA.',
                'baseline': report.get('baseline'), 'best': report.get('best'),
                'plateau': journal.get('plateau', report.get('plateau')), 'rounds': rounds[-30:],
                'tools': dict(collections.Counter(e.get('tool', '?') for e in tools)),
                'timeline': timeline, 'images': images, 'report': report_name,
                'skills': ['writing-plans', 'test-driven-development', 'verification-before-completion', 'requesting-code-review']}


class Dashboard:
    IMAGE_SECONDS = 2  # asset checks reuse the image list of a snapshot at most this old
    REQUEST_SECONDS = 30  # an idle or stalled connection gives up its thread after this long

    def __init__(self, project, port=0, host=None, public_host=None):
        settings = {}
        config = Path(project) / 'soso.json'
        if config.is_file() and not config.is_symlink() and config.stat().st_size <= LIMIT:
            try:
                settings = json.loads(config.read_text())
            except ValueError as exc:
                raise ValueError(f'{config} is not valid JSON: {exc}') from exc
            settings = settings.get('dashboard', {}) if isinstance(settings, dict) else None
        if not isinstance(settings, dict):
            raise ValueError(f'{config}: "dashboard" must be an object')
        host = host or os.environ.get('SOSO_DASHBOARD_HOST') or settings.get('host', '127.0.0.1')
        public_host = public_host or os.environ.get('SOSO_DASHBOARD_PUBLIC_HOST') or settings.get('public_host')
        blank = public_host is not None and not (isinstance(public_host, str) and public_host.strip())
        if not (isinstance(host, str) and host) or blank:
            raise ValueError('dashboard.host and dashboard.public_host must be nonempty hostnames or IP addresses')
        self.monitor = Monitor(project)
        self.token = secrets.token_urlsafe(24)
        self.images = (float('-inf'), set())
        owner = self
        class Handler(BaseHTTPRequestHandler):
            timeout = owner.REQUEST_SECONDS

            def log_message(self, *args):
                pass

            def do_GET(self):
                prefix = '/' + owner.token + '/'
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                route = unquote(self.path[len(prefix):])
                try:
                    if route == 'api':
                        data = json.dumps(owner.snapshot()).encode()
                        mime = 'application/json'
                    elif route in ('', 'dashboard.js', 'dashboard.css'):
                        file = 'dashboard.html' if not route else route
                        data = (Path(__file__).parent / 'web' / file).read_bytes()
                        mime = {'': 'text/html; charset=utf-8', 'dashboard.js': 'text/javascript', 'dashboard.css': 'text/css'}[route]
                    elif route.startswith('asset/'):
                        relative = route[6:]
                        candidate = owner.monitor.root / relative
                        # The inventory may be up to IMAGE_SECONDS old: recheck that no link was swapped in since.
                        if relative not in owner.listed_images() or any(
                                p.is_symlink() for p in [candidate, *candidate.parents] if p.is_relative_to(owner.monitor.root)):
                            raise ValueError('unknown asset')
                        path = owner.monitor.path(owner.monitor.root / relative)
                        data = read_image(path)
                        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                    else:
                        raise ValueError('unknown route')
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
                    self.end_headers()
                    for start in range(0, len(data), CHUNK):  # the timeout bounds each send, not the whole body
                        self.wfile.write(data[start:start + CHUNK])
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    return  # the client closed or stalled the connection; nothing to log or answer
                except Exception as exc:
                    # Unknown routes and assets are 404; anything else is a monitor defect, logged once per reason.
                    if route != 'api' and isinstance(exc, (OSError, ValueError, TypeError, KeyError)):
                        self.send_error(404)
                        return
                    reason = type(exc).__name__ + ': ' + redact(str(exc))[:500]
                    if reason != getattr(owner, 'last_error', None):
                        owner.last_error = reason
                        print('Dashboard error: ' + reason, file=sys.stderr, flush=True)
                    self.send_error(500)
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        try:
            self.server = type('Server', (ThreadingHTTPServer,), {'address_family': family})((host, port), Handler)
        except OSError as exc:
            raise OSError(f'cannot listen on {host} port {port}: {exc.strerror or exc}') from exc
        self.server.daemon_threads = True
        # Decided from the address actually bound: '0' and '0x0' bind every interface as '0.0.0.0' does.
        if wildcard(self.server.server_address[0]) and (
                not public_host or unspecified(public_host)):
            self.server.server_close()
            raise ValueError('set dashboard.public_host to a reachable hostname or IP when binding all interfaces')
        public_host = public_host or host
        self.url = f'http://{f"[{public_host}]" if ":" in public_host else public_host}:{self.server.server_port}/{self.token}/'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def snapshot(self):
        snapshot = self.monitor.snapshot()
        self.images = (time.monotonic(), {x['path'] for x in snapshot['images']})
        return snapshot

    def listed_images(self):
        """Images the dashboard lists; polled asset requests need not rebuild a full snapshot each."""
        if time.monotonic() - self.images[0] > self.IMAGE_SECONDS:
            self.snapshot()
        return self.images[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@contextlib.contextmanager
def automatic(project):
    dashboard = None
    try:
        if os.environ.get('SOSO_DASHBOARD', '1') != '0':
            dashboard = Dashboard(project)
            dashboard.__enter__()
            print('Dashboard: ' + dashboard.url, flush=True)
    except Exception as exc:  # e.g. malformed soso.json dashboard settings; never stop the work
        print('Dashboard unavailable: ' + str(exc), flush=True)
    try:
        yield dashboard
    finally:
        if dashboard:
            dashboard.__exit__()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    try:
        dashboard = Dashboard(args.project, args.port)
    except (OSError, OverflowError, ValueError) as exc:  # e.g. bad settings, an address not on this host, a bad port
        print('dashboard: ' + str(exc), file=sys.stderr)
        return 1
    with dashboard:
        print('Dashboard: ' + dashboard.url, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
