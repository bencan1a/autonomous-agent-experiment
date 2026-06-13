"""Aggregate runner for the plain-runner test suite (no pytest).

Discovers every ``tests/test_*.py`` and runs each as its own subprocess with the
*same* interpreter that launched this runner (so ``./venv/bin/python
tests/run_all.py`` runs each test under the venv). Prints a one-line PASS/FAIL
per file plus a final summary, and exits nonzero if any file fails.

This is the suite's single source of truth for "is everything green?" — there is
no conftest and pytest is not installed, so individual files are standalone
``main()`` runners. Run before and after any refactor.

Usage:
    ./venv/bin/python tests/run_all.py            # run all
    ./venv/bin/python tests/run_all.py v2 fork    # only files whose name contains v2 or fork
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PER_FILE_TIMEOUT_S = 180


def _discover(filters: list[str]) -> list[Path]:
    files = sorted(p for p in TESTS_DIR.glob("test_*.py") if p.name != "run_all.py")
    if filters:
        files = [p for p in files if any(f in p.name for f in filters)]
    return files


def _last_meaningful_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()[:100]
    return ""


def main(argv: list[str]) -> int:
    files = _discover(argv)
    if not files:
        print("no test files matched")
        return 1

    results: list[tuple[str, bool, str, float]] = []
    for path in files:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(TESTS_DIR.parent),
                capture_output=True,
                text=True,
                timeout=PER_FILE_TIMEOUT_S,
            )
            ok = proc.returncode == 0
            summary = _last_meaningful_line(proc.stdout + "\n" + proc.stderr)
        except subprocess.TimeoutExpired:
            ok = False
            summary = f"TIMEOUT after {PER_FILE_TIMEOUT_S}s"
        results.append((path.name, ok, summary, time.monotonic() - start))

    print("=" * 72)
    for name, ok, summary, secs in results:
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] {name:<32} {secs:5.1f}s  | {summary}")
    print("=" * 72)

    passed = sum(1 for _, ok, _, _ in results if ok)
    total = len(results)
    failed = [name for name, ok, _, _ in results if not ok]
    if failed:
        print(f"{passed}/{total} files passed — FAILED: {', '.join(failed)}")
        return 1
    print(f"{passed}/{total} files passed — ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
