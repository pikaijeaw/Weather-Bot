#!/usr/bin/env python3
"""
Daily log collector + performance analyzer for weatherbot.

  venv/bin/python log_collector/collect.py            # archive yesterday+today, print report
  venv/bin/python log_collector/collect.py --json     # machine-readable report

What it does
  1. Archives logs into log_collector/YYYY-MM-DD/:
       weatherbot.log lines for that date, trades.jsonl records for that date,
       dashboard journal (systemd) for that date.
  2. Builds a report from ALL trades in data/trades.jsonl (+ archived busts):
       - error / warning / traceback counts and the distinct error messages
       - settled trades: win rate, PnL, ROI
       - calibration: model probability vs actual hit rate (is the model over-confident?)
       - breakdowns by side (YES/NO), days_ahead, edge bucket, city, kind (max/min)
  3. Writes log_collector/reports/YYYY-MM-DD.json and .md
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LC = ROOT / "log_collector"
REPORTS = LC / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)


def log_files():
    """Current log + rotated copies (weatherbot.log.1 ...) + archived logs."""
    files = sorted(DATA.glob("weatherbot.log*"))
    files += sorted((DATA / "archive").glob("*/weatherbot.log*"))
    return files


def read_lines():
    out = []
    for f in log_files():
        try:
            out += f.read_text(errors="ignore").splitlines()
        except Exception:
            pass
    return out


def read_journal():
    recs = []
    f = DATA / "trades.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    return recs


def archive_day(day: str, lines, recs):
    d = LC / day
    d.mkdir(exist_ok=True)
    day_lines = sorted(set(l for l in lines if l.startswith(day)))
    if day_lines:
        (d / "weatherbot.log").write_text("\n".join(day_lines) + "\n")
    day_recs = [r for r in recs if r.get("ts", "").startswith(day)]
    if day_recs:
        (d / "trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in day_recs))
    try:
        j = subprocess.run(["journalctl", "-u", "weatherbot-dashboard", "--no-pager",
                            "--since", f"{day} 00:00:00", "--until", f"{day} 23:59:59"],
                           capture_output=True, text=True, timeout=30).stdout
        if j.strip() and "No entries" not in j:
            (d / "dashboard.log").write_text(j)
    except Exception:
        pass
    for f in ("config.json",):
        src = ROOT / f
        if src.exists() and not (d / f).exists():
            (d / f).write_text(src.read_text())
    return len(day_lines), len(day_recs)


def bucket(v, edges):
    for lo, hi, name in edges:
        if lo <= v < hi:
            return name
    return "other"


def stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = sum(1 for r in rows if r["status"] == "won")
    cost = sum(r["cost"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    return {"n": n, "wins": wins, "win_rate": round(wins / n, 3),
            "avg_model_p": round(sum(r["p"] for r in rows) / n, 3),
            "avg_price": round(sum(r["price"] for r in rows) / n, 3),
            "pnl": round(pnl, 2), "cost": round(cost, 2), "roi": round(pnl / cost, 3) if cost else 0}


def analyze(lines, recs, day: str):
    since = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    recent = [l for l in lines if l[:10] >= since]
    errs = [l for l in recent if " ERROR " in l]
    warns = [l for l in recent if " WARNING " in l]
    tb = sum(1 for l in recent if "Traceback" in l)
    norm = lambda l: re.sub(r"\d+(\.\d+)?", "#", l[20:])[:160]
    skip_reasons = Counter(m.group(1).split(" ")[0] for l in recent
                           if (m := re.search(r"skip \(([^)]*)\)", l)))
    scans = [l for l in recent if "=== scan done" in l]
    crashes = [l for l in recent if "scan crashed" in l]
    # a scan that finds 0 candidates is normal sometimes, but every scan for hours means
    # the bot is blind (e.g. Polymarket changed the rules text and stations stop parsing)
    cand_counts = [int(m.group(1)) for l in recent
                   if (m := re.search(r"(\d+) candidate\(s\) after pre-filter", l))]
    last_day = [l for l in lines if l[:10] == day]
    cand_today = [int(m.group(1)) for l in last_day
                  if (m := re.search(r"(\d+) candidate\(s\) after pre-filter", l))]
    blind = len(cand_counts) >= 12 and max(cand_counts, default=0) == 0

    buys = [r for r in recs if r.get("type") == "buy"]
    settles = [r for r in recs if r.get("type") == "settle"]
    busts = [r for r in recs if r.get("type") == "bust"]
    buy_by_id = {r["id"]: r for r in buys}
    for s in settles:  # enrich settles with buy-time info
        b = buy_by_id.get(s["id"], {})
        for k in ("edge", "days_ahead", "city", "kind", "vc", "obs"):
            if s.get(k) is None and k in b:
                s[k] = b[k]

    calib = defaultdict(list)
    for s in settles:
        calib[bucket(s["p"], [(0, .2, "0-20%"), (.2, .4, "20-40%"), (.4, .6, "40-60%"),
                              (.6, .8, "60-80%"), (.8, 1.01, "80-100%")])].append(s)
    by = lambda key: {k: stats(v) for k, v in sorted(_group(settles, key).items())}

    open_ids = {r["id"] for r in buys} - {r["id"] for r in settles}
    return {
        "date": day,
        "health": {"scans_completed": len(scans), "scan_crashes": len(crashes),
                   "errors": len(errs), "warnings": len(warns), "tracebacks": tb,
                   "candidates_per_scan_max": max(cand_counts, default=0),
                   "candidates_per_scan_avg": round(sum(cand_counts) / len(cand_counts), 1) if cand_counts else 0,
                   "zero_candidate_scans": sum(1 for c in cand_counts if c == 0),
                   "candidates_today_max": max(cand_today, default=0),
                   "station_parse_errors": sum(1 for l in recent if "STATION PARSE" in l),
                   "BLIND": blind,
                   "top_errors": Counter(map(norm, errs)).most_common(8),
                   "top_warnings": Counter(map(norm, warns)).most_common(8),
                   "skip_reasons": skip_reasons.most_common()},
        "trades": {"buys_total": len(buys), "settled_total": len(settles), "open": len(open_ids),
                   "busts": len(busts),
                   "buys_last_24h": sum(1 for r in buys if r["ts"][:10] >= since)},
        "overall": stats(settles),
        "last_7d": stats([s for s in settles if s["ts"][:10] >=
                          (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")]),
        "calibration": {k: stats(v) for k, v in sorted(calib.items())},
        "by_side": by(lambda s: s.get("side")),
        "by_days_ahead": by(lambda s: f"D+{s.get('days_ahead')}"),
        "by_kind": by(lambda s: s.get("kind")),
        "by_edge": by(lambda s: bucket(s.get("edge") or 0, [(0, .12, "0.08-0.12"), (.12, .18, "0.12-0.18"),
                                                            (.18, 1, "0.18+")])),
        "by_city": by(lambda s: s.get("city")),
        "paper_wallet": json.loads((DATA / "paper_wallet.json").read_text())
        if (DATA / "paper_wallet.json").exists() else None,
        "config": json.loads((ROOT / "config.json").read_text()),
    }


def _group(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return g


def to_md(rep):
    h, t, o = rep["health"], rep["trades"], rep["overall"]
    L = [f"# Weatherbot daily report {rep['date']}", ""]
    if h["BLIND"]:
        L.append(f"**⚠️ BLIND: every scan in 24h found 0 candidates ({h['zero_candidate_scans']} scans). "
                 f"This is a bug, not a quiet market — check station/unit/date parsing and the "
                 f"pre-filter in evaluate_event().**")
    if h["station_parse_errors"]:
        L.append(f"**⚠️ STATION PARSE errors: {h['station_parse_errors']}** — Polymarket rules format changed?")
    L += [f"**Health (24h):** {h['scans_completed']} scans, {h['scan_crashes']} crashes, "
         f"{h['errors']} errors, {h['warnings']} warnings, {h['tracebacks']} tracebacks",
         f"**Candidates per scan (24h):** max {h['candidates_per_scan_max']}, "
         f"avg {h['candidates_per_scan_avg']}, {h['zero_candidate_scans']} scans with 0",
         f"**Trades:** {t['buys_total']} buys ({t['buys_last_24h']} in 24h), {t['settled_total']} settled, "
         f"{t['open']} open, {t['busts']} busts"]
    if rep["paper_wallet"]:
        L.append(f"**Paper wallet:** ${rep['paper_wallet']['balance']:.2f} (start ${rep['paper_wallet']['start']:.0f})")
    if o.get("n"):
        L.append(f"**Overall:** {o['wins']}/{o['n']} won ({o['win_rate']:.0%}), "
                 f"avg model p {o['avg_model_p']:.2f}, PnL ${o['pnl']:+.2f}, ROI {o['roi']:+.1%}")
    for title, key in (("Calibration (model p vs hit rate)", "calibration"), ("By side", "by_side"),
                       ("By days ahead", "by_days_ahead"), ("By edge", "by_edge"), ("By kind", "by_kind")):
        sec = {k: v for k, v in rep[key].items() if v.get("n")}
        if sec:
            L += ["", f"## {title}"] + [
                f"- {k}: n={v['n']} win {v['win_rate']:.0%} vs model {v['avg_model_p']:.2f} | "
                f"PnL ${v['pnl']:+.2f} ROI {v['roi']:+.0%}" for k, v in sec.items()]
    if h["top_errors"]:
        L += ["", "## Errors"] + [f"- {c}× {m}" for m, c in h["top_errors"]]
    if h["top_warnings"]:
        L += ["", "## Warnings"] + [f"- {c}× {m}" for m, c in h["top_warnings"]]
    return "\n".join(L) + "\n"


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    lines, recs = read_lines(), read_journal()
    archived = {d: archive_day(d, lines, recs) for d in (yday, today)}
    rep = analyze(lines, recs, today)
    rep["archived"] = archived
    (REPORTS / f"{today}.json").write_text(json.dumps(rep, indent=1, default=str))
    md = to_md(rep)
    (REPORTS / f"{today}.md").write_text(md)
    print(json.dumps(rep, indent=1, default=str) if "--json" in sys.argv else md)


if __name__ == "__main__":
    main()
