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
TWSE_INFO = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_HISTORY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_INFO = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

CACHE: dict = {}
CACHE_TTL = 300  # 5 minutes

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})


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


def is_tw_stock(symbol: str) -> bool:
    return symbol.upper().endswith(".TW") or symbol.upper().endswith(".TWO")


def fetch_tw_stock(symbol: str) -> dict:
    """Fetch Taiwan stock data from TWSE/TPEX official API."""
    # Extract the numeric code (e.g. "2330" from "2330.TW")
    code = symbol.upper().replace(".TWO", "").replace(".TW", "")
    is_otc = symbol.upper().endswith(".TWO")

    exchange = "otc" if is_otc else "tse"
    ex_ch = f"{exchange}_{code}.tw"

    try:
        r = SESSION.get(TWSE_INFO, params={"ex_ch": ex_ch, "json": 1, "delay": 0}, timeout=10)
        r.raise_for_status()
        data = r.json()
        msg = data.get("msgArray", [])
        if not msg:
            raise HTTPException(status_code=404, detail=f"找不到台股代碼: {symbol}")

        m = msg[0]
        price_str = m.get("z") or m.get("y")  # z=current, y=yesterday close
        price = float(price_str) if price_str and price_str != "-" else None
        name = m.get("n", symbol)
        high = float(m.get("h")) if m.get("h") and m.get("h") != "-" else None
        low = float(m.get("l")) if m.get("l") and m.get("l") != "-" else None

        # Fallback: get previous close from history if price is None
        if price is None:
            price = fetch_tw_prev_close(code)

        return {
            "symbol": symbol.upper(),
            "name": name,
            "price": price,
            "currency": "TWD",
            "forward_eps": None,
            "forward_pe": None,
            "trailing_pe": None,
            "target_price": None,
            "upside_pct": None,
            "sector": "",
            "fifty_two_week_high": high,
            "fifty_two_week_low": low,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"台股資料錯誤: {e}")


def fetch_tw_prev_close(code: str) -> float | None:
    """Fallback: get last closing price from TWSE history."""
    try:
        r = SESSION.get(TWSE_HISTORY, params={
            "response": "json", "stockNo": code
        }, timeout=10)
        data = r.json()
        rows = data.get("data", [])
        if rows:
            last = rows[-1]
            return float(last[6].replace(",", ""))  # closing price column
    except Exception:
        pass
    return None


def fmp_get(path: str, params: dict = {}) -> dict | list | None:
    p = dict(params)
    p["apikey"] = FMP_KEY
    try:
        r = SESSION.get(f"{FMP_BASE}{path}", params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"FMP error {path}: {e}")
        return None


def fetch_us_stock(symbol: str) -> dict:
    """Fetch US stock data from FMP."""
    sym = symbol.upper()

    quote_data = fmp_get(f"/quote/{sym}")
    if not quote_data or not isinstance(quote_data, list) or len(quote_data) == 0:
        raise HTTPException(status_code=404, detail=f"找不到股票代碼: {symbol}")

    q = quote_data[0]
    price = q.get("price")
    name = q.get("name") or sym
    trailing_pe = q.get("pe")
    year_high = q.get("yearHigh")
    year_low = q.get("yearLow")

    # Forward EPS from analyst estimates
    forward_eps = None
    forward_pe = None
    est_data = fmp_get(f"/analyst-estimates/{sym}", {"limit": 4})
    if est_data and isinstance(est_data, list):
        annual = [e for e in est_data if e.get("period") == "annual"]
        if not annual:
            annual = est_data
        eps_avg = annual[0].get("estimatedEpsAvg") if annual else None
        if eps_avg:
            forward_eps = round(float(eps_avg), 2)
            if price:
                forward_pe = round(price / forward_eps, 2)

    # Analyst price target
    target_price = None
    upside_pct = None
    pt_data = fmp_get(f"/price-target-consensus/{sym}")
    if pt_data and isinstance(pt_data, list) and len(pt_data) > 0:
        target_price = pt_data[0].get("targetConsensus")
        if target_price and price:
            upside_pct = round((float(target_price) - price) / price * 100, 1)

    # Sector from profile
    sector = ""
    profile_data = fmp_get(f"/profile/{sym}")
    if profile_data and isinstance(profile_data, list) and len(profile_data) > 0:
        sector = profile_data[0].get("sector", "")

    return {
        "symbol": sym,
        "name": name,
        "price": price,
        "currency": "USD",
        "forward_eps": forward_eps,
        "forward_pe": forward_pe,
        "trailing_pe": round(float(trailing_pe), 2) if trailing_pe else None,
        "target_price": target_price,
        "upside_pct": upside_pct,
        "sector": sector,
        "fifty_two_week_high": year_high,
        "fifty_two_week_low": year_low,
    }


def fetch_stock_data(symbol: str) -> dict:
    cache_key = symbol.upper()
    now = time.time()
    if cache_key in CACHE and now - CACHE[cache_key]["ts"] < CACHE_TTL:
        return CACHE[cache_key]["data"]

    if is_tw_stock(symbol):
        data = fetch_tw_stock(symbol)
    else:
        data = fetch_us_stock(symbol)

    CACHE[cache_key] = {"ts": now, "data": data}
    return data


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/stock/{symbol}")
def get_stock(symbol: str):
    try:
        return fetch_stock_data(symbol.upper())
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
