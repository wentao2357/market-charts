"""One-off probe: which Yahoo symbols return data. Writes data/probe.json."""
import json
import yfinance as yf
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cands = [l.strip() for l in (ROOT / "config" / "probe.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
out = {"symbols": {}, "search": {}}
for s in cands:
    try:
        h = yf.Ticker(s).history(period="3mo", auto_adjust=True)
        out["symbols"][s] = {"rows": len(h), "last": str(h.index[-1].date()) if len(h) else None,
                             "close": float(h["Close"].iloc[-1]) if len(h) else None}
    except Exception as e:
        out["symbols"][s] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
for q in ["iTraxx Crossover", "iTraxx Europe", "CDX", "Markit CDX"]:
    try:
        res = yf.Search(q, max_results=15).quotes
        out["search"][q] = [{k: r.get(k) for k in ("symbol", "shortname", "longname", "exchange", "quoteType")} for r in res]
    except Exception as e:
        out["search"][q] = f"{type(e).__name__}: {e}"
(ROOT / "data" / "probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps(out, ensure_ascii=False)[:3000])
