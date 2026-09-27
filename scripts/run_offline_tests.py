#!/usr/bin/env python3
"""Run this workspace's small pytest-style test functions without pytest."""
from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
from pathlib import Path


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"offline_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, default=Path("tests"))
    args = parser.parse_args()
    # Executing ``python scripts/run_offline_tests.py`` puts scripts/ first on
    # sys.path.  Tests import sibling modules as ``scripts.<name>``, so add the
    # workspace root explicitly rather than requiring pytest's import shim.
    workspace = Path.cwd().resolve()
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    failures = 0
    executed = 0
    for path in sorted(args.tests.glob("test_*.py")):
        module = load_module(path)
        for name in sorted(dir(module)):
            fn = getattr(module, name)
            if not (name.startswith("test_") and callable(fn)):
                continue
            executed += 1
            try:
                fn()
            except Exception:
                failures += 1
                print(f"FAIL {path.name}::{name}")
                traceback.print_exc()
            else:
                print(f"PASS {path.name}::{name}")
    print(f"executed={executed} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
