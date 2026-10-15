import json
import math
import subprocess
import tempfile
from pathlib import Path
cases = [(['1', '2'], 1.5), (['-1', '2'], .5), (['1.5', '2.5'], 2),
         (['1e2', '3e2'], 200), (['0'], 0), (['-5', '-3'], -4),
         ([], None), (['oops'], None), (['2junk'], None), (['nan'], None),
         (['inf'], None), (['1e20', '3e20'], 2e20)]
passed = 0
with tempfile.TemporaryDirectory() as folder:
    binary = str(Path(folder) / 'stats')
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', 'stats.c', '-lm', '-o', binary], check=True)
    for argv, expected in cases:
        result = subprocess.run([binary, *argv], text=True, capture_output=True, timeout=2)
        if expected is None:
            passed += result.returncode != 0
        elif result.returncode == 0:
            try:
                passed += math.isclose(float(result.stdout), expected, abs_tol=1e-6, rel_tol=1e-10)
            except ValueError:
                pass
print(json.dumps({'score': passed, 'total': len(cases)}))
