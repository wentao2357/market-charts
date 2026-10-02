"""Daily market data fetcher.

Reads config/universe.json, downloads ~5 years of daily closes and writes
data/market.json for the dashboard. Sources, in order of preference:
  - Yahoo Finance via yfinance (prices, indices, futures, FX, crypto, yields)
  - FRED CSV (2Y yield, real yield, breakevens, credit spreads, SOFR) and
    as a fallback for a few FX / oil / crypto series
  - "alt" Yahoo tickers listed in the universe as proxies when the primary fails
A series that cannot be fetched is listed in data/market.json -> "missing";
the page shows it rather than failing.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "market.json"
YEARS = 5
START = (dt.date.today() - dt.timedelta(days=365 * YEARS + 10)).isoformat()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
EPOCH = dt.date(1970, 1, 1)


ERRORS: list[str] = []


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def err(msg: str):
    ERRORS.append(msg)
    log(msg)


def clean(s: pd.Series | None) -> pd.Series | None:
    if s is None:
        return None
    s = pd.to_numeric(s, errors="coerce").dropna()
    s = s[~s.index.duplicated(keep="last")]
    if len(s) == 0:
        return None
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s.index >= pd.Timestamp(START)]
    return s if len(s) > 20 else None


# ---------- Yahoo ----------

def yf_batch(symbols: list[str]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for i in range(0, len(symbols), 20):
        chunk = symbols[i : i + 20]
        for attempt in range(3):
            try:
                df = yf.download(
                    chunk, start=START, interval="1d", auto_adjust=True,
                    group_by="ticker", threads=True, progress=False,
                )
                break
            except Exception as e:  # rate limit or network
                log(f"yf batch error ({attempt + 1}/3): {e}")
                time.sleep(5 * (attempt + 1))
                df = None
        if df is None or df.empty:
            continue
        for sym in chunk:
            try:
                col = df[sym]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
            except KeyError:
                continue
            s = clean(col)
            if s is not None:
                out[sym] = s
        time.sleep(1.5)
    return out


def yf_single(sym: str) -> pd.Series | None:
    for attempt in range(2):
        try:
            h = yf.Ticker(sym).history(start=START, interval="1d", auto_adjust=True)
            s = clean(h["Close"]) if not h.empty else None
            if s is not None:
                return s
        except Exception as e:
            log(f"yf single {sym} error: {e}")
        time.sleep(2)
    return None


# ---------- FRED ----------

def fred(series_id: str) -> pd.Series | None:
    urls = [f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={START}",
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"]
    for attempt, url in enumerate(urls):
        try:
            r = requests.get(url, headers=UA | {"Accept": "text/csv,*/*"}, timeout=20)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["date", "value"]
            s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values,
                          index=pd.to_datetime(df["date"]))
            return clean(s)
        except Exception as e:
            err(f"fred {series_id} ({attempt + 1}/2): {type(e).__name__}: {str(e)[:160]}")
            time.sleep(2)
    return None


# ---------- US Treasury (official daily par curves) ----------

def treasury_tables() -> dict[str, pd.Series]:
    """Nominal and real par yield curves, keyed 'TSY:<col>' / 'TSYR:<col>'."""
    out: dict[str, pd.Series] = {}
    base = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
    for kind, prefix in (("daily_treasury_yield_curve", "TSY"), ("daily_treasury_real_yield_curve", "TSYR")):
        frames = []
        for year in range(int(START[:4]), dt.date.today().year + 1):
            url = f"{base}/{year}/all?type={kind}&field_tdr_date_value={year}&page&_format=csv"
            try:
                r = requests.get(url, headers=UA, timeout=30)
                r.raise_for_status()
                df = pd.read_csv(io.StringIO(r.text))
                if "Date" in df.columns and len(df):
                    frames.append(df)
            except Exception as e:
                err(f"treasury {prefix} {year}: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(0.5)
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        df.index = pd.to_datetime(df.pop("Date"), format="mixed")
        for col in df.columns:
            s = clean(df[col])
            if s is not None:
                out[f"{prefix}:{col.strip()}"] = s
    log(f"Treasury columns: {sorted(out)}")
    return out


# ---------- New York Fed (SOFR) ----------

def nyfed_sofr() -> pd.Series | None:
    url = ("https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"
           f"?startDate={START}&endDate={dt.date.today().isoformat()}")
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        rows = r.json().get("refRates", [])
        s = pd.Series({pd.Timestamp(x["effectiveDate"]): x["percentRate"] for x in rows})
        return clean(s)
    except Exception as e:
        err(f"nyfed sofr: {type(e).__name__}: {str(e)[:160]}")
        return None


# ---------- encode ----------

def sig(x: float, digits: int = 6) -> float:
    if x == 0 or not math.isfinite(x):
        return 0.0
    return round(x, max(0, digits - 1 - int(math.floor(math.log10(abs(x))))))


def encode(s: pd.Series) -> dict:
    days = [(d.date() - EPOCH).days for d in s.index]
    deltas = [days[0]] + [days[i] - days[i - 1] for i in range(1, len(days))]
    return {"t": deltas, "v": [sig(float(v)) for v in s.values]}


def main() -> int:
    all_series = [s for g in UNIVERSE["groups"] for s in g["series"]]
    yahoo_syms = sorted({s["sym"] for s in all_series if s.get("src", "yf") == "yf"})

    log(f"Yahoo batch: {len(yahoo_syms)} symbols")
    ydata = yf_batch(yahoo_syms)
    for sym in yahoo_syms:
        if sym not in ydata:
            s = yf_single(sym)
            if s is not None:
                ydata[sym] = s

    need_tsy = any(sp.get("src") in ("treasury", "treasury_real") or
                   any(str(x).startswith("TSY") for x in sp.get("expr", [])) for sp in all_series)
    tsy = treasury_tables() if need_tsy else {}

    got: dict[str, pd.Series] = {}
    used: dict[str, str] = {}
    missing: list[dict] = []
    today = pd.Timestamp.today().normalize()

    def stale(x: pd.Series | None) -> bool:
        return x is not None and (today - x.index[-1]).days > 10

    for spec in all_series:
        sid, src = spec["id"], spec.get("src", "yf")
        s, label = None, None
        if src == "derived":
            continue
        if src == "fred":
            s, label = fred(spec["sym"]), f"FRED {spec['sym']}"
        elif src == "treasury":
            s, label = tsy.get(f"TSY:{spec['field']}"), f"美国财政部 {spec['field']}"
        elif src == "treasury_real":
            s, label = tsy.get(f"TSYR:{spec['field']}"), f"美国财政部 实际 {spec['field']}"
        elif src == "nyfed":
            s, label = nyfed_sofr(), "纽约联储 SOFR"
        else:
            s, label = ydata.get(spec["sym"]), f"Yahoo {spec['sym']}"
            if s is not None and spec.get("kind") == "rate" and s.median() > 20:
                s = s / 10.0  # older Yahoo yield quotes are x10
        if (s is None or stale(s)) and spec.get("alt"):
            for alt in spec["alt"]:
                a = ydata.get(alt)
                if a is None:
                    a = yf_single(alt)
                if a is not None and not stale(a):
                    s, label = a, f"Yahoo {alt}（替代）"
                    break
        if (s is None or stale(s)) and spec.get("fred") and src != "fred":
            f = fred(spec["fred"])
            if f is not None and (s is None or f.index[-1] > s.index[-1]):
                s, label = f, f"FRED {spec['fred']}"
        if s is None:
            missing.append({"id": sid, "name": spec["name"], "sym": spec.get("sym") or spec.get("field") or src})
            err(f"MISSING {sid}")
            continue
        got[sid], used[sid] = s, label

    def lookup(key: str) -> pd.Series | None:
        return tsy.get(key) if key.startswith("TSY") else got.get(key)

    for spec in all_series:
        if spec.get("src") != "derived":
            continue
        a, op, b = spec["expr"]
        sa, sb = lookup(a), lookup(b)
        s = None
        if sa is not None and sb is not None:
            j = pd.concat([sa, sb], axis=1, join="inner").dropna()
            s = j.iloc[:, 0] - j.iloc[:, 1] if op == "-" else j.iloc[:, 0] / j.iloc[:, 1]
            label = f"{a} {op} {b}".replace("TSYR:", "财政部实际 ").replace("TSY:", "财政部 ")
        if (s is None or len(s) < 20) and spec.get("fred"):
            s, label = fred(spec["fred"]), f"FRED {spec['fred']}"
        if s is None or len(s) < 20:
            missing.append({"id": spec["id"], "name": spec["name"], "sym": " ".join(spec["expr"])})
            err(f"MISSING {spec['id']}")
            continue
        got[spec["id"]], used[spec["id"]] = s, label

    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "groups": [
            {k: g[k] for k in ("id", "title", "question")}
            | {"series": [{k: v for k, v in s.items() if k not in ("expr", "field")} for s in g["series"]]}
            for g in UNIVERSE["groups"]
        ],
        "ratios": UNIVERSE["ratios"],
        "divergences": UNIVERSE.get("divergences", []),
        "source": used,
        "missing": missing,
        "data": {sid: encode(s) for sid, s in got.items()},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    (OUT.parent / "fetch_log.json").write_text(json.dumps(
        {"updated": payload["updated"], "source": used, "missing": missing,
         "last": {k: str(v.index[-1].date()) for k, v in got.items()}, "errors": ERRORS[-200:]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Wrote {OUT} — {len(got)} series, {len(missing)} missing, {OUT.stat().st_size / 1e6:.2f} MB")
    for m in missing:
        log(f"  missing: {m['id']} ({m['sym']})")
    # Fail the run only if most data is gone (likely a source outage), so the
    # last good file stays published.
    return 0 if len(got) >= 0.6 * len(all_series) else 1


if __name__ == "__main__":
    sys.exit(main())
