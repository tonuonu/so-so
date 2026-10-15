"""Pinned Superpowers skill excerpts and bounded, on-demand full text."""
import hashlib
import json
from pathlib import Path

PACK = Path(__file__).parent / 'skillpacks' / 'superpowers'
ROLE_SKILLS = {
    'planner': ['writing-plans'],
    'worker': ['test-driven-development', 'verification-before-completion'],
    'reviewer': ['requesting-code-review', 'verification-before-completion'],
}


def catalog():
    return sorted(p.parent.name for p in PACK.glob('*/SKILL.md'))


def read_skill(name, start=1, lines=60):
    if name not in catalog():
        raise ValueError('unknown skill; choose from ' + ', '.join(catalog()))
    manifest = json.loads((PACK / 'manifest.json').read_text())
    path = PACK / name / 'SKILL.md'
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['files'][f'{name}/SKILL.md']:
        raise ValueError('skill content differs from pinned manifest')
    content = data.decode().splitlines()
    start = max(1, int(start))
    count = min(80, max(1, int(lines)))
    return {'skill': name, 'revision': manifest['revision'], 'start': start,
            'total_lines': len(content), 'content': '\n'.join(content[start - 1:start - 1 + count])[:7000]}


def prelude(role):
    text = ('SUPERPOWERS ADAPTATION: The supplied INTENT is the authorized scope. '
            'The runner owns worktrees, dispatch, commits, and acceptance. '
            'Use the JSON protocol, not interactive handoffs or nested agents. '
            'Existing protected tests are immutable; reproduce failures using check, and '
            'add focused regression tests outside protected paths when appropriate. '
            'Never claim a test ran unless a tool result proves it. '
            'For broader or ambiguous requirements, report the limitation rather than inventing scope.\n')
    for name in ROLE_SKILLS.get(role, []):
        page = read_skill(name, 1, 30)['content']
        # Include the opening overview; the rest is available through the skill tool.
        body = page.split('---', 2)[-1].strip()
        overview = body.split('\n## ', 2)
        excerpt = '\n## '.join(overview[:2])
        text += '\nUPSTREAM SKILL ' + name + ':\n' + excerpt + '\n'
    return text + '\nAvailable skill tool names: ' + ', '.join(catalog()) + '\n'
