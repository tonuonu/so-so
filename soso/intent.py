"""Turn user Markdown into an internal model pool; no user JSON is required."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import shlex
import time

from .delivery import configure_models, load_model_pool, ollama_models
from .models import Budget, BudgetExceeded, Router, ModelUnavailable


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
        try:
            pool = load_model_pool(cache)
            Router(pool, Budget(1, 1), lambda *a, **k: None)
            return pool
        except (ValueError, KeyError, TypeError):
            cache.rename(cache.with_suffix('.invalid'))
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
    # Bootstrap policy interpretation uses a mentioned adapter when possible;
    # it must not send intent to an unrelated installed service as a fallback.
    mentioned = [n for n, m in models.items() if
                 (m.get('model') and m['model'] in text) or
                 re.search(r'\b' + re.escape(n) + r'\b', text, re.I)]
    candidates = mentioned or list(models)
    preferred = [n for n in ('kimi', 'codex', 'claude') if n in candidates]
    preferred += [n for n in candidates if n not in preferred]
    compiler_config = {'delivery': True, 'models': models, 'routes': {'worker': preferred},
                       '_router_state': str(folder / 'compiler-cooldowns.json')}
    def log(event, **data):
        with (folder / 'setup-events.jsonl').open('a') as stream:
            stream.write(json.dumps({'time': time.time(), 'event': event, **data}) + '\n')
    router = Router(compiler_config, Budget(6, 600), log)
    prompt = """Interpret user Markdown as a model routing policy. Return ONLY JSON with:
"aliases": {"friendly-name":"exact-inventory-key", ...},
"routes": {"planner":["friendly-name"],"worker":["friendly-name"],"reviewer":["friendly-name"]},
optional "settings": {"friendly-name":{"context_tokens":8192,"max_tokens":128,"options":{"num_gpu":0}}},
optional "routing": {"rules":[...],"selector":{...},"default":["friendly-name"]}.
Select only the requested available models. Do NOT reproduce endpoint/command definitions;
the host copies them from inventory. Every alias must refer to an exact inventory key.
Rules are first-match with optional role, contains:[strings] (any), not_contains:[strings]
(none), regex:string and required models:[aliases]. The user's default is authoritative.
If asked for a small classifier, selector is {"model":"small-alias","prompt":"classify instructions",
"routes":{"code":["qwen-alias","kimi-alias"],"hard":["kimi-alias"],"other":["kimi-alias"]}}.
Classifier returns {"route":"code"}. Send planner/reviewer directly to their requested model
using role-only rules, and classify worker jobs. When worker classification is requested,
DO NOT add worker keyword rules or a worker catch-all: those bypass the classifier. options.num_gpu=0 means CPU inference. For an Ollama classifier that supports thinking, settings think=false avoids spending its small output budget on reasoning.
Respect user preferences; no downloads, no commands, no task implementation here.
INVENTORY:
"""
    feedback = ''
    for attempt in range(3):
        try:
            result = router.ask('configuration', prompt + json.dumps(models) + '\nUSER MARKDOWN:\n' + text + feedback,
                                cwd, attempt=attempt, task='Interpret model pool and routing preferences')
        except BudgetExceeded:
            raise ModelUnavailable(reason='model setup epoch exhausted; retry after a pause') from None
        if isinstance(result.get('aliases'), dict):
            aliases = result['aliases']
            settings = result.get('settings', {})
            try:
                if not isinstance(settings, dict):
                    raise ValueError('settings must be an object')
                chosen = {}
                for alias, source in aliases.items():
                    if source not in models:
                        raise ValueError('alias must name an exact inventory key')
                    tuning = settings.get(alias, {})
                    allowed = {'context_tokens', 'context_bytes', 'max_tokens', 'timeout', 'parallel', 'options', 'think'}
                    if not isinstance(tuning, dict) or set(tuning) - allowed:
                        raise ValueError('unsupported model tuning setting')
                    chosen[alias] = {**models[source], **tuning}
                result = {**result, 'models': chosen}
            except (ValueError, KeyError, TypeError) as exc:
                log('policy_invalid', attempt=attempt, error=str(exc)[:300])
                feedback = '\nREPAIR POLICY: ' + str(exc)[:300]
                continue
        temporary = cache.with_suffix('.pending')
        temporary.write_text(json.dumps(result))
        try:
            pool = load_model_pool(temporary)
            adapter_keys = ('kind', 'url', 'model', 'command', 'executable', 'args', 'api_key_env')
            for model in pool['models'].values():
                for field in ('context_tokens', 'context_bytes', 'max_tokens', 'parallel', 'timeout'):
                    if field in model and (type(model[field]) is not int or model[field] < 1):
                        raise ValueError(field + ' must be a positive integer')
                if 'think' in model and not isinstance(model['think'], (bool, str)):
                    raise ValueError('think must be a boolean or provider thinking level')
                if 'options' in model and not isinstance(model['options'], dict):
                    raise ValueError('model options must be an object')
                identity = {k: model[k] for k in adapter_keys if k in model}
                verified = any(identity == {k: item[k] for k in adapter_keys if k in item}
                               for item in models.values())
                if not verified:
                    # Explicit custom adapters may be described in user Markdown.
                    command = model.get('command', [])
                    explicit_command = (model.get('kind') == 'command' and isinstance(command, list)
                        and command and shlex.join(command) in text and shutil.which(command[0]))
                    explicit_api = (model.get('kind') in ('ollama', 'openai')
                        and isinstance(model.get('url'), str) and model['url'] in text
                        and isinstance(model.get('model'), str) and model['model'] in text
                        and not any(k in model for k in ('command', 'args', 'executable')))
                    if not (explicit_command or explicit_api):
                        raise ValueError('adapter must preserve discovered connection details or be explicitly provided in Markdown')
            Router(pool, Budget(1, 1), lambda *a, **k: None)
        except (ValueError, KeyError, TypeError) as exc:
            log('policy_invalid', attempt=attempt, error=str(exc)[:300])
            feedback = '\nREPAIR POLICY: ' + str(exc)[:300]
            temporary.unlink(missing_ok=True)
            continue
        temporary.replace(cache)
        return pool
    raise ModelUnavailable(reason='model policy translation needs another attempt')
