#!/usr/bin/env python3
"""Run every test suite. `python tests/run.py` from the worker/ directory.

These run off-platform: tests/fakes.py stubs the Worker runtime and backs the
D1 binding with real SQLite, so schema.sql and every query in db.py and
payments.py are genuinely executed. Requires: pip install fastapi jinja2 httpx
python-multipart
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
SUITES = ["test_app.py", "test_money.py", "test_sweeper.py"]

failed = []
for suite in SUITES:
    print(f"\n{'#' * 60}\n#  {suite}\n{'#' * 60}")
    if subprocess.run([sys.executable, str(HERE / suite)]).returncode != 0:
        failed.append(suite)

print("\n" + "=" * 60)
print("FAILED: " + ", ".join(failed) if failed else "All suites passed.")
sys.exit(1 if failed else 0)
