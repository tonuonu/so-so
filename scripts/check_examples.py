"""Verify fixed baseline evaluators without model access (CI smoke test)."""
import json
from pathlib import Path
import subprocess
import sys
root = Path(__file__).resolve().parents[1]
for name in ('slugger', 'c-stats', 'csv-reader'):
    result = subprocess.run([sys.executable, 'evaluate.py'], cwd=root/'examples'/name,
                            check=True, capture_output=True, text=True, timeout=30)
    score = json.loads(result.stdout.strip().splitlines()[-1])
    assert 0 <= score['score'] < score['total'], (name, score)
    print(name, score)
