"""Human-readable intent history alongside internal checkpoint data."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import quote


def title(intent):
    return next((line.strip().lstrip('#').strip()[:100] for line in intent.splitlines()
                 if line.strip().lstrip('#').strip()), 'Untitled intent')


def folder_name(intent, fingerprint):
    slug = re.sub(r'[^\w-]+', '-', title(intent).lower(), flags=re.UNICODE).strip('-_')[:60]
    return (slug or 'intent') + '--' + fingerprint[:12]


def _write(path, text):
    temporary = path.with_name(path.name + '.tmp')
    # Replacing the directory entry avoids following a previous symlink.
    temporary.unlink(missing_ok=True)
    temporary.write_text(text)
    temporary.replace(path)


def update_history(managed, folder, entry):
    updated = datetime.fromtimestamp(entry['updated_at'], timezone.utc).isoformat(timespec='seconds')
    lines = ['# ' + entry.get('title', 'Saved intent'), '',
             'Status: ' + entry['status'], 'Updated (UTC): ' + updated, '',
             '[Original assignment](INTENT.md)', '',
             'This is an isolated source snapshot. Changes to the intent or source create a new snapshot.', '']
    for label, key in [('Result', 'worktree'), ('Evidence', 'report')]:
        if entry.get(key):
            lines += [label + ': `' + str(entry[key]) + '`', '']
    if entry.get('reason'):
        lines += ['Stop reason: ' + str(entry['reason']), '']
    _write(folder / 'README.md', '\n'.join(lines))
    records = []
    for path in (managed / 'projects').glob('*/entry.json'):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            item = json.loads(path.read_text())
            records.append((item.get('updated_at', 0), path.parent, item))
        except (OSError, ValueError):
            continue
    lines = ['# Intent history', '',
             'Each folder is a snapshot of an assignment and its source files. The short suffix distinguishes revisions.',
             'Open its README for status and result paths, or INTENT.md for the exact assignment.', '',
             'This directory also holds checkpoints, logs, model settings and result worktrees.',
             'Deleting it removes resume history and any results stored here; copy wanted results first.', '']
    for timestamp, path, item in sorted(records, key=lambda record: record[0], reverse=True):
        label = item.get('title', path.name).replace('\n', ' ').replace('[', '\\[').replace(']', '\\]')
        when = datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='seconds')
        lines.append(f'- [{label}]({quote(str(path.relative_to(managed)))}/README.md) — {item.get("status", "unknown")} — {when}')
    _write(managed / 'README.md', '\n'.join(lines) + '\n')
