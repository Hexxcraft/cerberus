"""Treasury tab: stock watchlist with live quotes, and a simple personal budget."""
from __future__ import annotations

import time

import customtkinter as ctk

from . import theme as T
from .widgets import button, entry, hint, section, set_text, textbox


class TreasuryTab(ctk.CTkScrollableFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._build()

    def _build(self) -> None:
        section(self, "Watchlist").pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "Quotes come from public sources and may be delayed. This is information, not financial advice.", 900).pack(anchor="w", padx=8)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=4)
        self.symbol = entry(row, 160, "ticker, e.g. AAPL")
        self.symbol.pack(side="left")
        self.symbol.bind("<Return>", lambda e: self._add_symbol())
        button(row, "Watch", self._add_symbol, "normal", 90).pack(side="left", padx=8)
        button(row, "Refresh quotes", self.load_quotes, "gold", 150).pack(side="left")
        self.quotes = ctk.CTkFrame(self, fg_color=T.PANEL, border_width=1, border_color=T.EDGE)
        self.quotes.pack(fill="x", padx=8, pady=4)

        section(self, "Personal budget (last 30 days)").pack(anchor="w", padx=8, pady=(18, 0))
        self.summary = textbox(self, 110)
        self.summary.pack(fill="x", padx=8)
        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=8, pady=6)
        self.kind = ctk.StringVar(value="expense")
        ctk.CTkOptionMenu(form, values=["expense", "income"], variable=self.kind, width=110, fg_color=T.PANEL2,
                          button_color=T.BLOOD_DK, font=T.font("body", 14)).pack(side="left")
        self.amount = entry(form, 110, "amount")
        self.amount.pack(side="left", padx=6)
        self.category = entry(form, 150, "category")
        self.category.pack(side="left", padx=6)
        self.note = entry(form, 260, "note (optional)")
        self.note.pack(side="left", padx=6)
        button(form, "Record", self._add_entry, "normal", 90).pack(side="left", padx=6)
        self.entries = textbox(self, 200, mono=True)
        self.entries.pack(fill="x", padx=8, pady=(4, 14))
        hint(self, "For analysis and advice, switch to the Treasury rite and ask. It sees your watchlist and budget.", 900).pack(anchor="w", padx=8, pady=(0, 14))

    def on_ready(self) -> None:
        pass

    def on_show(self) -> None:
        self.load_quotes()
        self.load_budget()

    # ------------------------------------------------------------ quotes
    def load_quotes(self) -> None:
        for w in self.quotes.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.quotes, text="Fetching...", font=T.font("body", 14), text_color=T.DIM).pack(padx=10, pady=6, anchor="w")
        self.app.run_bg(lambda: self.app.api.get("/watchlist"), self._quotes_loaded, lambda e: self.app.toast(str(e), "error"))

    def _quotes_loaded(self, quotes: list) -> None:
        for w in self.quotes.winfo_children():
            w.destroy()
        if not quotes:
            ctk.CTkLabel(self.quotes, text="Nothing watched yet.", font=T.font("body", 14), text_color=T.DIM).pack(padx=10, pady=6, anchor="w")
        for q in quotes:
            r = ctk.CTkFrame(self.quotes, fg_color="transparent")
            r.pack(fill="x", padx=6, pady=1)
            ctk.CTkButton(r, text="x", width=26, height=22, fg_color="transparent", hover_color=T.BLOOD_DK, text_color=T.BLOOD_HI,
                          command=lambda s=q["symbol"]: self._remove(s)).pack(side="left")
            ctk.CTkLabel(r, text=q["symbol"], width=70, anchor="w", font=T.font("head", 15, "bold"), text_color=T.GOLD).pack(side="left")
            if "error" in q:
                ctk.CTkLabel(r, text=f"unavailable ({q['error']})", font=T.font("body", 14), text_color=T.DIM).pack(side="left")
                continue
            day, month = q.get("day_change_pct"), q.get("month_change_pct")
            ctk.CTkLabel(r, text=f"{q['price']:,.2f} {q.get('currency', '')}", width=130, anchor="w", font=T.font("mono", 14), text_color=T.TEXT).pack(side="left")
            for label, v in (("day", day), ("month", month)):
                txt = "n/a" if v is None else f"{label} {v:+.2f}%"
                ctk.CTkLabel(r, text=txt, width=110, anchor="w", font=T.font("mono", 14),
                             text_color=T.DIM if v is None else (T.OK if v >= 0 else "#ff7b7b")).pack(side="left")
            ctk.CTkLabel(r, text=q.get("name", ""), font=T.font("body", 13), text_color=T.DIM).pack(side="left", padx=8)

    def _add_symbol(self) -> None:
        sym = self.symbol.get().strip()
        if not sym:
            return

        def ok(r: dict) -> None:
            self.symbol.delete(0, "end")
            self.app.toast(r["message"], "ok")
            self.load_quotes()
        self.app.run_bg(lambda: self.app.api.post("/watchlist", {"symbol": sym}), ok)

    def _remove(self, symbol: str) -> None:
        self.app.run_bg(lambda: self.app.api.delete(f"/watchlist/{symbol}"), lambda r: self.load_quotes())

    # ------------------------------------------------------------ budget
    def load_budget(self) -> None:
        self.app.run_bg(lambda: self.app.api.get("/budget"), self._budget_loaded, lambda e: self.app.toast(str(e), "error"))

    def _budget_loaded(self, b: dict) -> None:
        set_text(self.summary, b["summary"])
        lines = [f"{time.strftime('%Y-%m-%d', time.localtime(e['ts'])):<12} {e['kind']:<8} {e['amount']:>10,.2f}  {e['category']:<14} {e.get('note') or ''}"
                 for e in b["entries"]]
        set_text(self.entries, "\n".join(lines) or "(no entries yet)")

    def _add_entry(self) -> None:
        try:
            amount = float(self.amount.get().strip().replace(",", "").lstrip("$"))
        except ValueError:
            self.app.toast("Amount must be a number.", "warn")
            return
        body = {"kind": self.kind.get(), "amount": amount, "category": self.category.get().strip() or "other", "note": self.note.get().strip()}

        def ok(r: dict) -> None:
            for e in (self.amount, self.category, self.note):
                e.delete(0, "end")
            self.app.toast(r["message"], "ok")
            self.load_budget()
        self.app.run_bg(lambda: self.app.api.post("/budget", body), ok)
