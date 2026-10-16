"""Small, evidence-based terminal summaries; no extra inference calls."""
import json
from pathlib import Path
from .models import redact


def attempt_outcome(attempt):
    if attempt.get('error'):
        return 'execution failed: ' + attempt['error']
    if attempt.get('unavailable'):
        return 'waiting: ' + attempt['unavailable'].get('reason', 'provider unavailable')
    evaluation = attempt.get('evaluation') or {}
    if evaluation.get('reason') == 'no changes':
        return 'no code changes produced'
    if evaluation.get('valid') is False:
        return 'validation failed: ' + str(evaluation.get('reason', 'checks did not pass'))
    if attempt.get('review', {}).get('approve') is False:
        return 'review rejected: ' + str(attempt['review'].get('reason', 'review did not approve'))
    if evaluation.get('valid') is True:
        return 'evaluated; no accepted improvement'
    return 'evaluation not reached'


def summary_lines(result):
    """Read the final epoch only; earlier epochs stay in the durable goal journal."""
    name = result.get('report')
    if not name:
        return []
    try:
        path = Path(name)
        if path.stat().st_size > 2_000_000:
            return ['Detailed report is large; see the evidence file below.']
        report = json.loads(path.read_text())
        if 'rounds' not in report:
            epoch = report.get('active_epoch') or next((e.get('report') for e in reversed(report.get('epochs', [])) if e.get('report')), None)
            if epoch:
                path = Path(epoch)
                if path.stat().st_size > 2_000_000:
                    return ['Detailed epoch report is large; see the evidence file below.']
                report = json.loads(path.read_text())
        rounds = report.get('rounds', [])
    except (OSError, ValueError):
        return ['Detailed report unavailable; see the evidence location below.']
    attempts = [(row, attempt) for row in rounds for attempt in row.get('experiments', [])]
    accepted = sum(bool(row.get('accepted')) for row in rounds)
    errors = sum(bool(a.get('error')) for _, a in attempts)
    evaluated = sum(a.get('evaluation', {}).get('valid') is True for _, a in attempts)
    lines = [f'Latest epoch: {len(attempts)} attempts; {evaluated} valid evaluations; {accepted} accepted changes; {errors} execution failures.']
    baseline = (report.get('baseline') or {}).get('score')
    if baseline is not None:
        lines.append(f'Score: {baseline} -> {report.get("best", baseline)}.')
    for row, attempt in attempts[-8:]:
        task = attempt.get('task', 'Unspecified task').split('EXPERIMENT:\n')[-1]
        task = ' '.join(task.split())[:160]
        outcome = 'accepted' if row.get('accepted') and row['accepted'] == attempt.get('commit') else attempt_outcome(attempt)
        lines.append(f'  Round {row.get("round", "?")}: {task}\n    {outcome}')
    if result.get('status') == 'optimized':
        lines.append('Stopped after completed attempts made no accepted improvement; this does not prove the goal cannot improve further.')
    elif errors:
        lines.append('Next action: address the reported model/protocol or tool failure, then run ./so-so to retry. Accepted results are preserved.')
    return [redact(line) for line in lines]
