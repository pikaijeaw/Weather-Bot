"""
obsbot extension: non-temperature Weather markets whose outcome only moves one way.

Each handler turns a Gamma event into pseudo-events + signals in the same shape obsbot's
temperature engine uses, so pricing, paper fills, settlement and learning are shared.

  quake       USGS catalog. Counts and maxima within a window can only grow:
              how-many-<M>-or-above-earthquakes(-worldwide)-<window>, biggest-earthquake-<date>,
              highest-earthquake-magnitude-in-<year>, how-strong-of-an-earthquake-...-by-<date>.
              Revision safety: count only quakes >= threshold + quake_mag_margin, max - margin.
  rain_month  NWS Daily Climate Report (CLI) month-to-date total (precipitation-in-nyc/seattle).
              Total only grows -> buckets below it are dead.  rain_month_final once the month's
              last report is final.
  rain_cli    where-will-it-rain-on-<date>: a CLI report (even preliminary) with >= 0.01" -> YES.
  rain_cli_final  final CLI (issued after local-standard-time midnight) with 0 / T -> NO.
  rain_metar  hourly METAR P-groups in the climate day sum >= rain_metar_min_in -> YES (untrusted
              prior; must prove itself).
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
# Seattle resolves on NOWData "Seattle City Area" (not KSEA CLI) -> excluded until a matching feed exists
RAIN_MONTH_STATIONS = {"nyc": "KNYC"}

_usgs = {"t": 0, "q": None}
_cli: dict = {}


# --------------------------------------------------------------------------- helpers
def open_buckets(e, parse):
    out = []
    for mk in e.get("markets", []):
        if mk.get("closed") or not mk.get("acceptingOrders", True):
            continue
        try:
            yes, no = json.loads(mk["clobTokenIds"])
        except Exception:
            continue
        b = parse(mk.get("groupItemTitle") or "")
        if b is None:
            continue
        out.append({"id": mk["id"], "cid": mk.get("conditionId"), "b": b, "yes": yes, "no": no,
                    "label": mk.get("groupItemTitle") or mk.get("question", "")[:40],
                    "desc": mk.get("description") or ""})
    return out


def pseudo_event(e, kind, source, buckets, unit_sym, msym, slug=None, title=None):
    return {"slug": slug or e["slug"], "title": title or e.get("title", ""), "kind": kind,
            "city": "", "date": "", "icao": source, "unit": "", "usym": unit_sym, "msym": msym,
            "neg_risk": bool(e.get("negRisk")), "buckets": buckets, "generic": True}


def sig(tier, bk, side, margin, band, obs, extra=None):
    return {"tier": tier, "bucket": bk, "side": side, "margin_c": round(margin, 3), "band": band,
            "obs": obs, "hour": None, "n_obs": None, **(extra or {})}


def bounds_signals(tier, buckets, lower_bound, step, unit_band, yes_open_top=True):
    """Value can only grow and is already >= lower_bound.
    Buckets entirely below -> NO; open-top bucket already reached -> YES."""
    out = []
    for bk in buckets:
        lo, hi = bk["b"]
        if hi <= lower_bound:
            m = (lower_bound - hi) / step
            out.append(sig(tier, bk, "NO", m, unit_band(m), lower_bound))
        elif yes_open_top and hi == math.inf and lo <= lower_bound:
            m = (lower_bound - lo) / step
            out.append(sig(tier, bk, "YES", m, unit_band(m), lower_bound))
    return out


def band_steps(m):
    return "m<1" if m < 1 else ("m1-3" if m < 3 else "m3+")


# --------------------------------------------------------------------------- USGS
def usgs(o):
    if _usgs["q"] is not None and time.time() - _usgs["t"] < 150:
        return _usgs["q"]
    start = f"{datetime.now(timezone.utc).year - 1}-12-25"
    d = o.get_json("https://earthquake.usgs.gov/fdsnws/event/1/query",
                   {"format": "geojson", "starttime": start, "minmagnitude": 5.0,
                    "orderby": "time", "eventtype": "earthquake"}, timeout=40)
    if not d or "features" not in d:
        return _usgs["q"]
    q = [(datetime.fromtimestamp(f["properties"]["time"] / 1000, timezone.utc),
          float(f["properties"]["mag"]), f["properties"].get("place") or "")
         for f in d["features"] if f["properties"].get("mag") is not None]
    _usgs.update(t=time.time(), q=q)
    return q


def parse_count_label(t):
    t = t.replace("–", "-").replace("—", "-").strip()
    m = re.fullmatch(r"(?:≤|<=)\s*(\d+)", t)
    if m:
        return (-math.inf, int(m.group(1)) + 0.5)
    m = re.fullmatch(r"(?:more than|>)\s*(\d+)", t, re.I)
    if m:
        return (int(m.group(1)) + 0.5, math.inf)
    m = re.fullmatch(r"(\d+)\s*\+", t)
    if m:
        return (int(m.group(1)) - 0.5, math.inf)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", t)
    if m:
        return (int(m.group(1)) - 0.5, int(m.group(2)) + 0.5)
    m = re.fullmatch(r"(\d+)", t)
    if m:
        return (int(m.group(1)) - 0.5, int(m.group(1)) + 0.5)
    return None


def parse_mag_label(t):
    """Magnitudes rounded to 0.1 -> continuous [a-0.05, b+0.05)."""
    t = t.replace("–", "-").replace("—", "-").strip()
    N = r"(\d+(?:\.\d+)?)"
    m = re.fullmatch(r"under\s*" + N, t, re.I)
    if m:
        return (-math.inf, float(m.group(1)) - 0.05)
    m = re.fullmatch(N + r"\s*or (below|less)", t, re.I)
    if m:
        return (-math.inf, float(m.group(1)) + 0.05)
    m = re.fullmatch(N + r"\s*(or (more|above|higher)|\+)", t, re.I)
    if m:
        return (float(m.group(1)) - 0.05, math.inf)
    m = re.fullmatch(N + r"\s*(?:to|-)\s*" + N, t, re.I)
    if m:
        return (float(m.group(1)) - 0.05, float(m.group(2)) + 0.05)
    return None


def et_day(y, mo, d, end=False):
    t = datetime(y, mo, d, 23, 59, 59, tzinfo=ET) if end else datetime(y, mo, d, tzinfo=ET)
    return t.astimezone(timezone.utc)


def quake_events(o, events):
    out = []
    q = usgs(o)
    if not q:
        return out
    mm = o.CFG["quake_mag_margin"]
    now = datetime.now(timezone.utc)
    for e in events:
        s = e["slug"]
        end_year = int((e.get("endDate") or f"{now.year}")[:4])

        m = re.match(r"^how-many-(\d+)pt(\d)-or-above-earthquakes(?:-worldwide)?-(.+)$", s)
        if m:
            thr = float(f"{m.group(1)}.{m.group(2)}")
            rest = m.group(3)
            w = re.match(r"^([a-z]+)-(\d+)-([a-z]+)-(\d+)", rest)
            if w and w.group(1) in MONTHS and w.group(3) in MONTHS:
                y1 = end_year - (1 if MONTHS[w.group(1)] > MONTHS[w.group(3)] else 0)
                start = et_day(y1, MONTHS[w.group(1)], int(w.group(2)))
                end = et_day(end_year, MONTHS[w.group(3)], int(w.group(4)), end=True)
            elif re.match(r"^in-(\d{4})", rest):
                y = int(rest[3:7])
                start, end = et_day(y, 1, 1), et_day(y, 12, 31, end=True)
            else:
                continue
            if now < start:
                continue
            n = sum(1 for t, mag, _ in q if start <= t <= end and mag >= thr + mm)
            bks = open_buckets(e, parse_count_label)
            pe = pseudo_event(e, "quake_count", "USGS", bks, " quakes", " quakes")
            out.append((pe, bounds_signals("quake", bks, n, 1.0, band_steps)))
            continue

        m = re.match(r"^biggest-earthquake-(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            mags = [mag for t, mag, _ in q if day <= t < day + timedelta(days=1)]
            if not mags:
                continue
            lb = max(mags) - mm
            bks = open_buckets(e, parse_mag_label)
            pe = pseudo_event(e, "quake_max", "USGS", bks, " M", " M")
            out.append((pe, bounds_signals("quake", bks, lb, 0.1, band_steps)))
            continue

        m = re.match(r"^highest-earthquake-magnitude-in-(\d{4})", s)
        if m:
            y = int(m.group(1))
            mags = [mag for t, mag, _ in q if et_day(y, 1, 1) <= t <= et_day(y, 12, 31, end=True)]
            if not mags:
                continue
            lb = max(mags) - mm
            bks = open_buckets(e, parse_mag_label)
            pe = pseudo_event(e, "quake_max", "USGS", bks, " M", " M")
            out.append((pe, bounds_signals("quake", bks, lb, 0.1, band_steps)))
            continue

        m = re.match(r"^how-strong-of-an-earthquake-will-occur-worldwide-by-([a-z]+)-(\d+)", s)
        if m and m.group(1) in MONTHS:
            try:
                start = datetime.fromisoformat((e.get("startDate") or e["creationDate"]).replace("Z", "+00:00"))
            except Exception:
                continue
            end = et_day(end_year, MONTHS[m.group(1)], int(m.group(2)), end=True)
            mags = [mag for t, mag, _ in q if start <= t <= end]
            if not mags:
                continue
            lb = max(mags) - mm
            bks = open_buckets(e, parse_mag_label)        # "6.5+" -> [6.45, inf)
            for bk in bks:                                # binary thresholds: YES once reached
                bk["b"] = (bk["b"][0] + 0.05, math.inf)
            pe = pseudo_event(e, "quake_threshold", "USGS", bks, " M", " M")
            out.append((pe, bounds_signals("quake", bks, lb, 0.1, band_steps)))
    return out


# --------------------------------------------------------------------------- NWS CLI
def cli(o, icao, year):
    key = (icao, year)
    hit = _cli.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    d = o.get_json("https://mesonet.agron.iastate.edu/json/cli.py",
                   {"station": icao, "year": year}, timeout=30)
    if not d or "results" not in d:
        return hit[1] if hit else []
    _cli[key] = (time.time(), d["results"])
    return d["results"]


def precip_val(v):
    """float inches, 0.0 for trace, None for missing."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().strip("'")
        if v.upper() == "T":
            return 0.0
        try:
            return float(v)
        except ValueError:
            return None
    return float(v)


def is_trace(v):
    return isinstance(v, str) and v.strip().strip("'").upper() == "T"


def lst_window(tz, date):
    """NWS climate day = midnight..midnight local *standard* time -> (start_utc, end_utc)."""
    z = ZoneInfo(tz)
    y, mo, d = map(int, date.split("-"))
    std = min(datetime(y, 1, 15, tzinfo=z).utcoffset(), datetime(y, 7, 15, tzinfo=z).utcoffset())
    start = datetime(y, mo, d, tzinfo=timezone.utc) - std
    return start, start + timedelta(days=1)


def product_time(rec):
    try:
        return datetime.strptime(rec["product"][:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_rain_label(t):
    t = t.replace('"', "").replace("in", "").replace("mm", "").replace("–", "-").strip()
    N = r"(\d+(?:\.\d+)?)"
    m = re.fullmatch(r"<\s*" + N, t)
    if m:
        return (-math.inf, float(m.group(1)))
    m = re.fullmatch(r">\s*" + N + r"|" + N + r"\s*\+", t)
    if m:
        return (float(m.group(1) or m.group(2)), math.inf)
    m = re.fullmatch(N + r"\s*-\s*" + N, t)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    return None


def rain_month_events(o, events):
    out = []
    now = datetime.now(timezone.utc)
    for e in events:
        m = re.match(r"^precipitation-in-([a-z-]+?)-in-([a-z]+)", e["slug"])
        if not m or m.group(1) not in RAIN_MONTH_STATIONS or m.group(2) not in MONTHS:
            continue
        icao = RAIN_MONTH_STATIONS[m.group(1)]
        src = f"CLI:{icao}"
        if src in o.LEARN["blocked"]:
            continue
        mon = MONTHS[m.group(2)]
        end_year = int((e.get("endDate") or str(now.year))[:4])
        year = end_year - 1 if mon > int((e.get("endDate") or "x-12")[5:7]) else end_year
        if datetime(year, mon, 1, tzinfo=timezone.utc) > now:
            continue
        recs = [r for r in cli(o, icao, year) if r.get("valid", "")[:7] == f"{year}-{mon:02d}"]
        if not recs:
            continue
        last = max(recs, key=lambda r: r["valid"])
        total = precip_val(last.get("precip_month"))
        if total is None:
            continue
        st = o.station(icao)
        bks = open_buckets(e, parse_rain_label)
        pe = pseudo_event(e, "rain_month", src, bks, '"', '"')
        mg = o.CFG["rain_month_margin_in"] + o.CFG["rain_month_margin_frac"] * total
        days_in_month = ((datetime(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)).day)
        final = False
        if st and last["valid"] == f"{year}-{mon:02d}-{days_in_month:02d}":
            pt = product_time(last)
            final = pt is not None and pt >= lst_window(st["tz"], last["valid"])[1]
        if final:
            fm = o.CFG["rain_month_final_margin_in"]
            ss = []
            for bk in bks:
                lo, hi = bk["b"]
                if lo + fm <= total < hi - fm:
                    mm = min(total - lo, hi - total)
                    ss.append(sig("rain_month_final", bk, "YES", mm / 0.1, band_steps(mm / 0.1), total))
                elif hi <= total - fm or lo >= total + fm:
                    mm = max(total - hi, lo - total)
                    ss.append(sig("rain_month_final", bk, "NO", mm / 0.1, band_steps(mm / 0.1), total))
            out.append((pe, ss))
        else:
            out.append((pe, bounds_signals("rain_month", bks, total - mg, 0.1, band_steps)))
    return out


def rain_day_events(o, events):
    """where-will-it-rain-on-<date>: one pseudo-event per city market (independent outcomes)."""
    items = []
    for e in events:
        m = re.match(r"^where-will-it-rain-on-([a-z]+)-(\d+)-(\d{4})", e["slug"])
        if not m or m.group(1) not in MONTHS:
            continue
        date = f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
        for bk in open_buckets(e, lambda t: (0, 0)):
            sm = re.search(r"climate day for (K[A-Z]{3})", bk["desc"]) or \
                re.search(r"\(CLI\) for (K[A-Z]{3})", bk["desc"])
            if sm:
                items.append((e, date, sm.group(1), bk))
    if not items:
        return []
    icaos = sorted({i for _, _, i, _ in items})
    metars = raw_metars(o, icaos)
    out = []
    now = datetime.now(timezone.utc)
    for e, date, icao, bk in items:
        src = f"CLI:{icao}"
        if src in o.LEARN["blocked"]:
            continue
        st = o.station(icao)
        if not st:
            continue
        start, end = lst_window(st["tz"], date)
        if now < start:
            continue
        pe = pseudo_event(e, "rain_day", src, [bk], '"', '"', slug=f"{e['slug']}#{icao}",
                          title=f"Rain in {bk['label']} on {date}?")
        ss = []
        rec = next((r for r in cli(o, icao, int(date[:4])) if r.get("valid") == date), None)
        p = precip_val(rec.get("precip")) if rec else None
        if p is not None and p >= 0.01:
            ss.append(sig("rain_cli", bk, "YES", p / 0.01, band_steps(p / 0.05), p))
        elif rec and p is not None:
            pt = product_time(rec)
            if pt and pt >= end:      # final report issued after the climate day ended
                ss.append(sig("rain_cli_final", bk, "NO", 1.0, "trace" if is_trace(rec.get("precip")) else "zero", p))
        if not ss:
            tot = sum(v for t, v in metars.get(icao, []) if start < t <= end)
            if tot >= o.CFG["rain_metar_min_in"]:
                ss.append(sig("rain_metar", bk, "YES", tot / 0.01, band_steps(tot / 0.05),
                              round(tot, 2)))
        out.append((pe, ss))
    return out


def raw_metars(o, icaos):
    """{icao: [(utc, hourly_precip_in)]} from routine METAR P-groups (hourly accumulation)."""
    out = {}
    for k in range(0, len(icaos), 4):
        d = o.get_json(f"{o.AWC}/metar", {"ids": ",".join(icaos[k:k + 4]), "format": "json",
                                          "hours": 40}, timeout=40) or []
        for ob in d:
            if ob.get("metarType") != "METAR" or not ob.get("obsTime"):
                continue
            m = re.search(r"\sP(\d{4})(?=\s|$)", ob.get("rawOb", ""))
            if m:
                out.setdefault(ob["icaoId"], []).append(
                    (datetime.fromtimestamp(ob["obsTime"], timezone.utc), int(m.group(1)) / 100))
    for i in out:
        out[i] = sorted(set(out[i]))
    return out


def all_signals(o, other_events):
    """[(pseudo_event, [signals])] for every supported non-temperature market."""
    res = []
    for fn in (quake_events, rain_month_events, rain_day_events):
        if not o.CFG.get("extra_" + fn.__name__.replace("_events", ""), True):
            continue
        try:
            res += fn(o, other_events)
        except Exception as ex:
            o.log.warning("extra %s failed: %s", fn.__name__, ex)
    return res
