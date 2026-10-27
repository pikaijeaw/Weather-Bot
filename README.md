# Polymarket Weather Bot (CLOB V2)

Trades Polymarket **Weather** daily temperature markets ("Highest/Lowest temperature in <city> on <date>?") in all listed cities.

## How it works
1. Pulls every open temperature event from Gamma (`tag_slug=weather`).
2. Reads the resolution station (ICAO) and the unit (°F or °C) from each market's rules.
3. Fetches forecasts at that station from 82 ensemble members (ECMWF 51 + GFS 31, Open-Meteo). For today's markets it also uses METAR readings observed so far.
4. Turns each forecast member into a bucket probability, then compares it with the order book's ask plus the taker fee.
   - **Visual Crossing cross-check** (`VC_KEY` in `.env`): its single forecast for the station counts for 25% of the probability (`vc_weight`). The ensemble counts for the other 75%. If Visual Crossing and the ensemble average differ by more than 2°C (3.6°F, set by `vc_max_disagree_c`), the bot skips that market. The forecasts are cached for 3 hours, which uses about 400 of the free plan's 1000 daily calls. Set `"vc_enabled": false` to turn it off.
5. Buys YES or NO when `min_edge <= edge <= max_edge`. The order type is FAK (fills what it can at the limit price, cancels the rest). Position size is fractional Kelly on the pUSD balance, limited by `max_bet_usd` and `max_total_exposure_usd`.
6. Settles positions when markets resolve and sends alerts to Telegram.

## Setup
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill PK, WALLET, FUNDER, SIG_TYPE, Telegram
```
- `FUNDER` is your Polymarket deposit wallet (shown in the polymarket.com profile menu). This wallet holds the **pUSD** the bot trades with.
- `SIG_TYPE=3` means the deposit wallet signs orders (POLY_1271). Use `0` only if you hold pUSD directly in the EOA.
- Fund the account by depositing on polymarket.com. The money must end up as pUSD in the deposit wallet.

## Run the bot

### One-off commands (foreground)
```bash
venv/bin/python weatherbot.py scan --dry   # one pass, never trades
venv/bin/python weatherbot.py scan         # one pass, trades only if mode=live
venv/bin/python weatherbot.py status       # pUSD balance + positions
```

### In the background
Use the scripts (or the dashboard buttons below):
```bash
./start.sh                  # start the loop in the background (scans every scan_interval_sec)
./stop.sh                   # stop it
tail -f data/weatherbot.log # follow the log
```
- The running bot's PID is stored in `data/bot.pid`. Both the scripts and the dashboard use this file, so it doesn't matter which one you use to start or stop.
- The bot keeps running after you log out of SSH. It does **not** start automatically after a server reboot. After a reboot, press **Start** on the dashboard or run `./start.sh`.

### Paper vs live
In `config.json`:
- `"mode": "paper"` (current setting) simulates each order against the **real order book** at that moment, including the actual taker fee. It keeps a paper wallet (`data/paper_wallet.json`, starts at `paper_bankroll` = $100), settles positions when markets resolve, and records everything in `data/trades.jsonl`. No real money is used.
- `"mode": "live"` places real orders with the pUSD in the `FUNDER` wallet.
- `weatherbot.py scan --dry` is a single pass that only logs `[DRY] would BUY …` and never records anything.

**Bust rule:** if equity (cash + cost of open positions) falls below `bust_threshold_usd` ($20):
- **paper:** the run is archived to `data/archive/bust-<time>/`, the bot restarts itself with a fresh $100, and Telegram is alerted.
- **live:** the bot stops opening new trades and alerts you. Real money is never refilled automatically.

After changing the mode, restart the bot with `./stop.sh && ./start.sh` or with Stop → Start on the dashboard.

## Dashboard (port 5001)

`dashboard.py` is a web UI with **Start / Stop / Reset** buttons and live status: whether the bot is running, the mode, pUSD balance, exposure, open and settled positions, PnL, the last scan's signals and the log tail. The page refreshes every 5 seconds.

- **URL:** `http://<server>:5001`
- **Login:** user `admin`. The password is `DASHBOARD_PASSWORD` in `.env`. The dashboard refuses to start without it.
- **Start** launches the bot in the background. **Stop** stops it.
- **Reset** stops the bot and moves `data/positions.json` and `data/weatherbot.log` into `data/archive/<timestamp>/`. It does **not** sell or cancel anything on Polymarket.

### Run the dashboard in the background (systemd, recommended)
The dashboard is installed as a service that restarts on crash and starts on boot.
```bash
sudo systemctl status  weatherbot-dashboard    # check
sudo systemctl restart weatherbot-dashboard    # restart (e.g. after editing dashboard.py)
sudo systemctl stop    weatherbot-dashboard    # stop
sudo systemctl disable --now weatherbot-dashboard  # stop + don't start on boot
journalctl -u weatherbot-dashboard -f          # dashboard logs
```
Restarting or stopping the dashboard does **not** stop the bot (`KillMode=process`).

To install the service on a new machine, create the unit file:
```bash
sudo tee /etc/systemd/system/weatherbot-dashboard.service >/dev/null <<EOF
[Unit]
Description=Weatherbot dashboard (port 5001)
After=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/python dashboard.py
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now weatherbot-dashboard
```

### Run the dashboard without systemd
```bash
nohup venv/bin/python dashboard.py > data/dashboard.log 2>&1 &   # background
pkill -f "python dashboard.py"                                   # stop
```

### Access from your computer
The firewall (ufw) does not allow port 5001 by default. Use either option:
- **SSH tunnel (recommended):** run `ssh -L 5001:localhost:5001 <user>@<server>`, then open `http://localhost:5001`.
- **Open the port:** `sudo ufw allow 5001/tcp`. The connection is plain HTTP, so the password is not encrypted. Use a strong password, or put the dashboard behind HTTPS.

## Self-review (log_collector)

Two Hermes cron jobs look after the bot:

| job | when | what |
|---|---|---|
| `weatherbot-watchdog` | every 10 min | If the bot process is gone (reboot, crash), restarts it and posts to Telegram. Silent when healthy. It does not restart a bot you stopped on purpose (Stop leaves `data/.manual_stop`, Start removes it). |
| `weatherbot-daily-review` | 01:00 UTC daily | Runs `log_collector/collect.py`, reads the report and logs, fixes bugs, tunes `config.json` within the limits in `log_collector/TUNING_RULES.md`, commits to git, restarts the bot, and sends a report to Telegram. |

`log_collector/` contents:
- `collect.py`: archives each day's logs and trades into `log_collector/YYYY-MM-DD/` and builds the performance report (win rate, ROI, model calibration, and breakdowns by side, days ahead, edge, city and kind) in `log_collector/reports/`
- `TUNING_RULES.md`: what the review may change, in what steps, and how much evidence it needs (at least 20 settled trades, at most 2 parameters per day). It must **never** switch to live mode.
- `changes.md`: history of every automatic fix or tune
- `watchdog.sh`: the restart check

Run it by hand with `venv/bin/python log_collector/collect.py`. Every automatic change is a git commit, so you can undo one with `git revert <commit>`.

## Files
- `weatherbot.py`: the whole bot
- `dashboard.py`: web dashboard (port 5001)
- `start.sh` / `stop.sh`: start/stop the bot in the background
- `config.json`: strategy and risk limits
- `.env`: secrets (PK, wallets, Telegram, `DASHBOARD_PASSWORD`)
- `data/`: positions, logs, `bot.pid`, archive and the station cache
