"""Turn user Markdown into an internal model pool; no user JSON is required."""
import hashlib
import json
from pathlib import Path
import re
import shutil

from .delivery import configure_models, load_model_pool, ollama_models
from .models import Budget, Router, ModelUnavailable


def resolve_pool(text, cwd):
    """Discover adapters and interpret model/routing preferences once per intent.

    Markdown is interpreted by available inference, then validated by the same
    pool/policy validators as runtime. Unknown destinations are never guessed.
    """
    cwd = Path(cwd)
    folder = cwd / '.so-so' / 'pools'
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(text.encode()).hexdigest()
    cache = folder / (key + '.json')
    if cache.exists():
        pool = load_model_pool(cache)
        Router(pool, Budget(1, 1), lambda *a, **k: None)
        return pool
    try:
        discovered = configure_models({})
    except ModelUnavailable:
        discovered = {'models': {}, 'routes': {}}
    models = dict(discovered['models'])
    if shutil.which('kimi'):
        models['kimi'] = {'kind': 'kimi', 'context_bytes': 60000, 'timeout': 180}
    # Inventory explicitly supplied Ollama endpoints, without downloading weights.
    for url in dict.fromkeys(re.findall(r'https?://[^\s`<>"\)]+', text)):
        if ':1143' not in url and 'ollama' not in url.lower():
            continue
        for name in ollama_models(url):
            alias = 'remote-' + hashlib.sha256((url + name).encode()).hexdigest()[:10]
            models[alias] = {'kind': 'ollama', 'url': url, 'model': name,
                             'context_tokens': 32768, 'context_bytes': 60000, 'timeout': 180}
    if not models:
        raise ModelUnavailable(reason='no inference endpoint available yet')
    # Use explicitly named installed models first to interpret their own settings.
    preferred = [n for n, m in models.items() if m.get('model') and m['model'] in text]
    preferred += [n for n in ('kimi', 'codex', 'claude') if n in models and n not in preferred]
    preferred += [n for n in models if n not in preferred]
    compiler_config = {'models': models, 'routes': {'worker': preferred},
                       '_router_state': str(folder / 'compiler-cooldowns.json')}
    router = Router(compiler_config, Budget(6, 600), lambda *a, **k: None)
    prompt = '''Translate user intent and model preferences into an internal inference routing policy.
Return only JSON with models, routes, and optional routing. No tools or shell actions.
Models is a dictionary of aliases to adapter objects. Inventory below contains verified
available adapters. Preserve their exact endpoint/model/executable details; rename aliases
for clarity. Select the models requested by the user. If no preferences, choose sensible
available models, preferring local workers and capable planning/review. Never download.
User may specify additional real endpoints/commands explicitly; don't invent them.
Kinds: ollama {url,model,options?} (options.num_gpu=0 requests CPU inference for a small classifier), openai {url,model,api_key_env?}, kimi {}, codex {},
command {command:[argv...]}. Credentials are ENVIRONMENT VARIABLE NAMES only.
Routes maps planner,worker,reviewer to ordered nonempty lists of model aliases.
Routing rules are ordered first-match dictionaries with optional role, contains:[strings]
(any substring), not_contains:[strings] (none), regex:string, and required models:[aliases].
Routing default:[aliases] is authoritative when no rule matches. A cheap classifier is
optional: selector:{model:alias,prompt:string,routes:{label:[aliases]}}. Classifier output
selects a named route. Use rules/default when the user asks for them; do not replace their
policy with your opinion. Design can go to a design specialist, routine coding to local
Qwen, and unmatched tasks to the user's default. Do not put task implementation into policy.
Do not invent a restriction to one backend. Every destination must exist in models.
INVENTORY:\n'''
    feedback = ''
    for _ in range(3):
        result = router.ask('configuration', prompt + json.dumps(models) + '\nUSER MARKDOWN:\n' + text + feedback,
                            cwd, task='Interpret model pool and routing preferences')
        temporary = cache.with_suffix('.pending')
        temporary.write_text(json.dumps(result))
        try:
            pool = load_model_pool(temporary)
            Router(pool, Budget(1, 1), lambda *a, **k: None)
        except (ValueError, KeyError, TypeError) as exc:
            feedback = '\nREPAIR POLICY: ' + str(exc)[:300]
            temporary.unlink(missing_ok=True)
            continue
        temporary.replace(cache)
        return pool
    raise ModelUnavailable(reason='model policy translation needs another attempt')
