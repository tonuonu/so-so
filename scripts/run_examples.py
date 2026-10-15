#!/usr/bin/env python3
"""Run REAL inference on independent copies of all three example projects."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from soso.engine import Run, load_config
from soso.process import git


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['codex', 'ollama', 'mixed'], default='codex')
    parser.add_argument('--model', help='Ollama model name; required for ollama/mixed')
    parser.add_argument('--output', required=True, type=Path, help='new directory for isolated example repos')
    parser.add_argument('--jobs', type=int, default=1)
    args = parser.parse_args()
    if args.backend != 'codex' and not args.model:
        parser.error('--model required for Ollama')
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1] / 'examples'

    def trial(name):
        repo = args.output.resolve() / name
        shutil.copytree(source / name, repo, ignore=shutil.ignore_patterns('__pycache__'))
        config_path = repo / 'soso.json'
        config = json.loads(config_path.read_text())
        if args.backend == 'codex':
            config['routes'] = {role:['codex'] for role in ('planner','worker','reviewer')}
        elif args.backend == 'ollama':
            config['routes'] = {role:['local'] for role in ('planner','worker','reviewer')}
        if args.model:
            config['models']['local']['model'] = args.model
        config_path.write_text(json.dumps(config, indent=2)+'\n')
        git(repo,'init','-b','main')
        git(repo,'config','user.name','Example Maintainer')
        git(repo,'config','user.email','example@example.invalid')
        git(repo,'add','--all')
        git(repo,'commit','-m','Example baseline')
        base = git(repo,'rev-parse','HEAD')
        runner = Run(repo,load_config(config_path),(repo/'INTENT.md').read_text())
        report = runner.run()
        preserved = git(repo,'rev-parse','HEAD') == base and not git(repo,'status','--porcelain')
        summary = {'project':name,'backend':args.backend,'baseline':report['baseline'].get('score'),
                   'best':report.get('best'),'status':report['status'],'calls':report.get('calls'),
                   'rounds':len(report['rounds']),'source_preserved':preserved,
                   'report':str(runner.report_path),'branch':report['branch']}
        (repo/'result-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        return summary

    names = ['slugger','c-stats','csv-reader']
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(trial,names))
    (args.output/'summary.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
    return 0 if all(r['source_preserved'] and r['best'] > r['baseline'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
