# obsbot — real-world vs Polymarket gap bot (paper only)

Separate from `weatherbot.py`: its own config, data, log, paper wallet and watchdog.
It uses the same `.env` (Telegram) and `venv`, and the same dashboard (port 5001, **Obsbot** section).

## Idea
It does not forecast. For every open Polymarket temperature market (all cities, highs and lows),
it reads live METAR reports from the **resolution station named in the market rules** and looks for
buckets that the real world has already decided while the order book still sells them below $1.

| tier | rule | prior |
|---|---|---|
| `hard` | Today's running extreme rules the bucket out. A daily high can only rise, so every bucket below the observed max is NO and `X or higher` is YES once obs ≥ X+margin. Lows are the mirror case. | very sure |
| `final` | The station's local day ended 90 min ago with full coverage, so the extreme is known. Margins allow for peaks that fell between reports. | sure |
| `late` | Late in the day, the bias-corrected hourly forecast can't reach the bucket. | **unproven**: trades only after it has proven itself |

Safety guards: skip extremes within 1 h of local midnight; bucket margins in °C (×1.8 for °F);
skip final days with <18 reports or gaps >3 h.

### Other weather markets (`extra.py`)
These use the same rule: the real-world value can only rise during the window.
| tier | markets | source | certain? |
|---|---|---|---|
| `quake` | how-many-M-or-above earthquakes (weekly/yearly), biggest earthquake of a day, highest magnitude of the year, how-strong-by-date | USGS catalog (real time) | yes (count/max only grows). The 0.2-magnitude margin covers USGS revisions |
| `rain_month` / `rain_month_final` | NYC monthly precipitation | NWS Daily Climate Report (CLI) month-to-date | yes |
| `rain_cli` / `rain_cli_final` | where-will-it-rain-on-date (14 US cities) | CLI precip ≥ 0.01" → YES; final CLI with 0/T → NO | yes |
| `rain_metar` | where-will-it-rain | hourly METAR rain groups ≥ 0.03" before the CLI is out | **unproven**, must learn |

Not covered: Seattle, London, Hong Kong and Seoul precipitation (their resolution sources have no matching
live feed); rivers, drought, El Niño, yearly records, disease, meteors and volcanoes (slow or irregular official data).

## Self-learning
- Each signal is recorded the first time it appears, whether or not it was traded (`data/signals.json`).
- When the market resolves, the result is written into `data/learned.json` under the key
  `tier|kind|side|margin_band[|hour]`. One event counts once per key.
- Buy rule: the Beta-posterior **10% lower bound** of the win rate must beat ask + fee + `min_edge`.
  Size uses quarter-Kelly on that lower bound, so tiers that keep winning get more confidence
  and more trades, and losing tiers stop.
- If a certain-tier signal loses (hard, final, rain_*, but not quake), the station is **blocked** and you get a Telegram alert.
  That loss means the resolution source disagreed with METAR.
- Reset from the dashboard archives the wallet, positions and log. **Learning is kept.**

## Run
Use the dashboard (**Obsbot → Start/Stop/Reset**), or:
```
venv/bin/python obsbot/obsbot.py scan --dry   # one pass, log only
venv/bin/python obsbot/obsbot.py status       # wallet, positions, learned stats
```
The Hermes cron `obsbot-watchdog` (every 10 min, `obsbot/watchdog.sh`) restarts it after a crash or reboot.
It stays down after a manual Stop.

Daily review: the Hermes cron `obsbot-daily-review` (01:30 UTC) follows `REVIEW_RULES.md`. It checks health,
audits every loss in a certain tier, fixes bugs, tunes margins only from evidence (≤ 2 changes/day, logged in
`changes.md`) and sends a Telegram report. It never enables live trading.

Paper only: the script refuses to start unless `"mode": "paper"`. It has no order-placing code.
