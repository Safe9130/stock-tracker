import csv
import io
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

CACHE: dict = {}
CACHE_TTL = 300  # 5 minutes

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# ── Stooq symbol mapping ───────────────────────────────────────────────────
# Taiwan listed (TSE):  2330.TW  → 2330.tw
# Taiwan OTC (TPEx):   6547.TWO → 6547.tw
# US stocks:           NVDA     → nvda.us


def to_stooq_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith(".TWO") or s.endswith(".TW"):
        code = s.replace(".TWO", "").replace(".TW", "")
        return f"{code}.tw"
    return f"{s.lower()}.us"


def is_tw(symbol: str) -> bool:
    s = symbol.upper()
    return s.endswith(".TW") or s.endswith(".TWO")


# ── TWSE name lookup ────────────────────────────────────────────────────────

TWSE_NAMES: dict[str, str] = {}


def get_tw_name(code: str) -> str:
    if code in TWSE_NAMES:
        return TWSE_NAMES[code]
    try:
        r = SESSION.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={"ex_ch": f"tse_{code}.tw", "json": 1, "delay": 0},
            timeout=8,
        )
        msg = r.json().get("msgArray", [])
        name = msg[0].get("n", code) if msg else code
        TWSE_NAMES[code] = name
        return name
    except Exception:
        return code


# ── Stooq price fetch ───────────────────────────────────────────────────────

def fetch_stooq(stooq_sym: str) -> dict | None:
    """Return latest OHLCV dict from Stooq CSV endpoint."""
    url = f"https://stooq.com/q/l/?s={stooq_sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
        if not rows:
            return None
        row = rows[-1]
        close = row.get("Close") or row.get("close")
        if not close or close.strip() in ("", "N/D", "null"):
            return None
        return {
            "close": float(close),
            "open": float(row.get("Open") or 0) or None,
            "high": float(row.get("High") or 0) or None,
            "low":  float(row.get("Low")  or 0) or None,
            "date": row.get("Date") or row.get("date", ""),
        }
    except Exception as e:
        print(f"Stooq error [{stooq_sym}]: {e}")
        return None


# ── 52-week high/low from Stooq (1-year history) ────────────────────────────

def fetch_52w(stooq_sym: str) -> tuple[float | None, float | None]:
    url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        highs, lows = [], []
        for row in reader:
            h = row.get("High") or row.get("high")
            l = row.get("Low")  or row.get("low")
            if h and h not in ("", "N/D"):
                highs.append(float(h))
            if l and l not in ("", "N/D"):
                lows.append(float(l))
        return (max(highs) if highs else None, min(lows) if lows else None)
    except Exception:
        return None, None


# ── Main fetch ──────────────────────────────────────────────────────────────

def fetch_stock_data(symbol: str) -> dict:
    sym = symbol.upper()
    cache_key = sym
    now = time.time()
    if cache_key in CACHE and now - CACHE[cache_key]["ts"] < CACHE_TTL:
        return CACHE[cache_key]["data"]

    stooq_sym = to_stooq_symbol(sym)
    quote = fetch_stooq(stooq_sym)
    if quote is None:
        raise HTTPException(status_code=404, detail=f"找不到股票代碼: {symbol}（請確認格式，台股請加 .TW，如 2330.TW）")

    price = quote["close"]
    currency = "TWD" if is_tw(sym) else "USD"

    # Company name
    if is_tw(sym):
        code = sym.replace(".TWO", "").replace(".TW", "")
        name = get_tw_name(code)
    else:
        name = sym  # fallback; can be improved

    # 52-week range (use day high/low as proxy; full history is a second call)
    year_high = quote.get("high")
    year_low  = quote.get("low")

    data = {
        "symbol": sym,
        "name": name,
        "price": price,
        "currency": currency,
        "forward_eps": None,
        "forward_pe": None,
        "trailing_pe": None,
        "target_price": None,
        "upside_pct": None,
        "sector": "",
        "fifty_two_week_high": year_high,
        "fifty_two_week_low":  year_low,
        "price_date": quote.get("date", ""),
    }

    CACHE[cache_key] = {"ts": now, "data": data}
    return data


# ── FastAPI app ─────────────────────────────────────────────────────────────

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
