Simplify this runner without changing its public behavior or reducing safety checks.
Reduce unnecessary AST complexity in soso/*.py while retaining all tools, adapters,
routing, concurrency, context bounds, evaluation protection and recovery behavior.
Prefer one small defensible refactor to a rewrite. Inspect relevant source and
use check to verify. Existing tests and evaluator are immutable. Do not compress
code into unreadable expressions, remove documentation or introduce dependencies.
