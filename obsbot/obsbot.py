#!/usr/bin/env python3
"""
obsbot — Polymarket weather *observation arbitrage* (paper first)
=================================================================
Separate from weatherbot.py (own config, data, log, paper wallet). Shares .env and venv.

Idea: don't forecast. Find temperature buckets whose outcome the REAL WORLD has already
decided (or nearly decided) while Polymarket still prices them as uncertain.

Real-world data = METAR reports from the resolution station named in each market's rules
(the same observations NOAA timeseries / Wunderground publish).

Tiers (how sure we are):
  hard   Today's running extreme already rules a bucket out.
         Highest temp can only go UP: every bucket entirely below the observed max is dead (NO),
         and "X or higher" is already won (YES) once obs >= X. Mirror for lowest temp.
  final  The station's local day is over: the extreme is known. Winning bucket YES, rest NO.
  late   Heuristic: late in the day and the bias-corrected hourly forecast says the extreme
         won't move enough to reach a bucket. Starts untrusted; trades only after it has proven itself.

Self-learning: every qualifying signal (traded or not) is recorded and graded when the market
resolves. Win/loss counts per (tier, kind, side, margin band[, hour]) form a Beta posterior; the bot
only buys when the posterior *lower bound* (quantile `confidence_q`) beats price + fee + min_edge.
Bets are sized from that same lower bound. A hard/final loss blocks the station (data mismatch).

Usage (from repo root or obsbot/):
  venv/bin/python obsbot/obsbot.py run         # loop forever
  venv/bin/python obsbot/obsbot.py scan        # one pass (paper fills)
  venv/bin/python obsbot/obsbot.py scan --dry  # one pass, log only
  venv/bin/python obsbot/obsbot.py status      # wallet, positions, learned stats
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
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extra  # noqa: E402  non-temperature markets

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent
DATA = BOT_DIR / "data"
DATA.mkdir(exist_ok=True)
CFG_FILE = BOT_DIR / "config.json"
CFG = json.loads(CFG_FILE.read_text())

POSITIONS = DATA / "paper_positions.json"
WALLET = DATA / "paper_wallet.json"
SIGNALS = DATA / "signals.json"        # pending shadow signals awaiting resolution
LEARNED = DATA / "learned.json"        # Beta stats + blocked stations
JOURNAL = DATA / "trades.jsonl"        # append-only: buy | settle | learn | block
STATIONS = DATA / "stations.json"      # lat/lon/tz cache

load_dotenv(ROOT / ".env")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
AWC = "https://aviationweather.gov/api/data"

log = logging.getLogger("obsbot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = RotatingFileHandler(DATA / "obsbot.log", maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(_fmt)
log.addHandler(_fh)
if sys.stdout.isatty() or "--stdout" in sys.argv or "run" not in sys.argv:
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "obsbot/1.0"


def get_json(url, params=None, timeout=20, tries=3, method="GET", body=None):
    for i in range(tries):
        try:
            if method == "POST":
                r = HTTP.post(url, json=body, timeout=timeout)
            else:
                r = HTTP.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == tries - 1:
                log.warning("%s %s failed: %s", method, url, e)
            time.sleep(1.5 * (i + 1))
    return None


def telegram(msg: str):
    """Name kept for the many call sites; the message now goes to Discord #bot-reports."""
    if not CFG.get("telegram", True):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from discord_notify import send
        send("🔭 obsbot · " + msg)
    except Exception as e:
        log.warning("discord notify failed: %s", e)


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path: Path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def journal(kind: str, **rec):
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "type": kind, **rec}
    with open(JOURNAL, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


# =========================================================================== markets
SLUG_RE = re.compile(r"^(highest|lowest)-temperature-in-(.+)-on-([a-z]+)-(\d{1,2})-(\d{4})$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
NUM = r"(-?\d+(?:\.\d+)?)"


def parse_station(desc: str):
    m = re.search(r"timeseries\?site=([A-Za-z0-9]{4})", desc)
    if m:
        return m.group(1).upper()
    m = re.search(r"wunderground\.com/history/daily/[a-z]+/[^/\s]+/([A-Z0-9]{4})", desc)
    return m.group(1).upper() if m else None


def parse_bucket(mk: dict):
    """Continuous [lo, hi) in market units (whole-degree rounding => [x-0.5, y+0.5))."""
    t = (mk.get("groupItemTitle") or "").strip()
    q = mk.get("question") or ""
    for s in (t, q):
        m = re.search(NUM + r"\s*°?\s*[FC]?\s*or below", s, re.I)
        if m:
            return (-math.inf, float(m.group(1)) + 0.5)
        m = re.search(NUM + r"\s*°?\s*[FC]?\s*or (higher|above)", s, re.I)
        if m:
            return (float(m.group(1)) - 0.5, math.inf)
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


def fmt_bucket(b, unit):
    lo, hi = b
    if lo == -math.inf:
        return f"<={hi - 0.5:g}°{unit}"
    if hi == math.inf:
        return f">={lo + 0.5:g}°{unit}"
    a, z = lo + 0.5, hi - 0.5
    return f"{a:g}°{unit}" if a == z else f"{a:g}-{z:g}°{unit}"


def fetch_weather_events():
    """All open events under the Weather tag. Returns (temperature_events, other_raw_events)."""
    events, offset = [], 0
    while True:
        batch = get_json(f"{GAMMA}/events", {"tag_slug": "weather", "closed": "false",
                                             "limit": 100, "offset": offset}, timeout=30)
        if not batch:
            break
        events += batch
        if len(batch) < 100 or offset > 5000:
            break
        offset += 100
    temps, other = [], []
    for e in events:
        m = SLUG_RE.match(e.get("slug", ""))
        if not m or m.group(3) not in MONTHS:
            other.append(e)
            continue
        kind, city, mon, day, year = m.groups()
        desc = e.get("description") or (e.get("markets") or [{}])[0].get("description", "")
        um = re.search(r"in degrees (Fahrenheit|Celsius)", desc)
        if um:
            unit = um.group(1)[0]
        else:
            labels = " ".join(x.get("groupItemTitle") or "" for x in e.get("markets", []))
            unit = "F" if "°F" in labels else "C"
        buckets = []
        for mk in e.get("markets", []):
            if mk.get("closed") or not mk.get("acceptingOrders", True):
                continue
            b = parse_bucket(mk)
            try:
                yes, no = json.loads(mk["clobTokenIds"])
            except Exception:
                continue
            if b:
                buckets.append({"id": mk["id"], "cid": mk.get("conditionId"), "b": b,
                                "yes": yes, "no": no, "label": mk.get("groupItemTitle", "")})
        temps.append({"slug": e["slug"], "title": e.get("title", ""),
                      "kind": "max" if kind == "highest" else "min", "city": city,
                      "date": f"{year}-{MONTHS[mon]:02d}-{int(day):02d}", "icao": parse_station(desc),
                      "unit": unit, "neg_risk": bool(e.get("negRisk")), "buckets": buckets})
    return temps, other


# =========================================================================== real-world data
_stations = load(STATIONS, {})


def station(icao: str):
    s = _stations.get(icao)
    if s and "tz" in s:
        return s
    d = get_json(f"{AWC}/stationinfo", {"ids": icao, "format": "json"})
    if not d:
        return None
    lat, lon = d[0]["lat"], d[0]["lon"]
    fc = get_json("https://api.open-meteo.com/v1/forecast",
                  {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
                   "current": "temperature_2m"})
    if not fc or not fc.get("timezone"):
        return None
    _stations[icao] = {"lat": lat, "lon": lon, "tz": fc["timezone"]}
    save(STATIONS, _stations)
    return _stations[icao]


def fetch_metars(icaos: list[str], hours: int = 50) -> dict:
    """{icao: [(utc_datetime, temp_c), ...]} sorted. AWC caps a response at 400 rows -> chunk."""
    out = {i: [] for i in icaos}

    def pull(ids):
        d = get_json(f"{AWC}/metar", {"ids": ",".join(ids), "format": "json", "hours": hours},
                     timeout=40)
        if d is None:
            return None
        if len(d) >= 400 and len(ids) > 1:     # truncated: split
            for i in ids:
                pull([i])
            return True
        for ob in d:
            if ob.get("temp") is None or ob.get("obsTime") is None:
                continue
            out.setdefault(ob["icaoId"], []).append(
                (datetime.fromtimestamp(ob["obsTime"], timezone.utc), float(ob["temp"])))
        return True

    for k in range(0, len(icaos), 3):
        pull(icaos[k:k + 3])
    for i in out:
        out[i] = sorted(set(out[i]))
    return out


_hourly_cache: dict = {}


def hourly_forecast(icao: str):
    """{local_iso_hour: temp_c} from Open-Meteo best-match, cached 1h."""
    hit = _hourly_cache.get(icao)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    st = station(icao)
    if not st:
        return None
    d = get_json("https://api.open-meteo.com/v1/forecast",
                 {"latitude": st["lat"], "longitude": st["lon"], "hourly": "temperature_2m",
                  "timezone": st["tz"], "forecast_days": 2, "past_days": 1})
    if not d or "hourly" not in d:
        return None
    res = dict(zip(d["hourly"]["time"], d["hourly"]["temperature_2m"]))
    _hourly_cache[icao] = (time.time(), res)
    return res


def to_unit(c: float, unit: str) -> float:
    return c * 9 / 5 + 32 if unit == "F" else c


def day_summary(obs, tz: str, date: str):
    """Observations on the station's local calendar `date`."""
    z = ZoneInfo(tz)
    day = [(t.astimezone(z), c) for t, c in obs if t.astimezone(z).strftime("%Y-%m-%d") == date]
    now_local = datetime.now(z)
    if not day:
        return None
    hi_t, hi = max(day, key=lambda x: x[1])
    lo_t, lo = min(day, key=lambda x: x[1])
    times = [t for t, _ in day]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(times, times[1:])]
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=z)
    end = start + timedelta(days=1)
    edge_gap = max((times[0] - start).total_seconds(), 0) / 3600

    def near_midnight(t):  # DST / local-day ambiguity guard
        return (t - start).total_seconds() < 3600 or (end - t).total_seconds() < 3600

    return {"n": len(day), "max": hi, "min": lo, "max_t": hi_t, "min_t": lo_t,
            "max_near_mid": near_midnight(hi_t), "min_near_mid": near_midnight(lo_t),
            "max_gap_h": max(gaps + [edge_gap]), "last": day[-1], "now": now_local,
            "complete": now_local >= end + timedelta(minutes=CFG["final_delay_min"]),
            "is_today": now_local.strftime("%Y-%m-%d") == date}


# =========================================================================== learning
def betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c; c = c if abs(c) > 1e-30 else 1e-30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c; c = c if abs(c) > 1e-30 else 1e-30
        de = d * c
        h *= de
        if abs(de - 1) < 3e-12:
            break
    return h


def beta_cdf(x, a, b):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * betacf(a, b, x) / a
    return 1 - math.exp(lbt) * betacf(b, a, 1 - x) / b


def beta_quantile(q, a, b):
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


LEARN = load(LEARNED, {"stats": {}, "blocked": {}})


def learn_key(tier, kind, side, band, hour=None):
    k = f"{tier}|{kind}|{side}|{band}"
    return k + (f"|h{hour}" if hour is not None else "")


def posterior(key: str, tier: str):
    """(lower_bound_p, mean_p, wins, losses) for a learning key."""
    a0, b0 = CFG["priors"][tier]
    w, l = LEARN["stats"].get(key, [0, 0])
    a, b = a0 + w, b0 + l
    return beta_quantile(CFG["confidence_q"], a, b), a / (a + b), w, l


def margin_band(m_c: float) -> str:
    return "m<1" if m_c < 1 else ("m1-2" if m_c < 2 else "m2+")


# =========================================================================== signal engine
def signals_for_event(ev, obs_by_station):
    """Real-world-certain (or learned-likely) outcomes for each bucket of one event."""
    if not ev["icao"] or ev["icao"] in LEARN["blocked"] or len(ev["buckets"]) < 2:
        return []
    st = station(ev["icao"])
    if not st:
        return []
    s = day_summary(obs_by_station.get(ev["icao"], []), st["tz"], ev["date"])
    if not s:
        return []
    kind, unit = ev["kind"], ev["unit"]
    k_deg = 1.8 if unit == "F" else 1.0          # °C margin -> market units
    ext_c = s["max"] if kind == "max" else s["min"]
    near_mid = s["max_near_mid"] if kind == "max" else s["min_near_mid"]
    v = to_unit(ext_c, unit)
    out = []

    def add(tier, bk, side, margin_c, extra=None, hour=None):
        out.append({"tier": tier, "bucket": bk, "side": side, "margin_c": round(margin_c, 2),
                    "band": margin_band(margin_c), "obs": round(v, 2), "hour": hour,
                    "n_obs": s["n"], **(extra or {})})

    if s["complete"]:
        # ---- final: local day over, extreme is known
        if near_mid or s["n"] < CFG["final_min_obs"] or s["max_gap_h"] > CFG["final_max_gap_h"]:
            return []
        # the true extreme can lie beyond what the reports caught (peak between METARs):
        # for highs only upward misses are possible, for lows only downward ones
        m = CFG["final_margin_c"] * k_deg
        u = CFG["final_unseen_c"] * k_deg
        up, down = (m + u, m) if kind == "max" else (m, m + u)
        for bk in ev["buckets"]:
            lo, hi = bk["b"]
            if lo + down <= v < hi - up:
                add("final", bk, "YES", min(v - lo - down, hi - v - up) / k_deg + CFG["final_margin_c"])
            elif hi <= v - down:
                add("final", bk, "NO", (v - hi) / k_deg)
            elif lo >= v + up:
                add("final", bk, "NO", (lo - v) / k_deg)
        return out

    if not s["is_today"] or near_mid:
        return []                                 # future day or ambiguous extreme

    # ---- hard: running extreme bounds the final value
    m = CFG["hard_margin_c"] * k_deg
    for bk in ev["buckets"]:
        lo, hi = bk["b"]
        if kind == "max":
            if hi <= v - m:
                add("hard", bk, "NO", (v - hi) / k_deg)
            elif hi == math.inf and lo <= v - m:
                add("hard", bk, "YES", (v - lo) / k_deg)
        else:
            if lo >= v + m:
                add("hard", bk, "NO", (lo - v) / k_deg)
            elif lo == -math.inf and hi >= v + m:
                add("hard", bk, "YES", (hi - v) / k_deg)

    # ---- late: forecast for the rest of the day can't reach the bucket
    if not CFG.get("late_enabled", True):
        return out
    hour = s["now"].hour
    if hour < CFG["late_min_hour"][kind]:
        return out
    fc = hourly_forecast(ev["icao"])
    if not fc:
        return out
    now_key = s["now"].strftime("%Y-%m-%dT%H:00")
    if now_key not in fc or fc[now_key] is None:
        return out
    bias = s["last"][1] - fc[now_key]           # station vs model right now (°C)
    rest = [t for k, t in fc.items() if k[:10] == ev["date"] and k > now_key and t is not None]
    if not rest:
        return out
    rest_c = (max(rest) if kind == "max" else min(rest)) + bias
    reach = to_unit(max(ext_c, rest_c) if kind == "max" else min(ext_c, rest_c), unit)
    m = CFG["late_margin_c"] * k_deg
    hard_ids = {(x["bucket"]["id"], x["side"]) for x in out}
    hm = CFG["hard_margin_c"] * k_deg
    for bk in ev["buckets"]:
        lo, hi = bk["b"]
        extra = {"fc_rest": round(to_unit(rest_c, unit), 1), "bias_c": round(bias, 1)}
        if kind == "max":
            if lo >= reach + m:
                add("late", bk, "NO", (lo - reach) / k_deg, extra, hour)
            elif lo <= v - hm and hi >= reach + m and hi != math.inf:
                add("late", bk, "YES", (hi - reach) / k_deg, extra, hour)
        else:
            if hi <= reach - m and hi != math.inf and (bk["id"], "NO") not in hard_ids:
                add("late", bk, "NO", (reach - hi) / k_deg, extra, hour)
            elif hi >= v + hm and lo <= reach - m and lo != -math.inf:
                add("late", bk, "YES", (reach - lo) / k_deg, extra, hour)
    return out


# =========================================================================== order book / paper fills
_fee_cache: dict = {}


def fee_info(cid: str):
    if cid in _fee_cache:
        return _fee_cache[cid]
    info = get_json(f"{CLOB}/clob-markets/{cid}", timeout=10) or {}
    fd = info.get("fd") or {}
    res = (float(fd.get("r", 0.05)), float(fd.get("e", 1.0)), float(info.get("mos", 5)))
    _fee_cache[cid] = res
    return res


def books(tokens: list[str]) -> dict:
    out = {}
    for k in range(0, len(tokens), 50):
        d = get_json(f"{CLOB}/books", method="POST", body=[{"token_id": t} for t in tokens[k:k + 50]],
                     timeout=20)
        for b in d or []:
            out[b.get("asset_id")] = b
    return out


def simulate_buy(book, usd, limit, r, e, min_size):
    """Walk asks up to `limit` like a real FAK market BUY; fee = r*(p(1-p))^e per share."""
    asks = sorted(((float(a["price"]), float(a["size"])) for a in book.get("asks", [])))
    budget, shares, notional, fees = usd, 0.0, 0.0, 0.0
    for px, sz in asks:
        if px > limit + 1e-9 or budget <= 0.005:
            break
        per = px + r * (px * (1 - px)) ** e
        take = math.floor(min(sz, budget / per) * 100) / 100
        if take <= 0:
            break
        shares += take
        notional += take * px
        fees += take * (per - px)
        budget -= take * per
    if shares < min_size:
        return None
    return {"shares": round(shares, 2), "spent": round(notional + fees, 6), "fee": round(fees, 6),
            "avg": (notional + fees) / shares}


def wallet():
    w = load(WALLET, None)
    if not w:
        w = {"balance": CFG["paper_bankroll"], "start": CFG["paper_bankroll"]}
        save(WALLET, w)
    return w


def wallet_adjust(delta):
    w = wallet()
    w["balance"] = round(w["balance"] + delta, 6)
    save(WALLET, w)


# =========================================================================== resolution + learning
def market_results(ids: list[str]) -> dict:
    """{market_id: True if YES won / False if NO won} for resolved markets."""
    res = {}
    for k in range(0, len(ids), 40):
        params = [("id", i) for i in ids[k:k + 40]] + [("closed", "true"), ("limit", 100)]
        for m in get_json(f"{GAMMA}/markets", params, timeout=30) or []:
            try:
                p = [float(x) for x in json.loads(m["outcomePrices"])]
            except Exception:
                continue
            if max(p) >= 0.99:   # fully resolved
                res[str(m["id"])] = p[0] > 0.5
    return res


def settle_all():
    positions = load(POSITIONS, {})
    sigs = load(SIGNALS, {})
    ids = sorted({str(p["market_id"]) for p in positions.values() if p["status"] == "open"} |
                 {str(s["market_id"]) for s in sigs.values()})
    if not ids:
        return
    res = market_results(ids)
    if not res:
        return

    # positions -> paper PnL
    changed = False
    for pid, p in positions.items():
        if p["status"] != "open" or str(p["market_id"]) not in res:
            continue
        yes_won = res[str(p["market_id"])]
        win = yes_won if p["side"] == "YES" else not yes_won
        p.update(status="won" if win else "lost", settled=datetime.now(timezone.utc).isoformat(),
                 pnl=round((p["shares"] if win else 0) - p["cost"], 4))
        if win:
            wallet_adjust(p["shares"])
        changed = True
        journal("settle", id=pid, title=p["title"], tier=p["tier"], side=p["side"], status=p["status"],
                pnl=p["pnl"], cost=p["cost"], p_lb=p["p_lb"], price=p["price"])
        log.info("SETTLED %s [%s] -> %s pnl=%+.2f", p["title"], p["tier"], p["status"], p["pnl"])
        telegram(f"{'🏆' if win else '💀'} {p['title']} [{p['tier']}] → {p['status'].upper()} "
                 f"(PnL {p['pnl']:+.2f})")
    if changed:
        save(POSITIONS, positions)

    # shadow signals -> learning. Unit = (event, learn key): wins only if every signal in it won,
    # so one bad station-day counts once per key rather than 8x.
    groups: dict = {}
    done = []
    for sid, s in sigs.items():
        if str(s["market_id"]) not in res:
            continue
        yes_won = res[str(s["market_id"])]
        ok = yes_won if s["side"] == "YES" else not yes_won
        g = groups.setdefault((s["event"], s["key"]), {"ok": True, "s": s, "bad": []})
        g["ok"] &= ok
        if not ok:
            g["bad"].append(s)
        done.append(sid)
    pending_events = {(s["event"], s["key"]) for sid, s in sigs.items() if sid not in done}
    for (event, key), g in groups.items():
        if (event, key) in pending_events:      # wait until every bucket of the group resolves
            continue
        st = LEARN["stats"].setdefault(key, [0, 0])
        st[0 if g["ok"] else 1] += 1
        journal("learn", event=event, key=key, ok=g["ok"], wins=st[0], losses=st[1])
        if not g["ok"]:
            s = g["bad"][0]
            log.warning("LEARN: %s failed on %s (%s %s obs=%s) -> %s", key, event, s["side"],
                        s["label"], s["obs"], st)
            if s["tier"] in CFG["block_tiers"]:
                LEARN["blocked"][s["icao"]] = (f"{s['tier']} signal lost on {event} "
                                               f"({s['side']} {s['label']}, obs {s['obs']})")
                journal("block", icao=s["icao"], reason=LEARN["blocked"][s["icao"]])
                telegram(f"⚠️ Station {s['icao']} blocked: {LEARN['blocked'][s['icao']]}. "
                         f"Real-world data disagreed with the resolution source.")
    for sid in done:
        if (sigs[sid]["event"], sigs[sid]["key"]) not in pending_events:
            LEARN.setdefault("done", {})[sid] = 1   # never re-learn a settled signal
            sigs.pop(sid)
    save(SIGNALS, sigs)
    save(LEARNED, LEARN)


# =========================================================================== scan
def scan(dry: bool):
    t0 = time.time()
    settle_all()
    positions = load(POSITIONS, {})
    sigs = load(SIGNALS, {})
    bal = wallet()["balance"]
    open_pos = [p for p in positions.values() if p["status"] == "open"]
    exposure = sum(p["cost"] for p in open_pos)
    log.info("=== scan | mode=%s | paper$=%.2f | exposure=$%.2f | open=%d ===",
             "DRY" if dry else "PAPER", bal, exposure, len(open_pos))

    events, other = fetch_weather_events()
    if CFG.get("cities"):
        events = [e for e in events if e["city"] in CFG["cities"]]
    icaos = sorted({e["icao"] for e in events if e["icao"] and e["icao"] not in LEARN["blocked"]})
    for i in icaos:
        station(i)
    obs = fetch_metars([i for i in icaos if i in _stations])
    log.info("%d temperature events | %d other weather events | %d stations, %d METARs",
             len(events), len(other), len(icaos), sum(len(v) for v in obs.values()))

    # 1) find real-world vs market gaps
    pairs = []
    for ev in events:
        try:
            pairs.append((ev, signals_for_event(ev, obs)))
        except Exception as e:
            log.warning("signal %s failed: %s", ev["slug"], e)
    xp = [(ev, ss) for ev, ss in extra.all_signals(sys.modules[__name__], other)
          if ev["icao"] not in LEARN["blocked"]]
    log.info("extra markets modelled: %d (%s)", len(xp),
             ", ".join(f"{k}={sum(1 for e, _ in xp if e['kind'] == k)}"
                       for k in sorted({e["kind"] for e, _ in xp})))
    pairs += xp
    cands = []
    new_sigs = 0
    for ev, ss in pairs:
        for s in ss:
            bk = s["bucket"]
            key = learn_key(s["tier"], ev["kind"], s["side"], s["band"],
                            s["hour"] if s["tier"] == "late" else None)
            sid = f"{bk['id']}|{s['side']}|{s['tier']}"
            if sid not in sigs and sid not in LEARN.setdefault("done", {}):  # shadow record for learning (first sighting)
                sigs[sid] = {"event": ev["slug"], "market_id": bk["id"], "side": s["side"],
                             "tier": s["tier"], "key": key, "icao": ev["icao"], "label": bk["label"],
                             "obs": s["obs"], "margin_c": s["margin_c"],
                             "seen": datetime.now(timezone.utc).isoformat()}
                new_sigs += 1
            lb, mean, w, l = posterior(key, s["tier"])
            cands.append({**s, "ev": ev, "key": key, "lb": lb, "mean": mean, "w": w, "l": l,
                          "token": bk["yes"] if s["side"] == "YES" else bk["no"]})
    save(SIGNALS, sigs)

    # 2) price check against the live order book
    held = {p["token"] for p in open_pos}
    toks = list({c["token"] for c in cands if c["token"] not in held})
    bk_map = books(toks) if toks else {}
    priced = []
    for c in cands:
        b = bk_map.get(c["token"])
        if not b or not b.get("asks"):
            continue
        ask = min(float(a["price"]) for a in b["asks"])
        r, e, mos = fee_info(c["bucket"]["cid"])
        cost = ask + r * (ask * (1 - ask)) ** e
        edge = c["lb"] - cost
        c.update(ask=ask, cost=cost, edge=edge, book=b, fee_r=r, fee_e=e, mos=mos)
        priced.append(c)
    gaps = [c for c in priced if c["edge"] >= CFG["min_edge"] and c["ask"] <= CFG["max_price"]]
    gaps.sort(key=lambda c: c["edge"], reverse=True)
    by_tier = {}
    for c in cands:
        by_tier[c["tier"]] = by_tier.get(c["tier"], 0) + 1
    log.info("%d real-world signals %s (%d new) | %d with asks | %d mispriced (edge>=%.3f)",
             len(cands), by_tier, new_sigs, len(priced), len(gaps), CFG["min_edge"])

    # 3) paper trade
    ev_exposure: dict = {}
    for p in open_pos:
        ev_exposure[p["event"]] = ev_exposure.get(p["event"], 0) + p["cost"]
    trades = 0
    for c in gaps:
        ev = c["ev"]
        if ev.get("generic"):
            label = f"{ev['title']} | {c['side']} {c['bucket']['label']}"
            obs_txt = f"obs={c['obs']}{ev['usym']} margin={c['margin_c']} steps"
        else:
            label = f"{ev['title']} | {c['side']} {fmt_bucket(c['bucket']['b'], ev['unit'])}"
            obs_txt = f"obs={c['obs']}{ev['unit']} margin={c['margin_c']}°C"
        info = (f"[{c['tier']}] p_lb={c['lb']:.3f} (n={c['w']}/{c['w'] + c['l']}) ask={c['ask']:.3f} "
                f"edge={c['edge']:+.3f} {obs_txt}"
                + (f" fc_rest={c.get('fc_rest')}" if c["tier"] == "late" else ""))
        if c["token"] in held or trades >= CFG["max_trades_per_scan"]:
            continue
        kelly = (c["lb"] - c["cost"]) / (1 - c["cost"])
        usd = min(CFG["max_bet_usd"], CFG["kelly_fraction"] * kelly * bal,
                  CFG["max_event_exposure_usd"] - ev_exposure.get(ev["slug"], 0),
                  CFG["max_total_exposure_usd"] - exposure, bal - CFG["reserve_usd"])
        if usd < max(CFG["min_bet_usd"], c["mos"] * c["ask"] * 1.02):
            log.info("[GAP] %s | %s | skip: size $%.2f below minimum", label, info, max(usd, 0))
            continue
        limit = min(CFG["max_price"], c["lb"] - CFG["min_edge"])   # never pay through our edge
        if dry:
            log.info("[DRY] would BUY %s | %s | $%.2f", label, info, usd)
            continue
        fill = simulate_buy(c["book"], usd, limit, c["fee_r"], c["fee_e"], c["mos"])
        if not fill:
            log.info("[GAP] %s | %s | skip: not enough size <= %.3f", label, info, limit)
            continue
        trades += 1
        bal -= fill["spent"]
        exposure += fill["spent"]
        ev_exposure[ev["slug"]] = ev_exposure.get(ev["slug"], 0) + fill["spent"]
        wallet_adjust(-fill["spent"])
        held.add(c["token"])
        pid = f"paper-{int(time.time() * 1000)}-{trades}"
        positions[pid] = {
            "event": ev["slug"], "market_id": c["bucket"]["id"], "token": c["token"], "title": label,
            "side": c["side"], "tier": c["tier"], "key": c["key"], "p_lb": round(c["lb"], 4),
            "price": round(fill["avg"], 4), "shares": fill["shares"], "cost": fill["spent"],
            "obs": c["obs"], "margin_c": c["margin_c"], "icao": ev["icao"], "date": ev["date"],
            "opened": datetime.now(timezone.utc).isoformat(), "status": "open", "pnl": None}
        save(POSITIONS, positions)
        journal("buy", id=pid, **{k: positions[pid][k] for k in (
            "event", "title", "side", "tier", "key", "p_lb", "price", "shares", "cost", "obs",
            "margin_c", "icao")})
        log.info("📝 PAPER BOUGHT %s | %s | %.2f sh for $%.2f", label, info, fill["shares"], fill["spent"])
        telegram(f"📝 PAPER BUY {label}\n[{c['tier']}] real obs {c['obs']}"
                 f"{ev['usym'] if ev.get('generic') else '°' + ev['unit']} · "
                 f"p≥{c['lb']:.3f} vs {fill['avg']:.3f}\n{fill['shares']:.2f} sh for ${fill['spent']:.2f}")
    log.info("=== scan done: %d trade(s) in %.0fs ===", trades, time.time() - t0)


def status():
    settle_all()
    w = wallet()
    pos = load(POSITIONS, {})
    opn = [p for p in pos.values() if p["status"] == "open"]
    done = [p for p in pos.values() if p["status"] != "open"]
    print(f"paper balance ${w['balance']:.2f} (start ${w['start']:.0f}) | open {len(opn)} "
          f"cost ${sum(p['cost'] for p in opn):.2f}")
    for p in opn:
        print(f"  [{p['tier']}] {p['title']} | {p['shares']:.2f} sh @ {p['price']:.3f}")
    if done:
        print(f"settled {len(done)} won {sum(p['status'] == 'won' for p in done)} "
              f"PnL ${sum(p['pnl'] for p in done):+.2f}")
    print(f"pending shadow signals: {len(load(SIGNALS, {}))}")
    print("learned (key: wins/losses -> p_lb):")
    for k, (wn, ls) in sorted(LEARN["stats"].items()):
        print(f"  {k}: {wn}/{ls} -> {posterior(k, k.split('|')[0])[0]:.3f}")
    if LEARN["blocked"]:
        print("blocked stations:", LEARN["blocked"])


def run():
    log.info("obsbot starting (paper) | interval %ss", CFG["scan_interval_sec"])
    telegram("started (paper)")
    while True:
        try:
            scan(dry=False)
        except Exception as e:
            log.exception("scan crashed: %s", e)
        time.sleep(CFG["scan_interval_sec"])


if __name__ == "__main__":
    if CFG.get("mode", "paper") != "paper":
        sys.exit("obsbot is paper-only. Set \"mode\": \"paper\" in obsbot/config.json.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "run":
        run()
    elif cmd == "scan":
        scan(dry="--dry" in sys.argv)
    elif cmd == "status":
        status()
    else:
        print(__doc__)
