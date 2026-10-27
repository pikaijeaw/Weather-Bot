# obsbot daily review rules (read by the `obsbot-daily-review` Hermes cron job)

obsbot is **paper only**. Never add order-placing code. Never set `mode` to anything but `paper`.
Never touch `.env`, `weatherbot.py`, `config.json` (the weatherbot one), or the weatherbot data.

## 1. Health
- The bot is running: `kill -0 $(cat obsbot/data/bot.pid)`. A new `=== scan |` line appears every ≤ 10 min.
  No `Traceback` or `scan crashed` in the last 24 h.
- Each scan line `N real-world signals {...}` must show temperature tiers (hard/final/late) **and** extra tiers
  (quake / rain_*). `extra markets modelled: 0` for a whole day means a parser broke. Polymarket may
  have renamed slugs or labels, so check `obsbot/extra.py` against the live Gamma events.
- Fix bugs at the root. Validate: `venv/bin/python -m py_compile obsbot/obsbot.py obsbot/extra.py dashboard.py`
  then `timeout 900 venv/bin/python obsbot/obsbot.py scan --dry` must end with `=== scan done` and no Traceback.
  If the fix fails, revert it (`git checkout -- <file>`) and report it as unresolved.
  Commit with `auto-fix(obsbot): ...`. Restart: dashboard API, or kill the PID and let `obsbot/watchdog.sh` restart it.

## 2. Learning audit
`venv/bin/python obsbot/obsbot.py status` prints learned keys (`tier|kind|side|band[|hour]`: wins/losses → p_lb).
- **Any loss in a certain tier** (hard, final, quake, rain_month, rain_month_final, rain_cli, rain_cli_final) is a red flag.
  Find the record (`grep '"ok": false' obsbot/data/trades.jsonl`) and the market rules, then decide the cause:
  (a) data-source mismatch, e.g. METAR vs NOAA timeseries, or CLI vs another product. The station is auto-blocked; keep it blocked.
  (b) a parse or timezone bug: fix the code and unblock the station (remove it from `blocked` in `learned.json`).
  (c) a margin that's too small: raise that tier's margin one step.
- Do not edit win/loss counts by hand.

## 3. Tuning (evidence only, max 2 changes/day, one step each, log every change)
| param | bounds | step | when |
|---|---|---|---|
| `hard_margin_c` / `final_margin_c` / `late_margin_c` | 0.25–2.0 | 0.25 | raise after a loss caused by margin (2c); lower only if the tier has ≥ 30 resolved with 0 losses **and** 0 trades because of no asks |
| `final_unseen_c` | 0.5–1.5 | 0.25 | raise if a final loss came from a peak missed between METARs |
| `quake_mag_margin` | 0.1–0.4 | 0.1 | raise after a quake loss caused by a USGS magnitude revision |
| `rain_metar_min_in` | 0.02–0.10 | 0.01 | raise if rain_metar has losses (METAR rain, no CLI ≥ 0.01) |
| `late_min_hour.max` / `.min` | 14–21 / 8–15 | 1 | raise if late keys for early hours lose; lower if all late hours ≥ 20 resolved with ≥ 97% wins |
| `min_edge` | 0.01–0.05 | 0.01 | raise if paper ROI < 0 over ≥ 20 settled trades |
| `scan_interval_sec` | 60–600 | 60 | lower if gaps (`[GAP]` lines) appear but asks are gone by the next scan |

- Never change `priors`, `confidence_q`, `paper_bankroll`, exposure caps, or `mode`.
- Append one line per change to `obsbot/changes.md`: date | param old → new | evidence.
  Commit with `auto-tune(obsbot): ...`. Don't change a param again that was changed in the last 3 days.

## 4. Report (Telegram, plain text, ≤ 20 lines)
```
🔭 Obsbot daily — <date>
Health: scans/errors, running?
Signals: today's counts per tier; how many had asks; how many were mispriced
Paper: balance $X (start $100), trades 24h, settled, won, PnL
Learned: keys with new results today (w/l); any losses in certain tiers + cause
Blocked: stations (new today?)
Fixed / Tuned: ... (or none)
Verdict: is the gap real? (trades happening? market already priced?)
```
Start the report with ⚠️ if something is broken and you couldn't fix it.
