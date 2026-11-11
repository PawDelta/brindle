"""Print every T0 check from out/results_*.json and exit non-zero if any failed."""

import json
import sys
from pathlib import Path

OUT = Path(__file__).with_name("out")
rows = []
for name in ("results_node1.json", "results_py.json", "results_node2.json", "results_py_late.json", "results_platforms.json"):
    p = OUT / name
    if p.exists():
        rows += json.loads(p.read_text())
    else:
        rows.append({"id": name, "side": "-", "ok": False, "detail": "phase did not produce results"})
bad = 0
for r in rows:
    tag = "PASS" if r["ok"] is True else "FAIL" if r["ok"] is False else "INFO"
    bad += r["ok"] is False
    print(f"{tag}  {r['id']}  [{r['side']}]  {r['detail']}")
print(f"\n{sum(r['ok'] is True for r in rows)} passed, {bad} failed, {sum(r['ok'] is None for r in rows)} info")
sys.exit(1 if bad else 0)
