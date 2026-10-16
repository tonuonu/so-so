"""Durable strategic guidance; routine planning remains with the coordinator.

This artifact is guidance, never an acceptance policy or authority to execute tools.
"""
import hashlib
import json
from pathlib import Path


def _plan(value):
    if not isinstance(value, dict):
        raise ValueError('architect must return an object')
    result = {}
    for key in ('milestones', 'constraints', 'acceptance'):
        items = value.get(key)
        if (not isinstance(items, list) or len(items) > 12 or
                any(not isinstance(item, str) or not item.strip() or len(item) > 800 for item in items)):
            raise ValueError('architect ' + key + ' must contain at most twelve short strings')
        result[key] = items
    if not result['milestones'] or not result['acceptance']:
        raise ValueError('architect must provide milestones and acceptance guidance')
    if len(json.dumps(result).encode()) > 12000:
        raise ValueError('architecture guidance exceeds 12000 bytes')
    return result


def planning_reply(router, config, intent, storage, cwd, prompt, feedback=()):
    """Use one strategic plan per intent/policy, then ask the fast coordinator.

    A coordinator can explicitly request one strategic revision after at least two
    evaluated unsuccessful rounds. Transport, schema and worker failures do not
    qualify. The revision count persists across restarts and optimization epochs.
    """
    if not config.get('routes', {}).get('architect'):
        return router.ask('planner', prompt, cwd, task=intent)
    if not config['routes'].get('coordinator'):
        raise ValueError('architect routing requires an explicit coordinator route')
    binding = hashlib.sha256(json.dumps({'intent': intent, 'models': config['models'],
        'routes': config['routes'], 'routing': config.get('routing', {})}, sort_keys=True).encode()).hexdigest()
    folder = Path(storage) / 'architecture'
    if folder.is_symlink():
        raise ValueError('architecture storage must not be a symlink')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (binding + '.json')
    if path.is_symlink():
        raise ValueError('architecture artifact must not be a symlink')

    def save(record):
        temporary = path.with_suffix('.tmp')
        if temporary.is_symlink():
            raise ValueError('architecture temporary file must not be a symlink')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(path)

    def architect(previous=None, reason=''):
        strategic = ('Act only as system architect. Delegate implementation, file inspection and tests to '
            'faster agents. Return compact JSON {"milestones":["..."],"constraints":["..."],'
            '"acceptance":["..."]}. Use at most six short milestones, no code and no essay. '
            'These are strategic suggestions: existing instructions and frozen acceptance checks remain binding.\n'
            'INTENT:\n' + intent)
        if previous:
            strategic += '\nPREVIOUS STRATEGY:\n' + json.dumps(previous) + '\nESCALATION:\n' + reason[:2000]
        result = _plan(router.ask('architect', strategic, cwd, task=intent))
        router.log('architecture', plan=result, revised=previous is not None)
        return result

    if path.exists():
        if path.stat().st_size > 16000:
            raise ValueError('architecture artifact exceeds size limit')
        record = json.loads(path.read_text())
        if record.get('binding') != binding or type(record.get('revisions')) is not int or record['revisions'] not in (0, 1):
            raise ValueError('architecture artifact binding or revision is invalid')
        record['plan'] = _plan(record.get('plan'))
    else:
        record = {'binding': binding, 'plan': architect(), 'revisions': 0}
        save(record)
    before = path.read_bytes()
    context = ('\nSTRATEGIC GUIDANCE (not permission to change frozen checks):\n' + json.dumps(record['plan']) +
        '\nYou are the coordinator. Fulfill the request above using exactly its requested JSON structure. '
        'During acceptance setup, write the requested evaluator code; delegate application implementation '
        'to workers. Do not substitute the architecture schema for the requested response. If evaluated approaches show a '
        'strategic contradiction, optionally include "escalation":{"reason":"specific contradiction"}. '
        'Malformed output or provider failures are not strategic contradictions.')
    reply = router.ask('coordinator', prompt + context, cwd, task=intent)
    if path.is_symlink() or path.read_bytes() != before:
        raise ValueError('coordinator modified architecture guidance')
    rounds = []
    for item in feedback:
        if isinstance(item, dict):
            rounds.extend(item.get('feedback', []) if 'feedback' in item else [item])
    failed = sum(1 for item in rounds if isinstance(item, dict) and not item.get('accepted') and
        any(isinstance(e, dict) and not e.get('error') and isinstance(e.get('evaluation'), dict)
            and e['evaluation'].get('valid') is True for e in item.get('experiments', [])))
    escalation = reply.get('escalation')
    reason = escalation.get('reason') if isinstance(escalation, dict) else None
    if isinstance(reason, str) and reason.strip() and failed >= 2 and record['revisions'] == 0:
        record.update(plan=architect(record['plan'], reason), revisions=1)
        save(record)
        before = path.read_bytes()
        reply = router.ask('coordinator', prompt + '\nREVISED STRATEGIC GUIDANCE:\n' + json.dumps(record['plan']), cwd, task=intent)
        if path.is_symlink() or path.read_bytes() != before:
            raise ValueError('coordinator modified architecture guidance')
    return reply
