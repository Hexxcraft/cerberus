"""Money tools: stock/crypto quotes (Yahoo Finance public endpoint, Stooq fallback), a watchlist, and a tiny budget ledger.

Cerberus gives information and arithmetic - it is NOT a licensed financial advisor.
"""
from __future__ import annotations

import csv
import io
import re
import time
from typing import Optional

import requests

from .base import tool

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CerberusLocalAssistant/1.0)"}
_SYMBOL = re.compile(r"^[A-Za-z0-9.\-=^]{1,15}$")


def fetch_quote(symbol: str) -> dict:
    """Return {'symbol','price','day_change_pct','month_change_pct','currency'} or {'symbol','error'}."""
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        return {"symbol": symbol, "error": "invalid symbol"}
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                         params={"range": "1mo", "interval": "1d"}, headers=HEADERS, timeout=15)
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
        meta = result["meta"]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        if price is None:
            raise ValueError("no price")
        prev = closes[-2] if len(closes) >= 2 else None
        first = closes[0] if closes else None
        return {"symbol": symbol, "price": round(float(price), 4), "currency": meta.get("currency", ""),
                "day_change_pct": round((price - prev) / prev * 100, 2) if prev else None,
                "month_change_pct": round((price - first) / first * 100, 2) if first else None,
                "name": meta.get("longName") or meta.get("shortName") or symbol}
    except Exception as exc_yahoo:
        try:  # fallback: Stooq (US tickers get a .us suffix)
            code = symbol.lower() if "." in symbol else symbol.lower() + ".us"
            r = requests.get("https://stooq.com/q/l/", params={"s": code, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                             headers=HEADERS, timeout=15)
            row = next(csv.DictReader(io.StringIO(r.text)))
            close = float(row["Close"])
            open_ = float(row["Open"])
            return {"symbol": symbol, "price": close, "currency": "", "name": symbol,
                    "day_change_pct": round((close - open_) / open_ * 100, 2) if open_ else None,
                    "month_change_pct": None}
        except Exception:
            return {"symbol": symbol, "error": f"could not fetch ({type(exc_yahoo).__name__})"}


def format_quotes(quotes: list[dict]) -> str:
    lines = []
    for q in quotes:
        if "error" in q:
            lines.append(f"{q['symbol']}: {q['error']}")
            continue
        d = f"{q['day_change_pct']:+.2f}% today" if q.get("day_change_pct") is not None else "n/a today"
        m = f"{q['month_change_pct']:+.2f}% 1mo" if q.get("month_change_pct") is not None else ""
        lines.append(f"{q['symbol']} ({q.get('name', '')}): {q['price']} {q.get('currency', '')} | {d} {m}".strip())
    return "\n".join(lines)


@tool("stock_quote", "Stock Watching", "Get the latest price and 1-day / 1-month change for stock, ETF or crypto symbols.",
      {"symbols": "list of tickers, e.g. ['AAPL','VOO','BTC-USD']"})
def stock_quote(ctx, symbols) -> str:
    if isinstance(symbols, str):
        symbols = [s for s in re.split(r"[,\s]+", symbols) if s]
    return format_quotes([fetch_quote(s) for s in list(symbols)[:12]])


@tool("watchlist_add", "Stock Watching", "Add a symbol to the user's watchlist.", {"symbol": "ticker"})
def watchlist_add(ctx, symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        raise ValueError("invalid symbol")
    ctx.db.execute("INSERT OR IGNORE INTO watchlist(symbol, added) VALUES(?,?)", (symbol, time.time()))
    return f"{symbol} added to the watchlist."


@tool("watchlist_remove", "Stock Watching", "Remove a symbol from the watchlist.", {"symbol": "ticker"})
def watchlist_remove(ctx, symbol: str) -> str:
    ctx.db.execute("DELETE FROM watchlist WHERE symbol=?", (symbol.strip().upper(),))
    return f"{symbol.upper()} removed."


@tool("watchlist_show", "Stock Watching", "Show the watchlist with current prices.")
def watchlist_show(ctx) -> str:
    syms = [r["symbol"] for r in ctx.db.query("SELECT symbol FROM watchlist ORDER BY symbol")]
    return format_quotes([fetch_quote(s) for s in syms]) if syms else "The watchlist is empty."


@tool("budget_add", "Personal Budget", "Record income or an expense in the budget ledger.",
      {"kind": "'income' or 'expense'", "amount": "positive number", "category": "e.g. rent, food",
       "note": "short note (optional)"})
def budget_add(ctx, kind: str, amount: float, category: str = "other", note: str = "") -> str:
    kind = kind.lower()
    if kind not in ("income", "expense"):
        raise ValueError("kind must be 'income' or 'expense'")
    amount = float(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    ctx.db.execute("INSERT INTO budget(ts,kind,amount,category,note) VALUES(?,?,?,?,?)",
                   (time.time(), kind, amount, category.lower(), note))
    return f"Recorded {kind} of {amount:.2f} ({category})."


def budget_summary_text(db, days: Optional[int] = 30) -> str:
    since = time.time() - days * 86400 if days else 0
    rows = db.query("SELECT kind, category, SUM(amount) AS total FROM budget WHERE ts>=? GROUP BY kind, category "
                    "ORDER BY total DESC", (since,))
    if not rows:
        return "No budget entries recorded yet."
    income = sum(r["total"] for r in rows if r["kind"] == "income")
    expense = sum(r["total"] for r in rows if r["kind"] == "expense")
    lines = [f"Last {days} days: income {income:.2f}, expenses {expense:.2f}, net {income - expense:+.2f}"]
    lines += [f"  {r['kind']:<7} {r['category']:<14} {r['total']:.2f}" for r in rows]
    return "\n".join(lines)


@tool("budget_summary", "Personal Budget", "Summarise income and spending from the budget ledger.",
      {"days": "look-back window, default 30 (optional)"})
def budget_summary(ctx, days: int = 30) -> str:
    return budget_summary_text(ctx.db, int(days))
