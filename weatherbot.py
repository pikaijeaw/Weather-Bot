#!/usr/bin/env python3
"""
Polymarket Weather Bot (CLOB V2)
================================
Trades Polymarket "Weather" daily temperature markets
("Highest/Lowest temperature in <city> on <date>?").

Model:
  * Resolution station (ICAO) is parsed from each market's rules.
  * Forecast = Open-Meteo ensembles (ECMWF IFS 51 members + NCEP GEFS 31 members)
    at the station's coordinates, daily max/min in the station's local day.
  * For today's markets, METAR observations so far bound the result
    (high can't end below the max already observed; low can't end above the min).
  * Each member is blurred with a Gaussian kernel (station-vs-grid error) and
    integrated over the bucket (whole-degree rounding => [lo-0.5, hi+0.5)).
  * Buys YES or NO when model probability beats ask + taker fee by `min_edge`.
  * Size = fractional Kelly on pUSD balance, capped by max_bet / exposure limits.

Usage:
  python weatherbot.py run        # loop forever
  python weatherbot.py scan       # one pass (trades if mode=live)
  python weatherbot.py scan --dry # one pass, never trades
  python weatherbot.py status     # balance + positions
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
POSITIONS_FILE = DATA_DIR / "positions.json"
STATIONS_FILE = DATA_DIR / "stations.json"

load_dotenv(BOT_DIR / ".env")
CFG = json.loads((BOT_DIR / "config.json").read_text())

PK = os.getenv("PK", "").strip()
SIGNER = os.getenv("WALLET", "").strip()
FUNDER = os.getenv("FUNDER", "").strip() or SIGNER       # Polymarket deposit wallet
SIG_TYPE = int(os.getenv("SIG_TYPE", "3"))               # 3 = deposit wallet (POLY_1271)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# --------------------------------------------------------------------------- logging
log = logging.getLogger("weatherbot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = RotatingFileHandler(DATA_DIR / "weatherbot.log", maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "weatherbot/2.0"


def get_json(url, params=None, timeout=20, tries=3):
    for i in range(tries):
        try:
            r = HTTP.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == tries - 1:
                log.warning("GET %s failed: %s", url, e)
            time.sleep(1.5 * (i + 1))
    return None


def telegram(msg: str):
    """Name kept for the many call sites; the message now goes to Discord #bot-reports."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from discord_notify import send
        send("" + msg)
    except Exception as e:
        log.warning("discord notify failed: %s", e)


# --------------------------------------------------------------------------- state
# live and paper positions are kept apart so paper results never mix with real money
PAPER_POSITIONS_FILE = DATA_DIR / "paper_positions.json"
TRADES_JOURNAL = DATA_DIR / "trades.jsonl"   # append-only, read by log_collector


def positions_file() -> Path:
    return POSITIONS_FILE if CFG["mode"] == "live" else PAPER_POSITIONS_FILE


def load_positions() -> dict:
    f = positions_file()
    if f.exists():
        return json.loads(f.read_text())
    return {}


def save_positions(p: dict):
    f = positions_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=2))
    tmp.replace(f)


def journal(event_type: str, **rec):
    """Append one record. `type` = buy | settle | bust | tune ; `kind` (if present) = max | min."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "type": event_type, "mode": CFG["mode"], **rec}
    with open(TRADES_JOURNAL, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------------------- market discovery
SLUG_RE = re.compile(r"^(highest|lowest)-temperature-in-(.+)-on-([a-z]+)-(\d{1,2})-(\d{4})$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}


def fetch_weather_events() -> list[dict]:
    events, offset = [], 0
    while True:
        batch = get_json(f"{GAMMA}/events", {"tag_slug": "weather", "closed": "false",
                                             "limit": 100, "offset": offset})
        if not batch:
            break
        events += batch
        if len(batch) < 100 or offset > 3000:
            break
        offset += 100
    out = []
    for e in events:
        m = SLUG_RE.match(e.get("slug", ""))
        if not m:
            continue
        kind, city, mon, day, year = m.groups()
        if mon not in MONTHS:
            continue
        desc = e.get("description") or (e.get("markets") or [{}])[0].get("description", "")
        icao = parse_station((e.get("resolutionSource") or "") + "\n" + desc)
        um = (re.search(r"in degrees (Fahrenheit|Celsius)", desc)
              or re.search(r"range in whole degrees (Fahrenheit|Celsius)", desc))
        if um:
            unit = um.group(1)[0]
        else:  # fall back to bucket labels
            labels = " ".join(m.get("groupItemTitle") or "" for m in e.get("markets", []))
            unit = "F" if "°F" in labels else "C"
        out.append({
            "slug": e["slug"], "title": e.get("title", ""), "kind": "max" if kind == "highest" else "min",
            "city": city, "date": f"{year}-{MONTHS[mon]:02d}-{int(day):02d}",
            "icao": icao, "unit": unit, "neg_risk": bool(e.get("negRisk")),
            "markets": e.get("markets", []),
        })
    missing = [o["slug"] for o in out if not o["icao"]]
    if out and len(missing) > len(out) * 0.2:
        # rules text format changed again -> nothing can be evaluated; make it loud
        log.error("STATION PARSE: %d/%d events have no station (e.g. %s) — rules format changed?",
                  len(missing), len(out), missing[0])
    return out


def parse_station(desc: str) -> str | None:
    # Oct-2026 rules: "...metar?ids=KLGA..." and "LaGuardia Airport (KLGA)"
    m = re.search(r"metar\?ids=([A-Za-z0-9]{4})", desc)
    if m:
        return m.group(1).upper()
    m = re.search(r"(?:Airport|Aerodrome|Airfield|Station|Base)[^()\n]{0,40}\(([A-Z][A-Z0-9]{3})\)", desc)
    if m:
        return m.group(1).upper()
    # older rules formats
    m = re.search(r"timeseries\?site=([A-Za-z0-9]{4})", desc)
    if m:
        return m.group(1).upper()
    m = re.search(r"wunderground\.com/history/daily/[a-z]+/[^/\s]+/([A-Z0-9]{4})", desc)
    if m:
        return m.group(1).upper()
    return None


NUM = r"(-?\d+(?:\.\d+)?)"


def parse_bucket(market: dict):
    """Return continuous [lo, hi) in market units accounting for whole-degree rounding."""
    t = (market.get("groupItemTitle") or "").strip()
    q = market.get("question") or ""
    for s in (t, q):
        if re.search(NUM + r"\s*°?\s*[FC]?\s*or below", s, re.I):
            v = float(re.search(NUM + r"\s*°?\s*[FC]?\s*or below", s, re.I).group(1))
            return (-math.inf, v + 0.5)
        if re.search(NUM + r"\s*°?\s*[FC]?\s*or (higher|above)", s, re.I):
            v = float(re.search(NUM + r"\s*°?\s*[FC]?\s*or (higher|above)", s, re.I).group(1))
            return (v - 0.5, math.inf)
        m = re.search(NUM + r"\s*-\s*" + NUM + r"\s*°?\s*[FC]", s)
        if m:
            return (float(m.group(1)) - 0.5, float(m.group(2)) + 0.5)
    m = re.fullmatch(NUM + r"\s*°\s*[FC]", t)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)
    m = re.search(r"\bbe\s+" + NUM + r"\s*°\s*[FC]\s+on\b", q)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)
    return None


# --------------------------------------------------------------------------- weather data
_station_cache: dict = json.loads(STATIONS_FILE.read_text()) if STATIONS_FILE.exists() else {}


def station_coords(icao: str):
    if icao in _station_cache:
        return _station_cache[icao]
    d = get_json("https://aviationweather.gov/api/data/stationinfo", {"ids": icao, "format": "json"})
    if d:
        _station_cache[icao] = {"lat": d[0]["lat"], "lon": d[0]["lon"]}
        STATIONS_FILE.write_text(json.dumps(_station_cache, indent=1))
        return _station_cache[icao]
    return None


_fc_cache: dict = {}
FC_TTL = 3600


def ensemble(icao: str, unit: str):
    """{'offset': utc_offset_s, 'max': {date: [members]}, 'min': {...}}"""
    key = (icao, unit)
    hit = _fc_cache.get(key)
    if hit and time.time() - hit[0] < FC_TTL:
        return hit[1]
    st = station_coords(icao)
    if not st:
        return None
    d = get_json("https://ensemble-api.open-meteo.com/v1/ensemble", {
        "latitude": st["lat"], "longitude": st["lon"],
        "daily": "temperature_2m_max,temperature_2m_min",
        "models": "ecmwf_ifs025,gfs025", "timezone": "auto", "forecast_days": 5,
        "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
    }, timeout=30)
    if not d or "daily" not in d:
        return None
    daily = d["daily"]
    res = {"offset": d.get("utc_offset_seconds", 0), "max": {}, "min": {}}
    for kind in ("max", "min"):
        cols = [k for k in daily if k.startswith(f"temperature_2m_{kind}")]
        for i, date in enumerate(daily["time"]):
            vals = [daily[c][i] for c in cols if daily[c][i] is not None]
            res[kind][date] = vals
    _fc_cache[key] = (time.time(), res)
    return res


VC_KEY = os.getenv("VC_KEY", "").strip()
VC_CACHE_FILE = DATA_DIR / "vc_cache.json"
VC_TTL = 3 * 3600  # free tier = 1000 records/day; ~50 stations x 8 refreshes/day fits
_vc_cache: dict = json.loads(VC_CACHE_FILE.read_text()) if VC_CACHE_FILE.exists() else {}


def visual_crossing(icao: str, unit: str):
    """Visual Crossing point forecast at the station: {'max': {date: t}, 'min': {date: t}} or None."""
    if not VC_KEY or not CFG.get("vc_enabled", True):
        return None
    key = f"{icao}:{unit}"
    hit = _vc_cache.get(key)
    if hit and time.time() - hit["t"] < VC_TTL:
        return hit["d"]
    st = station_coords(icao)
    if not st:
        return None
    d = get_json(
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/"
        f"{st['lat']},{st['lon']}/next4days",
        {"unitGroup": "us" if unit == "F" else "metric", "include": "days",
         "elements": "datetime,tempmax,tempmin", "key": VC_KEY, "contentType": "json"},
        timeout=20, tries=2)
    if not d or "days" not in d:
        return hit["d"] if hit else None  # fall back to stale data on error / quota
    res = {"max": {x["datetime"]: x["tempmax"] for x in d["days"] if x.get("tempmax") is not None},
           "min": {x["datetime"]: x["tempmin"] for x in d["days"] if x.get("tempmin") is not None}}
    _vc_cache[key] = {"t": time.time(), "d": res}
    VC_CACHE_FILE.write_text(json.dumps(_vc_cache))
    return res


def observed_extreme(icao: str, unit: str, local_date: str, offset_s: int):
    """(max, min) observed so far on the station's local date from METARs, in market unit."""
    d = get_json("https://aviationweather.gov/api/data/metar",
                 {"ids": icao, "format": "json", "hours": 36})
    if not d:
        return None, None
    temps = []
    for ob in d:
        t = ob.get("temp")
        ts = ob.get("obsTime")
        if t is None or ts is None:
            continue
        # rules: SPECI (special) observations do not count
        if (ob.get("metarType") or "").upper() == "SPECI" or (ob.get("rawOb") or "").startswith("SPECI"):
            continue
        local = datetime.fromtimestamp(ts, timezone.utc) + timedelta(seconds=offset_s)
        if local.strftime("%Y-%m-%d") == local_date:
            temps.append(t * 9 / 5 + 32 if unit == "F" else float(t))
    if not temps:
        return None, None
    return max(temps), min(temps)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bucket_prob(members, lo, hi, sd, kind, obs):
    """Mean over members of P(final in [lo,hi)), final = max(obs, X) for highs / min(obs, X) for lows."""
    if not members:
        return None

    def p_between(m, a, b):
        pa = 0.0 if a == -math.inf else ncdf((a - m) / sd)
        pb = 1.0 if b == math.inf else ncdf((b - m) / sd)
        return max(pb - pa, 0.0)

    tot = 0.0
    for m in members:
        if obs is None:
            tot += p_between(m, lo, hi)
        elif kind == "max":
            if obs >= hi:
                p = 0.0
            elif obs >= lo:
                p = p_between(m, -math.inf, hi)
            else:
                p = p_between(m, lo, hi)
            tot += p
        else:
            if obs < lo:
                p = 0.0
            elif obs < hi:
                p = p_between(m, lo, math.inf)
            else:
                p = p_between(m, lo, hi)
            tot += p
    return tot / len(members)


# --------------------------------------------------------------------------- CLOB
_client = None


def clob():
    global _client
    if _client is None:
        from py_clob_client_v2.client import ClobClient
        c = ClobClient(CLOB, chain_id=137, key=PK, signature_type=SIG_TYPE, funder=FUNDER)
        c.set_api_creds(c.create_or_derive_api_key())
        _client = c
    return _client


def pusd_balance() -> float:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
    r = clob().get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                                            signature_type=SIG_TYPE))
    return int(r.get("balance", 0)) / 1e6


def best_ask(token_id: str):
    b = get_json(f"{CLOB}/book", {"token_id": token_id}, timeout=10)
    if not b or not b.get("asks"):
        return None, 0.0, b
    a = min(b["asks"], key=lambda x: float(x["price"]))
    return float(a["price"]), float(a["size"]), b


def fee_rate(condition_id: str):
    info = get_json(f"{CLOB}/clob-markets/{condition_id}", timeout=10) or {}
    fd = info.get("fd") or {}
    return float(fd.get("r", 0.0)), float(fd.get("e", 1.0)), float(info.get("mos", 5)), info


def simulate_fak_buy(book: dict, usd: float, limit: float, fee_r: float, fee_e: float, min_size: float):
    """Paper fill that mimics a real FAK market BUY against the live order book.

    Walks asks from best price up to `limit`, spending at most `usd` including taker fee
    (V2: fee = shares * r * (p*(1-p))^e, added to collateral spend). Unfilled remainder is
    killed, exactly like FAK. Returns dict shaped like the CLOB response.
    """
    asks = sorted(((float(a["price"]), float(a["size"])) for a in book.get("asks", [])),
                  key=lambda x: x[0])
    budget, shares, notional, fees = usd, 0.0, 0.0, 0.0
    for px, sz in asks:
        if px > limit + 1e-9 or budget <= 0.005:
            break
        per_share = px + fee_r * (px * (1 - px)) ** fee_e
        take = min(sz, budget / per_share)
        take = math.floor(take * 100) / 100  # CLOB share precision
        if take <= 0:
            break
        shares += take
        notional += take * px
        fees += take * (per_share - px)
        budget -= take * per_share
    if shares < min_size:
        return {"success": False, "status": "unmatched",
                "errorMsg": f"paper: only {shares:.2f} sh available <= limit (min {min_size:g})"}
    return {"success": True, "status": "matched", "orderID": f"paper-{int(time.time() * 1000)}",
            "makingAmount": round(notional + fees, 6), "takingAmount": round(shares, 2),
            "fee": round(fees, 6)}


# --------------------------------------------------------------------------- paper wallet
PAPER_WALLET = DATA_DIR / "paper_wallet.json"


def paper_balance() -> float:
    if not PAPER_WALLET.exists():
        PAPER_WALLET.write_text(json.dumps({"balance": CFG["paper_bankroll"],
                                            "start": CFG["paper_bankroll"]}))
    return float(json.loads(PAPER_WALLET.read_text())["balance"])


def paper_adjust(delta: float):
    w = json.loads(PAPER_WALLET.read_text()) if PAPER_WALLET.exists() else \
        {"balance": CFG["paper_bankroll"], "start": CFG["paper_bankroll"]}
    w["balance"] = round(w["balance"] + delta, 6)
    PAPER_WALLET.write_text(json.dumps(w))


def place_buy(token_id: str, usd: float, max_price: float, neg_risk: bool, tick: str):
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType, PartialCreateOrderOptions
    args = MarketOrderArgsV2(token_id=token_id, amount=round(usd, 2), side="BUY",
                             price=max_price, order_type=OrderType.FAK)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    return clob().create_and_post_market_order(args, opts, order_type=OrderType.FAK)


# --------------------------------------------------------------------------- strategy
def local_today(offset_s: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime("%Y-%m-%d")


def evaluate_event(ev: dict) -> list[dict]:
    """Return candidate trades (sorted best first) for an event."""
    if not ev["icao"]:
        return []
    fc = ensemble(ev["icao"], ev["unit"])
    if not fc:
        return []
    today = local_today(fc["offset"])
    days_ahead = (datetime.strptime(ev["date"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
    if days_ahead < 0 or days_ahead > CFG["max_days_ahead"]:
        return []
    members = fc[ev["kind"]].get(ev["date"]) or []
    if len(members) < 10:
        return []

    obs = None
    if days_ahead == 0:
        omax, omin = observed_extreme(ev["icao"], ev["unit"], ev["date"], fc["offset"])
        obs = omax if ev["kind"] == "max" else omin

    sd = CFG["kernel_sd_c"] * (1.8 if ev["unit"] == "F" else 1.0) * (1 + CFG["sd_growth_per_day"] * days_ahead)

    # Visual Crossing cross-check: blend its point forecast in, and skip the event
    # entirely when it disagrees strongly with the ensemble (models don't agree -> no edge).
    vc_t = None
    vc = visual_crossing(ev["icao"], ev["unit"])
    if vc:
        vc_t = vc[ev["kind"]].get(ev["date"])
    w_vc = CFG.get("vc_weight", 0.25) if vc_t is not None else 0.0
    if vc_t is not None:
        ens_mean = sum(members) / len(members)
        max_gap = CFG.get("vc_max_disagree_c", 2.0) * (1.8 if ev["unit"] == "F" else 1.0)
        if abs(vc_t - ens_mean) > max_gap:
            log.info("skip (VC %.1f vs ensemble %.1f disagree) %s", vc_t, ens_mean, ev["title"])
            return []

    rows = []
    for mk in ev["markets"]:
        if mk.get("closed") or not mk.get("acceptingOrders", True):
            continue
        b = parse_bucket(mk)
        if not b:
            continue
        try:
            toks = json.loads(mk["clobTokenIds"])
        except Exception:
            continue
        p = bucket_prob(members, b[0], b[1], sd, ev["kind"], obs)
        if p is None:
            continue
        if w_vc:
            p = (1 - w_vc) * p + w_vc * bucket_prob([vc_t], b[0], b[1], sd, ev["kind"], obs)
        rows.append({"market": mk, "bucket": b, "p": p, "yes": toks[0], "no": toks[1]})
    if len(rows) < 3:
        return []
    s = sum(r["p"] for r in rows)
    if s <= 0:
        return []
    for r in rows:
        r["p"] /= s  # buckets partition the line -> normalise

    cands = []
    for r in rows:
        mk = r["market"]
        ask_yes = mk.get("bestAsk")
        bid_yes = mk.get("bestBid")
        sides = [("YES", r["yes"], r["p"], float(ask_yes) if ask_yes else None)]
        if CFG["trade_no_side"]:
            sides.append(("NO", r["no"], 1 - r["p"], (1 - float(bid_yes)) if bid_yes else None))
        for side, tok, p, ask in sides:
            if ask is None or ask <= 0:
                continue
            fee = 0.05 * ask * (1 - ask)  # rough pre-filter; exact fee checked later
            edge = p - (ask + fee)
            # edges far above max_edge almost always mean the model is wrong
            # (station bias / bad parse), not that the market is
            if CFG["min_edge"] <= edge <= CFG["max_edge"] and CFG["min_price"] <= ask <= CFG["max_price"] \
                    and p >= CFG["min_model_prob"]:
                cands.append({"event": ev, "market": mk, "side": side, "token": tok, "p": p,
                              "gamma_ask": ask, "bucket": r["bucket"], "obs": obs,
                              "days_ahead": days_ahead, "members": len(members), "sd": sd,
                              "vc": vc_t})
    cands.sort(key=lambda c: c["p"] - c["gamma_ask"], reverse=True)
    return cands


def fmt_bucket(b, unit):
    lo, hi = b
    if lo == -math.inf:
        return f"<= {hi - 0.5:g}°{unit}"
    if hi == math.inf:
        return f">= {lo + 0.5:g}°{unit}"
    a, z = lo + 0.5, hi - 0.5
    return f"{a:g}°{unit}" if a == z else f"{a:g}-{z:g}°{unit}"


def check_bust(positions: dict, bankroll: float, live: bool) -> bool:
    """Equity (cash + open cost) below bust_threshold_usd.
    Paper: archive the run and restart the process with a fresh paper_bankroll.
    Live:  stop buying and alert (real money can't be refilled automatically)."""
    open_cost = sum(p["cost"] for p in positions.values() if p["status"] == "open")
    equity = bankroll + open_cost
    limit = CFG.get("bust_threshold_usd", 20.0)
    if equity >= limit:
        return False
    if live:
        log.error("BUST GUARD: live equity $%.2f < $%.2f — not opening new trades", equity, limit)
        telegram(f"🛑 <b>LIVE equity ${equity:.2f} &lt; ${limit:.0f}</b> — bot stopped opening trades. "
                 f"Check the strategy before adding funds.")
        return True
    arch = DATA_DIR / "archive" / f"bust-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    arch.mkdir(parents=True, exist_ok=True)
    for f in (PAPER_POSITIONS_FILE, PAPER_WALLET):
        if f.exists():
            f.replace(arch / f.name)
    journal("bust", equity=round(equity, 2), threshold=limit, archived=str(arch.relative_to(BOT_DIR)),
            restart_bankroll=CFG["paper_bankroll"])
    log.error("BUST: paper equity $%.2f < $%.2f — archived to %s, restarting with $%.0f",
              equity, limit, arch.relative_to(BOT_DIR), CFG["paper_bankroll"])
    telegram(f"💥 <b>Paper bankroll bust</b>: equity ${equity:.2f} &lt; ${limit:.0f}\n"
             f"Run archived to {arch.relative_to(BOT_DIR)}.\n"
             f"Bot restarted with a fresh ${CFG['paper_bankroll']:.0f}.")
    if "run" in sys.argv:  # real restart of the process
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return True


def scan(dry: bool):
    live = CFG["mode"] == "live" and not dry
    paper = CFG["mode"] != "live" and not dry      # dry/paper mode -> simulated fills
    positions = load_positions()
    settle(positions)

    open_cost = sum(p["cost"] for p in positions.values() if p["status"] == "open")
    bankroll = None
    if live:
        try:
            bankroll = pusd_balance()
        except Exception as e:
            log.error("balance check failed: %s", e)
            return
    elif paper:
        bankroll = paper_balance()
    if bankroll is not None and check_bust(positions, bankroll, live):
        return
    log.info("=== scan | mode=%s | %s=%s | open exposure=$%.2f ===",
             "LIVE" if live else ("PAPER" if paper else "DRY"), "pUSD" if live else "paper$",
             f"{bankroll:.2f}" if bankroll is not None else "n/a", open_cost)

    events = fetch_weather_events()
    if CFG.get("cities"):
        events = [e for e in events if e["city"] in CFG["cities"]]
    log.info("%d temperature events open", len(events))

    held_events = {p["event"] for p in positions.values() if p["status"] == "open"}
    all_cands = []
    for ev in events:
        if ev["slug"] in held_events:
            continue
        try:
            c = evaluate_event(ev)
        except Exception as e:
            log.warning("eval %s failed: %s", ev["slug"], e)
            continue
        if c:
            all_cands.append(c[0])  # best idea per event
    all_cands.sort(key=lambda c: c["p"] - c["gamma_ask"], reverse=True)
    log.info("%d candidate(s) after pre-filter", len(all_cands))

    trades = 0
    for c in all_cands:
        if trades >= CFG["max_trades_per_scan"]:
            break
        ev, mk = c["event"], c["market"]
        ask, ask_size, book = best_ask(c["token"])
        if ask is None:
            continue
        r, e, min_size, _ = fee_rate(mk["conditionId"])
        fee = r * (ask * (1 - ask)) ** e
        cost_per_share = ask + fee
        edge = c["p"] - cost_per_share
        label = f"{ev['title']} | {c['side']} {fmt_bucket(c['bucket'], ev['unit'])}"
        if edge < CFG["min_edge"] or not (CFG["min_price"] <= ask <= CFG["max_price"]):
            log.info("skip (book moved) %s ask=%.3f p=%.3f", label, ask, c["p"])
            continue

        kelly = (c["p"] - cost_per_share) / (1 - cost_per_share)
        base = bankroll if bankroll is not None else CFG["paper_bankroll"]
        usd = min(CFG["max_bet_usd"], CFG["kelly_fraction"] * kelly * base)
        floor = max(CFG["min_bet_usd"], min_size * ask * 1.02)   # exchange minimum
        usd = max(usd, floor)
        if usd > CFG["max_bet_usd"] or usd > ask * ask_size:     # too big for cap / top-of-book
            log.info("skip (size limits) %s need=$%.2f", label, usd)
            continue
        if open_cost + usd > CFG["max_total_exposure_usd"]:
            log.info("skip (exposure cap) %s", label)
            continue
        if bankroll is not None and usd > bankroll - CFG["reserve_usd"]:
            log.info("skip (insufficient pUSD %.2f) %s", bankroll, label)
            continue

        obs_txt = f" obs={c['obs']:.1f}" if c["obs"] is not None else ""
        obs_txt += f" vc={c['vc']:.1f}" if c.get("vc") is not None else " vc=n/a"
        msg = (f"{label} | p={c['p']:.3f} ask={ask:.3f} fee={fee:.4f} edge={edge:+.3f} "
               f"| ${usd:.2f} | D+{c['days_ahead']} n={c['members']} sd={c['sd']:.1f}{obs_txt}")
        if not live and not paper:
            log.info("[DRY] would BUY %s", msg)
            continue

        if paper:
            resp = simulate_fak_buy(book, usd, ask, r, e, min_size)
        else:
            tick = str(book.get("tick_size") or mk.get("orderPriceMinTickSize") or "0.01")
            try:
                resp = place_buy(c["token"], usd, ask, ev["neg_risk"], tick)
            except Exception as ex:
                log.error("order failed %s: %s", label, ex)
                telegram(f"❌ Order failed: {label}\n{ex}")
                continue
        ok = isinstance(resp, dict) and resp.get("success") and resp.get("status") in ("matched", "delayed", "live")
        spent = float(resp.get("makingAmount") or 0) if isinstance(resp, dict) else 0
        shares = float(resp.get("takingAmount") or 0) if isinstance(resp, dict) else 0
        if not ok or shares <= 0:
            log.warning("no fill %s resp=%s", label, resp)
            continue
        trades += 1
        open_cost += spent
        bankroll -= spent
        if paper:
            paper_adjust(-spent)
        pid = resp.get("orderID", f"{mk['id']}-{time.time()}")
        positions[pid] = {
            "event": ev["slug"], "market_id": mk["id"], "title": label, "side": c["side"],
            "token": c["token"], "p": round(c["p"], 4), "price": round(spent / shares, 4),
            "shares": shares, "cost": spent, "opened": datetime.now(timezone.utc).isoformat(),
            "status": "open", "pnl": None, "paper": paper,
            "city": ev["city"], "kind": ev["kind"], "unit": ev["unit"], "date": ev["date"],
            "days_ahead": c["days_ahead"], "edge": round(edge, 4), "vc": c.get("vc"), "obs": c["obs"],
        }
        save_positions(positions)
        journal("buy", id=pid, **{k: positions[pid][k] for k in (
            "event", "title", "side", "p", "price", "shares", "cost", "city", "kind", "unit",
            "date", "days_ahead", "edge", "vc", "obs", "paper")})
        tag = "📝 PAPER BUY" if paper else "✅ BUY"
        log.info("%s %s | filled %.2f sh for $%.2f", "📝 PAPER BOUGHT" if paper else "✅ BOUGHT",
                 msg, shares, spent)
        telegram(f"{tag} {label}\nmodel p={c['p']:.2f} vs price {spent / shares:.3f} "
                 f"(edge {edge:+.2f})\n{shares:.2f} sh for ${spent:.2f}")
    log.info("=== scan done: %d trade(s) ===", trades)


def settle(positions: dict):
    changed = False
    for pid, p in positions.items():
        if p["status"] != "open":
            continue
        m = get_json(f"{GAMMA}/markets/{p['market_id']}", timeout=10)
        if not m or not m.get("closed"):
            continue
        try:
            prices = [float(x) for x in json.loads(m["outcomePrices"])]
        except Exception:
            continue
        won_yes = prices[0] > 0.5
        win = won_yes if p["side"] == "YES" else not won_yes
        p["status"] = "won" if win else "lost"
        p["pnl"] = round((p["shares"] if win else 0) - p["cost"], 4)
        p["settled"] = datetime.now(timezone.utc).isoformat()
        changed = True
        if p.get("paper") and win:
            paper_adjust(p["shares"])  # each winning share pays $1
        journal("settle", id=pid, title=p["title"], event=p["event"], side=p["side"],
                status=p["status"], pnl=p["pnl"], cost=p["cost"], p=p["p"], price=p["price"],
                city=p.get("city"), kind=p.get("kind"), days_ahead=p.get("days_ahead"),
                edge=p.get("edge"), vc=p.get("vc"), paper=p.get("paper", False))
        tag = "PAPER " if p.get("paper") else ""
        log.info("SETTLED %s%s -> %s pnl=%+.2f", tag, p["title"], p["status"], p["pnl"])
        telegram(f"{'🏆' if win else '💀'} {tag}{p['title']} → {p['status'].upper()} (PnL {p['pnl']:+.2f})")
    if changed:
        save_positions(positions)


def status():
    positions = load_positions()
    settle(positions)
    if CFG["mode"] != "live":
        print(f"mode={CFG['mode']} (paper)  paper balance: ${paper_balance():.2f} "
              f"(start ${CFG['paper_bankroll']:.0f})")
    try:
        print(f"pUSD balance (deposit wallet {FUNDER[:6]}…{FUNDER[-4:]}): ${pusd_balance():.2f}")
    except Exception as e:
        print("balance check failed:", e)
    done = [p for p in positions.values() if p["status"] != "open"]
    opn = [p for p in positions.values() if p["status"] == "open"]
    print(f"open positions: {len(opn)}  cost ${sum(p['cost'] for p in opn):.2f}")
    for p in opn:
        print(f"  {p['title']} | {p['shares']:.2f} sh @ {p['price']:.3f}")
    if done:
        w = sum(1 for p in done if p["status"] == "won")
        print(f"settled: {len(done)}  won {w}  PnL ${sum(p['pnl'] for p in done):+.2f}")


def run():
    log.info("weatherbot starting | mode=%s | funder=%s…%s", CFG["mode"], FUNDER[:6], FUNDER[-4:])
    telegram(f"🌦 Weatherbot started (mode={CFG['mode']})")
    while True:
        try:
            scan(dry=False)
        except Exception as e:
            log.exception("scan crashed: %s", e)
        time.sleep(CFG["scan_interval_sec"])


if __name__ == "__main__":
    if not PK or not FUNDER:
        sys.exit("Set PK and WALLET/FUNDER in .env")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "run":
        run()
    elif cmd == "scan":
        scan(dry="--dry" in sys.argv)
    elif cmd == "status":
        status()
    else:
        print(__doc__)
