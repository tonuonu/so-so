# Development rules

Use Python 3.11+ standard library at runtime. Keep the runner small and inspectable.
Run `python3 -m unittest discover -s tests -v` before claiming a change works.
Run `python3 scripts/check_examples.py` for example evaluator changes.

Preserve source-checkout isolation, protected evaluation policy, bounded I/O,
explicit model routes, strict improvement acceptance, and durable run evidence.
Do not remove functionality or weaken tests to improve a size metric. Do not
modify vendored Superpowers sources or their license without updating the pin and
manifest deliberately. No generated attribution trailers or branding in user code.
