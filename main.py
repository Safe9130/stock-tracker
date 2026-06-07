import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", "/tmp/watchlist.db"))
STATIC_DIR = BASE_DIR / "static"
FMP_KEY = os.environ.get("FMP_KEY", "")
FMP_BASE = "https://financialmodelingprep.com/api/v3"

CACHE: dict = {}
CACHE_TTL = 300  # 5 minutes

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "stock-tracker/1.0"})


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY,
                display_name TEXT,
                manual_eps REAL,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Stock Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def get_db():
    return sqlite3.connect(DB_PATH)


def fmp_get(path: str, params: dict = {}) -> dict | list | None:
    params["apikey"] = FMP_KEY
    try:
        r = SESSION.get(f"{FMP_BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"FMP error {path}: {e}")
        return None


def fetch_stock_data(symbol: str) -> dict:
    cache_key = symbol.upper()
    now = time.time()
    if cache_key in CACHE and now - CACHE[cache_key]["ts"] < CACHE_TTL:
        return CACHE[cache_key]["data"]

    sym = symbol.upper()

    # 1. Quote — price, trailing PE, name
    quote_data = fmp_get(f"/quote/{sym}")
    if not quote_data or not isinstance(quote_data, list) or len(quote_data) == 0:
        raise HTTPException(status_code=404, detail=f"找不到股票代碼: {symbol}")

    q = quote_data[0]
    price = q.get("price")
    name = q.get("name") or sym
    trailing_pe = q.get("pe")
    currency = "TWD" if sym.endswith(".TW") else "USD"
    year_high = q.get("yearHigh")
    year_low = q.get("yearLow")

    # 2. Analyst estimates — forward EPS
    forward_eps = None
    forward_pe = None
    target_price = None
    upside_pct = None

    est_data = fmp_get(f"/analyst-estimates/{sym}", {"limit": 2})
    if est_data and isinstance(est_data, list) and len(est_data) > 0:
        # Find the most recent annual estimate (period = "annual" or closest future)
        annual = [e for e in est_data if e.get("period", "") == "annual" or "-12" in e.get("date", "")]
        target = annual[0] if annual else est_data[0]
        eps_avg = target.get("estimatedEpsAvg")
        if eps_avg:
            forward_eps = round(float(eps_avg), 2)
            if price:
                forward_pe = round(price / forward_eps, 2)

    # 3. Price target
    pt_data = fmp_get(f"/price-target-consensus/{sym}")
    if pt_data and isinstance(pt_data, list) and len(pt_data) > 0:
        target_price = pt_data[0].get("targetConsensus")
        if target_price and price:
            upside_pct = round((float(target_price) - price) / price * 100, 1)

    # 4. Profile — sector
    sector = ""
    profile_data = fmp_get(f"/profile/{sym}")
    if profile_data and isinstance(profile_data, list) and len(profile_data) > 0:
        sector = profile_data[0].get("sector", "")

    data = {
        "symbol": sym,
        "name": name,
        "price": price,
        "currency": currency,
        "forward_eps": forward_eps,
        "forward_pe": forward_pe,
        "trailing_pe": round(float(trailing_pe), 2) if trailing_pe else None,
        "target_price": target_price,
        "upside_pct": upside_pct,
        "sector": sector,
        "fifty_two_week_high": year_high,
        "fifty_two_week_low": year_low,
    }

    CACHE[cache_key] = {"ts": now, "data": data}
    return data


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/stock/{symbol}")
def get_stock(symbol: str):
    try:
        data = fetch_stock_data(symbol.upper())
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AddStockRequest(BaseModel):
    symbol: str
    display_name: str = ""
    manual_eps: float | None = None


class UpdateEpsRequest(BaseModel):
    manual_eps: float | None = None


@app.get("/api/watchlist")
def get_watchlist():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT symbol, display_name, manual_eps FROM stocks ORDER BY added_at DESC"
        ).fetchall()
    return [{"symbol": r[0], "display_name": r[1], "manual_eps": r[2]} for r in rows]


@app.post("/api/watchlist")
def add_to_watchlist(req: AddStockRequest):
    symbol = req.symbol.upper()
    try:
        data = fetch_stock_data(symbol)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    name = req.display_name or data.get("name", symbol)
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stocks (symbol, display_name, manual_eps) VALUES (?, ?, ?)",
            (symbol, name, req.manual_eps),
        )
    return {"ok": True, "symbol": symbol}


@app.delete("/api/watchlist/{symbol}")
def remove_from_watchlist(symbol: str):
    with get_db() as conn:
        conn.execute("DELETE FROM stocks WHERE symbol = ?", (symbol.upper(),))
    return {"ok": True}


@app.patch("/api/watchlist/{symbol}/eps")
def update_manual_eps(symbol: str, req: UpdateEpsRequest):
    with get_db() as conn:
        conn.execute(
            "UPDATE stocks SET manual_eps = ? WHERE symbol = ?",
            (req.manual_eps, symbol.upper()),
        )
    CACHE.pop(symbol.upper(), None)
    return {"ok": True}


@app.get("/api/watchlist/data")
def get_watchlist_with_data():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT symbol, display_name, manual_eps FROM stocks ORDER BY added_at DESC"
        ).fetchall()

    results = []
    for symbol, display_name, manual_eps in rows:
        try:
            data = fetch_stock_data(symbol)
            if manual_eps is not None:
                data["forward_eps"] = manual_eps
                price = data.get("price")
                if price:
                    data["forward_pe"] = round(price / manual_eps, 2)
            data["display_name"] = display_name or data.get("name", symbol)
            results.append(data)
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return results
