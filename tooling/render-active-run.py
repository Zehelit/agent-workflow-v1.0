"""Deterministic, read-only N06 context renderer. Never refreshes authority."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[1] / 'skills/agent-workflow-v1-0/scripts/workflow.py'
spec = importlib.util.spec_from_file_location('active_context_workflow', SOURCE)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


def render(project, codex_home, references, as_of):
    engine = workflow.Workflow(project, codex_home, lambda: as_of)
    engine.internal('state.json')
    return engine.render_context(workflow.read_json(engine.path), references, as_of)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--codex-home', required=True)
    parser.add_argument('--references', required=True, help='strict JSON list of pinned reference objects')
    parser.add_argument('--as-of', required=True, type=float, help='actual observation time, never source creation time')
    args = parser.parse_args(argv)
    try:
        result = render(args.project, args.codex_home, workflow.read_json(Path(args.references)), args.as_of)
    except (workflow.Error, OSError) as exc:
        print(json.dumps(dict(ok=False, error=str(exc), code=getattr(exc, 'code', 'CONTEXT_IO')), ensure_ascii=False))
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if not result['blockers'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
