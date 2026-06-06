import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import os
DB_PATH = Path(os.environ.get("DB_PATH", "/tmp/watchlist.db"))
CACHE: dict = {}
CACHE_TTL = 300  # 5 minutes


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
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db():
    return sqlite3.connect(DB_PATH)


def fetch_stock_data(symbol: str) -> dict:
    cache_key = symbol.upper()
    now = time.time()
    if cache_key in CACHE and now - CACHE[cache_key]["ts"] < CACHE_TTL:
        return CACHE[cache_key]["data"]

    ticker = yf.Ticker(symbol)
    info = ticker.info

    price = (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )
    forward_eps = info.get("forwardEps")
    forward_pe = info.get("forwardPE")
    trailing_pe = info.get("trailingPE")
    target_price = info.get("targetMeanPrice")
    currency = info.get("currency", "USD")
    name = info.get("shortName") or info.get("longName") or symbol
    sector = info.get("sector", "")
    fifty_two_week_high = info.get("fiftyTwoWeekHigh")
    fifty_two_week_low = info.get("fiftyTwoWeekLow")

    # Calculate forward PE from manual EPS if missing
    if price and forward_eps and not forward_pe:
        forward_pe = round(price / forward_eps, 2)

    upside = None
    if price and target_price:
        upside = round((target_price - price) / price * 100, 1)

    data = {
        "symbol": symbol.upper(),
        "name": name,
        "price": price,
        "currency": currency,
        "forward_eps": forward_eps,
        "forward_pe": round(forward_pe, 2) if forward_pe else None,
        "trailing_pe": round(trailing_pe, 2) if trailing_pe else None,
        "target_price": target_price,
        "upside_pct": upside,
        "sector": sector,
        "fifty_two_week_high": fifty_two_week_high,
        "fifty_two_week_low": fifty_two_week_low,
    }

    CACHE[cache_key] = {"ts": now, "data": data}
    return data


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/api/stock/{symbol}")
def get_stock(symbol: str):
    try:
        data = fetch_stock_data(symbol.upper())
        if data["price"] is None:
            raise HTTPException(status_code=404, detail=f"找不到股票代碼: {symbol}")
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
    # Validate the symbol exists
    try:
        data = fetch_stock_data(symbol)
        if data["price"] is None:
            raise HTTPException(status_code=404, detail=f"找不到股票代碼: {symbol}")
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
    # Invalidate cache so forward PE recalculates
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
            # Override EPS with manual value if set
            if manual_eps is not None:
                data["forward_eps"] = manual_eps
                price = data.get("price")
                if price:
                    data["forward_pe"] = round(price / manual_eps, 2)
            if display_name:
                data["display_name"] = display_name
            else:
                data["display_name"] = data.get("name", symbol)
            results.append(data)
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return results
