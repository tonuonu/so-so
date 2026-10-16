"""Command line entry points."""
import argparse
import json
from pathlib import Path
import sys
from .engine import Run, load_config, resources
from .models import Budget, Router


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('run', 'doctor', 'probe'):
        from .delivery import start_goal
        parser = argparse.ArgumentParser(description="Persistently deliver a goal in an isolated Git worktree")
        parser.add_argument('goal', nargs='?', help='goal text; omitted or read INTENT.md reads INTENT.md')
        parser.add_argument('--repo', default='.')
        parser.add_argument('--backend', default='auto', choices=('auto', 'ollama', 'codex', 'claude'))
        parser.add_argument('--model')
        parser.add_argument('--models', help='model pool JSON; defaults to SOSO_MODELS_FILE')
        parser.add_argument('--once', action='store_true', help='run one bounded epoch without waiting')
        parser.add_argument('--resume-goal', metavar='GOAL_ID')
        parser.add_argument('--no-exec', action='store_true', help='disable model-requested host commands (trusted local execution is enabled by default)')
        args = parser.parse_args(argv)
        try:
            result = start_goal(Path(args.repo), args.goal, backend=args.backend, model=args.model,
                                once=args.once, resume_goal=args.resume_goal, allow_exec=not args.no_exec, models_path=args.models)
            print(json.dumps(result, indent=2))
            return 0 if result['status'] == 'complete' else 2
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            print('soso: ' + str(exc), file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description="Run measured, parallel coding experiments from INTENT.md")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="create a result branch without changing your checkout")
    run.add_argument("--repo", default=".")
    run.add_argument("--config", default="soso.json")
    run.add_argument("--intent", default="INTENT.md")
    run.add_argument("--allow-exec", action="store_true", help="allow model-requested commands; use an external sandbox")
    run.add_argument("--resume", metavar="RUN_ID", help="resume checkpoint within original total budgets")
    doctor = sub.add_parser("doctor", help="show resource inventory and validate config")
    doctor.add_argument("--config")
    probe = sub.add_parser("probe", help="perform one real inference through a configured route")
    probe.add_argument("--config", default="soso.json")
    probe.add_argument("--role", default="worker")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(resources(), indent=2))
            if args.config:
                load_config(args.config)
                print("Configuration valid")
            return 0
        if args.command == "probe":
            config = load_config(args.config)
            router = Router(config, Budget(5, 240), lambda *a, **kw: None)
            examples = {'worker': {'tool': 'done'}, 'reviewer': {'approve': False},
                        'planner': {'experiments': [{'task': 'Connectivity probe only'}]},
                        'coordinator': {'experiments': [{'task': 'Connectivity probe only'}]},
                        'architect': {'milestones': ['Check connectivity'], 'constraints': ['No changes'],
                                      'acceptance': ['Valid response']}}
            prompt = 'Return exactly ' + json.dumps(examples.get(args.role, {'ok': True})) + '. Do not execute tools.'
            print(json.dumps(router.ask(args.role, prompt, Path.cwd())))
            return 0
        root = Path(args.repo).resolve()
        config = load_config(root / args.config)
        intent = (root / args.intent).read_text()
        for name in (args.config, args.intent):
            path = (root / name).resolve()
            if path.is_relative_to(root):
                config["protected"].append(str(path.relative_to(root)))
        if not intent.strip() or len(intent.encode()) > 8000:
            raise ValueError("intent must contain 1..8000 bytes; move detail into source files")
        runner = Run(root, config, intent, args.allow_exec, args.resume)
        report = runner.run()
        print(json.dumps({"status": report["status"], "branch": report.get("branch"),
                          "baseline": report.get("baseline", {}).get("score"),
                          "best": report.get("best"), "report": str(runner.report_path)}, indent=2))
        return 1 if report["status"] in ("error", "invalid_baseline") else 0
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print("soso: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
