"""Human-readable intent history alongside internal checkpoint data."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import quote

from .models import redact


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
    # Titles are redacted for display only: entry.json and the snapshot's INTENT.md keep the text as written.
    lines = ['# ' + redact(str(entry.get('title', 'Saved intent'))), '',
             'Status: ' + entry['status'], 'Updated (UTC): ' + updated, '',
             '[Original assignment](INTENT.md)', '',
             'This is an isolated source snapshot. Changes to the intent or source create a new snapshot.', '']
    if entry.get('resume_id'):
        lines += ['Resume: `./so-so resume ' + entry['resume_id'] + '`', '']
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
            timestamp = item.get('updated_at', 0)
            when = datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='seconds')
            records.append((timestamp, path.parent, item, when))
        except (OSError, ValueError, AttributeError, TypeError, OverflowError):
            continue  # One malformed snapshot entry must not block every launch.
    lines = ['# Intent history', '',
             'Each folder is a snapshot of an assignment and its source files. The short suffix distinguishes revisions.',
             'Open its README for status and result paths, or INTENT.md for the exact assignment.', '',
             'This directory also holds checkpoints, logs, model settings and result worktrees.',
             'Deleting it removes resume history and any results stored here; copy wanted results first.', '']
    for timestamp, path, item, when in sorted(records, key=lambda record: record[0], reverse=True):
        label = redact(str(item.get('title', path.name))).replace('\n', ' ').replace('[', '\\[').replace(']', '\\]')
        lines.append(f'- [{label}]({quote(str(path.relative_to(managed)))}/README.md) — {item.get("status", "unknown")} — {when}')
    _write(managed / 'README.md', '\n'.join(lines) + '\n')
