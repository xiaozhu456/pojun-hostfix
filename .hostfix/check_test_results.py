"""Fail closed if PG-dependent lifecycle regression was skipped instead of run."""
import json
import pathlib
import sys

events = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if line.startswith("{")]
if any(event.get("Action") == "fail" for event in events):
    raise SystemExit("Go regression failed")
required = "TestCoreTaskLifecyclePG"
if not any(event.get("Test") == required and event.get("Action") == "pass" for event in events):
    raise SystemExit("required PostgreSQL lifecycle test did not pass (possibly skipped)")
print("Complete Go regression passed; PostgreSQL lifecycle fixture was executed")
