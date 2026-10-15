"""AST-node count as a deliberately limited proxy for implementation bloat."""
import ast
import json
from pathlib import Path
count = sum(sum(1 for _ in ast.walk(ast.parse(path.read_text())))
            for path in Path('soso').glob('*.py'))
print(json.dumps({'score': count, 'metric': 'runtime_python_ast_nodes'}))
