# Daily self-review rules (read by the daily Hermes cron job)

The daily job runs `venv/bin/python log_collector/collect.py`, reads the report and the logs, then
fixes bugs and/or tunes `config.json` following these rules. Every change is logged and reported to Telegram.

## 1. Bugs (always fix)
- Any `Traceback`, `scan crashed`, repeated `ERROR`, or a scan that never completes is a bug. Find the
  root cause in `weatherbot.py` / `dashboard.py` and fix it.
- **"0 trades" is not "healthy".** If the report says `BLIND` (every scan for 24h found 0 candidates),
  or there were 0 buys for 24h while events are open, treat it as a bug and find which filter in
  `fetch_weather_events()` / `evaluate_event()` drops every event: no station (`icao` None), wrong unit,
  wrong date / days_ahead, no forecast, or no prices. Compare a few current events' raw Gamma
  `description` / `resolutionSource` against the parsing regexes. Polymarket rewrites the rules text
  from time to time (it did on 2026-10-22, which blinded the bot for 4 days).
- A `STATION PARSE` error means the rules format changed. Fix `parse_station()`.
- The report must start with ⚠️ whenever the bot placed 0 trades in 24h and you could not explain why.
- After any code change:
  1. `venv/bin/python -m py_compile weatherbot.py dashboard.py log_collector/collect.py`
  2. `timeout 600 venv/bin/python weatherbot.py scan --dry`, which must finish with `=== scan done` and no Traceback
  3. `git add -A && git commit -m "auto-fix: <what>"`
  4. restart the bot: `./stop.sh; ./start.sh`
- If the fix does not pass step 2, revert it (`git checkout -- <file>`) and report the bug as unresolved.

## 2. Parameter tuning (only with evidence)
Only tune when there are **≥ 20 settled trades** overall (or ≥ 10 in the specific group being changed).
Change at most **2 parameters per day**, each by at most one step. Never go outside these bounds:

| param | bounds | step | raise when | lower when |
|---|---|---|---|---|
| `min_edge` | 0.05–0.20 | 0.02 | ROI < 0 overall, or the 0.08–0.12 edge bucket loses money | ROI > +10% and < 3 trades/day |
| `max_edge` | 0.15–0.40 | 0.05 | the 0.18+ edge bucket wins (ROI > 0, n ≥ 10) | the 0.18+ edge bucket loses money |
| `kernel_sd_c` | 0.6–2.0 | 0.1 | model is over-confident: high-p buckets hit well below avg_model_p | under-confident: hit rate well above avg_model_p |
| `sd_growth_per_day` | 0.1–0.6 | 0.05 | D+1/D+2 lose but D+0 is fine | — |
| `max_days_ahead` | 0–3 | 1 | — | D+2 (or D+1) group ROI < −10% with n ≥ 10 |
| `vc_weight` | 0.0–0.5 | 0.05 | trades where VC agreed outperform | — |
| `vc_max_disagree_c` | 1.0–4.0 | 0.5 | too few trades | big losses on trades where VC differed |
| `trade_no_side` | true/false | — | — | NO side ROI < −15% with n ≥ 15 |
| `kelly_fraction` | 0.10–0.35 | 0.05 | ROI > +10% over ≥ 40 trades | ROI < 0 |
| `max_bet_usd` | 1–5 | 0.5 | ROI > +10% over ≥ 40 trades | drawdown > 30% of start |

- If `cities` must exclude a city: add the remaining cities to the `cities` list only if that city has n ≥ 8 and ROI < −30%.
- Never change: `mode`, `paper_bankroll`, `bust_threshold_usd`, `max_total_exposure_usd`, keys in `.env`.
  **Never switch to live mode.** Only the user does that.
- Record every tune as one line in `log_collector/changes.md`: date, param, old → new, the evidence (n, ROI, win rate).
- Then `git commit -am "auto-tune: ..."` and restart the bot.

## 3. Bankroll
- Paper equity < $20 means the bot archives the run and restarts itself at $100 (`check_bust` in weatherbot.py).
  On a bust day the report must say so and explain the most likely cause.
- Live equity < $20 means the bot stops opening trades and alerts. Real funds are never refilled automatically.

## 4. Report (Telegram)
Short. Include: health (scans/errors), trades (buys/settled/open), paper balance, win rate + ROI,
calibration verdict (over/under-confident or OK), what was fixed, what was tuned (old → new + reason),
and "no changes" if nothing was changed.
