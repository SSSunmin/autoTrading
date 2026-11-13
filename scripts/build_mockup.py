"""Fill the dashboard mock-up with the audit records, memory log and baseline summary under data/.

Run after scripts/baseline_krx.py:  .venv/Scripts/python scripts/build_mockup.py [--market KRX|US]
Reads one market's book (audit.market_audit: data/audit/<market>/, KRX also the pre-market-axis data/audit/ files)
and its baseline summary (audit.baseline_summary_path). Writes mockup/dashboard.html (KRX) or
mockup/dashboard_<market>.html from mockup/dashboard.template.html by replacing the /*__DATA__*/ marker with a
JSON literal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.audit import baseline_summary_path, market_audit, stage_counts
from agent_trading_core.config import MARKETS
from agent_trading_core.memory import TradingMemoryLog

DATA = ROOT / "data"


def build_payload(data: Path, market: str) -> dict:
    audit = market_audit(data / "audit", market)
    summary = json.loads(baseline_summary_path(data, market).read_text(encoding="utf-8"))
    log = TradingMemoryLog({"memory_log_path": summary["log_path"]})

    screens = []
    for run in audit.screens():
        d = run.model_dump()
        d["stage_counts"] = stage_counts(run)
        screens.append(d)
    decisions = [d.model_dump() for d in audit.decisions()]
    entries = log.load_entries()
    names = {c["ticker"]: c.get("name") for s in screens for c in s["candidates"] if c.get("name")}

    summary.pop("log_path", None)  # local absolute path; not needed on screen
    cache = [{"file": p.name, "kb": round(p.stat().st_size / 1024), "mtime": p.stat().st_mtime}
             for p in sorted((data / "cache").glob("*.csv"))]

    return {
        "cache": cache,
        "generated_from": {"run_id": summary["run_id"], "cells": summary["cells_run"],
                           "dates": summary["dates"], "universe": summary["universe"]},
        "screens": screens, "decisions": decisions, "memory": entries, "summary": summary, "names": names,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="KRX", choices=sorted(MARKETS))
    args = parser.parse_args()
    payload = build_payload(DATA, args.market)
    screens, decisions, entries = payload["screens"], payload["decisions"], payload["memory"]
    template = (ROOT / "mockup" / "dashboard.template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    out = ROOT / "mockup" / ("dashboard.html" if args.market == "KRX" else f"dashboard_{args.market}.html")
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB): {len(screens)} screens, "
          f"{len(decisions)} decisions, {len(entries)} memory entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
