"""AST-node count as a deliberately limited proxy for implementation bloat."""
import ast
import json
from pathlib import Path
# Subpackages count too, so moving code cannot lower the score; vendored skills do not.
count = sum(sum(1 for _ in ast.walk(ast.parse(path.read_text())))
            for path in Path('soso').rglob('*.py') if not path.is_relative_to('soso/skillpacks'))
print(json.dumps({'score': count, 'metric': 'runtime_python_ast_nodes'}))
