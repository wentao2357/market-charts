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


def log(*a):
    print(*a, file=sys.stderr, flush=True)


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
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={START}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["date", "value"]
            s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values,
                          index=pd.to_datetime(df["date"]))
            return clean(s)
        except Exception as e:
            log(f"fred {series_id} error ({attempt + 1}/3): {e}")
            time.sleep(3)
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

    got: dict[str, pd.Series] = {}
    used: dict[str, str] = {}
    missing: list[dict] = []

    for spec in all_series:
        sid, src = spec["id"], spec.get("src", "yf")
        if src == "derived":
            continue
        s, label = None, None
        if src == "fred":
            s, label = fred(spec["sym"]), f"FRED {spec['sym']}"
        else:
            s, label = ydata.get(spec["sym"]), f"Yahoo {spec['sym']}"
            if s is not None and spec.get("kind") == "rate" and s.median() > 20:
                s = s / 10.0  # older Yahoo yield quotes are x10
            if s is None and spec.get("fred"):
                s, label = fred(spec["fred"]), f"FRED {spec['fred']}"
            if s is None:
                for alt in spec.get("alt", []):
                    s = ydata.get(alt)
                    if s is None:
                        s = yf_single(alt)
                    if s is not None:
                        label = f"Yahoo {alt}（替代）"
                        break
        # stale primary (no print for 10+ days) but a fallback exists -> try it
        if s is not None and spec.get("fred") and src != "fred":
            if (pd.Timestamp.today().normalize() - s.index[-1]).days > 10:
                f = fred(spec["fred"])
                if f is not None and f.index[-1] > s.index[-1]:
                    s, label = f, f"FRED {spec['fred']}"
        if s is None:
            missing.append({"id": sid, "name": spec["name"], "sym": spec.get("sym")})
            log(f"MISSING {sid}")
            continue
        got[sid], used[sid] = s, label

    for spec in all_series:
        if spec.get("src") != "derived":
            continue
        a, op, b = spec["expr"]
        if a in got and b in got:
            j = pd.concat([got[a], got[b]], axis=1, join="inner").dropna()
            s = j.iloc[:, 0] - j.iloc[:, 1] if op == "-" else j.iloc[:, 0] / j.iloc[:, 1]
            got[spec["id"]] = s
            used[spec["id"]] = f"{a} {op} {b}"
        else:
            missing.append({"id": spec["id"], "name": spec["name"], "sym": " ".join(spec["expr"])})

    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "groups": [
            {k: g[k] for k in ("id", "title", "question")}
            | {"series": [{k: v for k, v in s.items() if k not in ("expr",)} for s in g["series"]]}
            for g in UNIVERSE["groups"]
        ],
        "ratios": UNIVERSE["ratios"],
        "divergences": UNIVERSE.get("divergences", []),
        "source": used,
        "missing": missing,
        "data": {sid: encode(s) for sid, s in got.items()},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Wrote {OUT} — {len(got)} series, {len(missing)} missing, {OUT.stat().st_size / 1e6:.2f} MB")
    for m in missing:
        log(f"  missing: {m['id']} ({m['sym']})")
    # Fail the run only if most data is gone (likely a source outage), so the
    # last good file stays published.
    return 0 if len(got) >= 0.6 * len(all_series) else 1


if __name__ == "__main__":
    sys.exit(main())
