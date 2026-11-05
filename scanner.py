#!/usr/bin/env python3
"""Daily Cash Secured Put (CSP) scanner.

Usage:
    python scanner.py                      # scan + send mail
    python scanner.py --no-mail            # scan only, write to out/
    python scanner.py --symbols AAPL,MSFT  # scan only these symbols (test)

Required environment variables:
    MARKETDATA_TOKEN, GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import math
import os
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from emailer import send_mail
from marketdata import MarketData, MarketDataAuthError, MarketDataError

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent

UNDERLYING_LABELS = {
    "no_quote": "No quote data",
    "price_range": "Price out of range",
    "underlying_volume": "Stock volume too low",
    "daily_change": "Daily change out of range",
}

# Hisse filtresi kodu -> "yakın kaçan" kodu
UNDERLYING_NEAR_CODE = {
    "price_range": "underlying_price_range",
    "underlying_volume": "underlying_volume",
    "daily_change": "underlying_daily_change",
}

FUNNEL_LABELS = [
    ("contracts_total", "Put contracts scanned (stocks that passed stock filters)"),
    ("rej_missing", "Missing / invalid data"),
    ("rej_dte", "DTE out of range"),
    ("rej_delta", "Delta out of range"),
    ("rej_oi", "Open interest too low"),
    ("rej_volume", "Option volume too low"),
    ("rej_bid", "Bid too low"),
    ("rej_spread", "Bid-ask spread too wide"),
    ("rej_return", "Annualized return too low"),
    ("rej_earnings", "Failed earnings rule"),
    ("rej_earnings_unknown", "Earnings date unknown (rejected)"),
    ("rej_iv", "IV rank / percentile too low"),
    ("warn_earnings_unknown", "Earnings date unknown (listed with warning)"),
    ("passed", "PASSED ALL FILTERS"),
]

# Near-miss group titles (display order)
NEAR_LABELS = [
    ("earnings", "Failed only the earnings rule"),
    ("earnings_unknown", "Rejected only because earnings date not found"),
    ("iv", "Failed only IV rank / percentile"),
    ("oi", "Failed only on open interest"),
    ("volume", "Failed only on option volume"),
    ("bid", "Failed only on bid"),
    ("spread", "Failed only on bid-ask spread"),
    ("return", "Failed only on annualized return"),
    ("delta", "Just outside the delta band (only miss)"),
    ("underlying_price_range", "Rejected only by stock price range"),
    ("underlying_daily_change", "Rejected only by stock daily change"),
    ("underlying_volume", "Rejected only by stock volume"),
]

CONTRACT_FILTER_ORDER = ("dte", "delta", "oi", "volume", "bid", "spread", "return")


# --------------------------------------------------------------------------- yardımcılar
def num(value):
    try:
        if value is None:
            return None
        out = float(value)
        return None if out != out else out
    except (TypeError, ValueError):
        return None


def to_date(value, tz) -> dt.date | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=tz).date()
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def normalise_universe(raw) -> list[str]:
    """YAML'da tırnaksız ON, NO, Y gibi yazılan semboller doğru/yanlış değerine dönüşür; bunu yakala."""
    out = []
    for item in raw or []:
        if not isinstance(item, str):
            raise ValueError(
                f"config.yaml 'universe' has a non-symbol entry ({item!r}). "
                "Symbols like ON, NO, Y, N, OFF must be quoted, e.g.: - \"ON\""
            )
        out.append(item.strip().upper())
    return out


# --------------------------------------------------------------------------- IV geçmişi
class IVHistory:
    """data/iv_history.csv: date,symbol,iv  (her hisse için günde bir satır)."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[dt.date, float]] = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    day = to_date(row.get("date"), NY)
                    iv = num(row.get("iv"))
                    if day and iv is not None and row.get("symbol"):
                        self.data.setdefault(row["symbol"], {})[day] = iv

    def set(self, symbol: str, day: dt.date, iv: float) -> None:
        self.data.setdefault(symbol, {})[day] = iv

    def prior_series(self, symbol: str, today: dt.date, lookback_days: int) -> list[float]:
        start = today - dt.timedelta(days=lookback_days)
        return [v for d, v in self.data.get(symbol, {}).items() if start <= d < today]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "symbol", "iv"])
            for symbol in sorted(self.data):
                for day in sorted(self.data[symbol]):
                    writer.writerow([day.isoformat(), symbol, f"{self.data[symbol][day]:.6f}"])


def iv_sample(rows: list[dict], today: dt.date, ocfg: dict, icfg: dict) -> float | None:
    """Bugünün IV örneği: DTE aralığındaki ve delta bandındaki put'ların medyan IV'si."""
    values = []
    for row in rows:
        exp = to_date(row.get("expiration"), NY)
        delta, iv = num(row.get("delta")), num(row.get("iv"))
        if exp is None or delta is None or iv is None or iv <= 0:
            continue
        if not (ocfg["dte_min"] <= (exp - today).days <= ocfg["dte_max"]):
            continue
        if icfg["sample_delta_abs_min"] <= abs(delta) <= icfg["sample_delta_abs_max"]:
            values.append(iv)
    return statistics.median(values) if len(values) >= 3 else None


def iv_metrics(history: IVHistory, symbol: str, today: dt.date, current: float | None, icfg: dict) -> dict:
    prior = history.prior_series(symbol, today, icfg["lookback_calendar_days"])
    if current is None:
        return {"status": "NO_IV", "n": len(prior)}
    if len(prior) + 1 < icfg["min_history_days"]:
        return {"status": "BUILDING", "n": len(prior) + 1}
    values = prior + [current]
    low, high = min(values), max(values)
    rank = 0.0 if high == low else (current - low) / (high - low) * 100
    pct = 100 * sum(1 for v in prior if v < current) / len(prior)
    return {"status": "OK", "rank": rank, "pct": pct, "n": len(values)}


def iv_passes(metrics: dict, icfg: dict) -> bool:
    if metrics["status"] == "OK":
        return metrics["rank"] > icfg["rank_gt"] and metrics["pct"] > icfg["percentile_gt"]
    return icfg["when_insufficient_history"] != "reject"


def apply_iv(cand: dict, metrics: dict) -> None:
    cand["iv_status"], cand["iv_n"] = metrics["status"], metrics.get("n")
    if metrics["status"] == "OK":
        cand["iv_rank"], cand["iv_pct"] = metrics["rank"], metrics["pct"]


# --------------------------------------------------------------------------- earnings kuralları
def earnings_rules(cfg: dict) -> dict:
    """Earnings settings. Defaults apply if keys are missing from config.yaml."""
    e = cfg.get("earnings", {}) or {}
    return {
        "min_days": e.get("min_days_to_earnings", 30),          # tarama tarihi ile earnings arası en az bu kadar gün
        "expiry_before": e.get("expiry_before_earnings", False),  # True: vade de earnings'ten önce olmalı
        "unknown": e.get("when_date_unknown", "warn"),            # warn = uyarıyla listele, reject = ele
        "lookahead": e.get("lookahead_days", 180),
        "lookback": e.get("lookback_days", 120),                  # son açıklama tarihi için geriye bakış
    }


def etf_set(cfg: dict) -> set[str]:
    return {x.upper() for x in (cfg.get("etf_symbols") or []) if isinstance(x, str)}


def mark_earnings(cand: dict, next_earnings: dt.date | None, unknown_note: str,
                  last_earnings: dt.date | None = None, is_etf: bool = False) -> None:
    cand["is_etf"] = is_etf
    cand["last_earnings"] = last_earnings
    if is_etf:
        cand["earnings_date"], cand["spans_earnings"], cand["earnings_note"] = None, False, ""
        return
    cand["earnings_date"] = next_earnings
    cand["spans_earnings"] = bool(next_earnings and cand["expiry"] >= next_earnings)
    if next_earnings is None:
        cand["earnings_note"] = f"earnings date {unknown_note}"
    elif cand["spans_earnings"]:
        cand["earnings_note"] = "expiry spans earnings"
    else:
        cand["earnings_note"] = ""


def earnings_note_text(cfg: dict) -> str:
    r = earnings_rules(cfg)
    text = f"Earnings: at least {r['min_days']} days between the scan date and the next earnings date."
    if r["expiry_before"]:
        text += " Expiry must also be before the earnings date."
    else:
        text += " Expiry may span earnings; such rows are flagged 'expiry spans earnings'."
    text += (" Stocks with no earnings date are listed with an 'earnings date unknown' warning."
             if r["unknown"] == "warn" else " Stocks with no earnings date are rejected.")
    etfs = sorted(etf_set(cfg))
    if etfs:
        text += f" ETFs ({', '.join(etfs)}) are exempt from the earnings rule."
    return text


def missing_breakdown(result) -> str:
    labels = [("missing_expiry", "no expiry"), ("missing_strike", "no strike"), ("missing_delta", "no delta"),
              ("missing_bid", "no bid"), ("missing_ask", "ask missing/0")]
    parts = [f"{label} {result.funnel[key]}" for key, label in labels if result.funnel.get(key)]
    return ", ".join(parts)


def price_out_of_range(last: float, ucfg: dict) -> bool:
    """Hisse fiyat sınırı varsayılan olarak KAPALI (liste senin kontrolünde).
    Açmak için config.yaml: underlying.use_price_limits: true"""
    if not ucfg.get("use_price_limits", False):
        return False
    lo, hi = ucfg.get("price_min"), ucfg.get("price_max")
    return (lo is not None and last < lo) or (hi is not None and last > hi)


# --------------------------------------------------------------------------- puanlama (0-100)
SCORE_LABELS = {"return": "Return", "safety": "Safety", "liquidity": "Liquidity", "earnings": "Earnings", "iv": "IV"}
DEFAULT_SCORE = {
    "weights": {"return": 30, "safety": 30, "liquidity": 20, "earnings": 10, "iv": 10},
    "return_zero_at": 10, "return_full_at": 30,     # yıllık getiri %: 10 -> 0 puan, 30 ve üstü -> 100 puan
    "delta_zero_at": 0.30, "delta_full_at": 0.10,   # |delta|: 0.30 -> 0 puan, 0.10 ve altı -> 100 puan
    "spread_zero_at": 10, "spread_full_at": 1,      # spread %: 10 -> 0 puan, 1 ve altı -> 100 puan
    "oi_zero_at": 300, "oi_full_at": 3000,          # open interest
    "ivr_zero_at": 20, "ivr_full_at": 80,           # IV rank
    "earnings_known": 100, "earnings_spans": 30, "earnings_unknown": 40,
    # Puan tavanları: çok yüksek IV'li ya da vadesi earnings'i kapsayan satırlar listeyi domine etmesin
    "iv_cap_above_pct": 100, "iv_cap_score": 70,    # IV % bunun üstündeyse puan en fazla 70
    "spans_cap_score": 70,                          # vade earnings'i kapsıyorsa puan en fazla 70
}


def score_settings(cfg: dict) -> dict:
    user = cfg.get("score", {}) or {}
    out = {**DEFAULT_SCORE, **{k: v for k, v in user.items() if k != "weights"}}
    out["weights"] = {**DEFAULT_SCORE["weights"], **(user.get("weights") or {})}
    return out


def _lin(x: float, zero_at: float, full_at: float) -> float:
    """zero_at -> 0, full_at -> 100, arası doğrusal; dışı kırpılır. (zero_at > full_at ise ters ölçek)"""
    if full_at == zero_at:
        return 100.0 if x >= full_at else 0.0
    t = (x - zero_at) / (full_at - zero_at)
    return max(0.0, min(1.0, t)) * 100.0


def score_candidate(c: dict, cfg: dict) -> tuple[int | None, dict]:
    """Adayın 0-100 puanı ve bileşenleri. Hesaplanamayan bileşen (ör. IV geçmişi yok) çıkarılır,
    kalan ağırlıklar yeniden ölçeklenir; yani eksik veri puanı düşürmez."""
    s = score_settings(cfg)
    parts: dict[str, float | None] = {
        "return": _lin(c["annualized_pct"], s["return_zero_at"], s["return_full_at"]),
        "safety": _lin(abs(c["delta"]), s["delta_zero_at"], s["delta_full_at"]),
    }
    oi = c.get("open_interest")
    liq = [_lin(c["spread_pct"], s["spread_zero_at"], s["spread_full_at"])]
    if oi is not None:
        liq.append(_lin(oi, s["oi_zero_at"], s["oi_full_at"]))
    parts["liquidity"] = sum(liq) / len(liq)
    if c.get("is_etf"):
        parts["earnings"] = None
    elif c.get("earnings_date") is None:
        parts["earnings"] = float(s["earnings_unknown"])
    elif c.get("spans_earnings"):
        parts["earnings"] = float(s["earnings_spans"])
    else:
        parts["earnings"] = float(s["earnings_known"])
    parts["iv"] = _lin(c["iv_rank"], s["ivr_zero_at"], s["ivr_full_at"]) if c.get("iv_status") == "OK" else None

    num_, den = 0.0, 0.0
    for key, value in parts.items():
        weight = float(s["weights"].get(key, 0) or 0)
        if value is None or weight <= 0:
            continue
        num_ += weight * value
        den += weight
    if den == 0:
        return None, {}
    score = round(num_ / den)
    caps = []
    iv_pct_now = c["iv"] * 100 if c.get("iv") is not None else None
    cap_iv = s.get("iv_cap_above_pct")
    if cap_iv and iv_pct_now is not None and iv_pct_now > cap_iv and score > s["iv_cap_score"]:
        score = int(s["iv_cap_score"])
        caps.append(f"IV above {cap_iv:g}%")
    if c.get("spans_earnings") and score > s["spans_cap_score"]:
        score = int(s["spans_cap_score"])
        caps.append("expiry spans earnings")
    c["score_cap"] = " + ".join(caps)
    return score, {k: round(v) for k, v in parts.items() if v is not None}


def score_note_text(cfg: dict) -> str:
    s = score_settings(cfg)
    w = s["weights"]
    parts = " · ".join(f"{label} {w[key]:g}" for key, label in SCORE_LABELS.items() if w.get(key))
    return ("Score (0-100) is a weighted average: " + parts + ". Return: annualized return; Safety: low |delta|; "
            "Liquidity: tight spread + high OI; Earnings: date known and expiry before it; IV: IV rank "
            "(dropped while history builds, so it does not lower the score). Caps: the score is limited to "
            f"{s['iv_cap_score']:g} when IV is above {s['iv_cap_above_pct']:g}% and to {s['spans_cap_score']:g} when the expiry "
            "spans earnings. It is a heuristic ranking aid, not a guarantee of return or safety.")


# --------------------------------------------------------------------------- HV, gruplar, bütçe, geçmiş
def hv_from_closes(closes: list[float], days: int) -> float | None:
    """Son `days` günlük log getirilerinin yıllıklandırılmış standart sapması (%)."""
    if len(closes) < days + 1:
        return None
    window = closes[-(days + 1):]
    rets = [math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0]
    if len(rets) < max(2, days):
        return None
    return statistics.stdev(rets) * math.sqrt(252) * 100


def group_map(cfg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, syms in (cfg.get("groups") or {}).items():
        for sym in syms or []:
            if isinstance(sym, str):
                out[sym.upper()] = str(name)
    return out


def exposure_settings(cfg: dict) -> dict | None:
    """config.yaml 'exposure' yoksa bütçe görünümü kapalıdır."""
    e = cfg.get("exposure")
    if not e:
        return None
    limit = float(e.get("limit_usd", 60000))
    manual = float(e.get("used_usd", 0))
    from_positions = float(cfg.get("_positions_used", 0) or 0)
    used = manual + from_positions
    return {"limit": limit, "used": used, "manual": manual, "from_positions": from_positions,
            "available": max(0.0, limit - used),
            "max_per_group": int(e.get("basket_max_per_group", 2))}


def build_basket(cands: list[dict], cfg: dict) -> dict | None:
    """Puana göre açgözlü örnek sepet: hisse başına 1 kontrat, grup başına en fazla N, bütçeyi aşmaz."""
    ex = exposure_settings(cfg)
    if not ex:
        return None
    left = ex["available"]
    picks, taken, per_group = [], set(), Counter()
    ordered = sorted(cands, key=lambda c: (-(c.get("score") if c.get("score") is not None else -1),
                                           -c["annualized_pct"], c["symbol"]))
    for c in ordered:
        grp = c.get("group") or "Other"
        if c["symbol"] in taken or per_group[grp] >= ex["max_per_group"] or c["collateral"] > left:
            continue
        picks.append(c)
        taken.add(c["symbol"])
        per_group[grp] += 1
        left -= c["collateral"]
    total = sum(c["collateral"] for c in picks)
    income = sum(c["premium"] * 100 for c in picks)
    return {"picks": picks, "total": total, "income": income, "left": left, **ex}


def cand_key(symbol, expiry, strike) -> tuple[str, str, str]:
    exp = expiry.isoformat() if isinstance(expiry, dt.date) else str(expiry)
    return (str(symbol), exp, f"{float(strike):.2f}")


class CandidateLog:
    """data/candidates_history.csv: her tarama gününün listesi (dünle karşılaştırma ve gün sayacı için)."""
    COLS = ["date", "symbol", "expiry", "strike", "bid", "score", "annualized", "collateral"]

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = []
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    day = to_date(row.get("date"), NY)
                    if day and row.get("symbol") and num(row.get("strike")) is not None:
                        self.rows.append({**row, "_day": day})

    def dates_before(self, today: dt.date) -> list[dt.date]:
        return sorted({r["_day"] for r in self.rows if r["_day"] < today}, reverse=True)

    def by_date(self, day: dt.date) -> dict[tuple, dict]:
        out = {}
        for r in self.rows:
            if r["_day"] == day:
                out[cand_key(r["symbol"], r["expiry"], r["strike"])] = r
        return out

    def record(self, today: dt.date, cands: list[dict], keep_days: int = 90) -> None:
        start = today - dt.timedelta(days=keep_days)
        self.rows = [r for r in self.rows if start <= r["_day"] < today]
        for c in cands:
            self.rows.append({"date": today.isoformat(), "symbol": c["symbol"], "expiry": c["expiry"].isoformat(),
                              "strike": f"{c['strike']:.2f}", "bid": f"{c['bid']:.2f}",
                              "score": "" if c.get("score") is None else c["score"],
                              "annualized": f"{c['annualized_pct']:.1f}", "collateral": f"{c['collateral']:.0f}",
                              "_day": today})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.COLS, extrasaction="ignore")
            writer.writeheader()
            for r in sorted(self.rows, key=lambda r: (r["_day"], r["symbol"], r["expiry"], float(r["strike"]))):
                writer.writerow(r)


def analyze_changes(result, log: CandidateLog, today: dt.date) -> dict | None:
    """Dünkü (son tarama günü) listeyle karşılaştırır; adaylara 'is_new' ve 'streak' yazar."""
    listed = result.listed
    prev_days = log.dates_before(today)
    if not prev_days:
        for c in listed:
            c["is_new"], c["streak"] = False, 1
        return None
    maps = [log.by_date(d) for d in prev_days]
    prev = maps[0]
    cur = {cand_key(c["symbol"], c["expiry"], c["strike"]): c for c in listed}
    for key, c in cur.items():
        n = 0
        for m in maps:
            if key not in m:
                break
            n += 1
        c["streak"], c["is_new"] = n + 1, key not in prev

    near_index = {cand_key(c["symbol"], c["expiry"], c["strike"]): c["miss_detail"] for c in result.near_misses}
    new = sorted((c for k, c in cur.items() if k not in prev),
                 key=lambda c: (-(c["score"] if c.get("score") is not None else -1), c["symbol"]))
    dropped = []
    for key, row in prev.items():
        if key in cur:
            continue
        sym = row["symbol"]
        if key in near_index:
            why = "now fails: " + near_index[key]
        elif sym in result.underlying_rejects:
            why = "stock filter: " + underlying_detail(result.underlying_rejects[sym], result.underlying_info.get(sym, {}))
        elif any(s_ == sym for s_, _ in result.errors):
            why = "scan error for this stock"
        else:
            why = "fails 2+ filters now, or contract no longer listed"
        dropped.append({"symbol": sym, "expiry": row["expiry"], "strike": float(row["strike"]), "why": why})
    dropped.sort(key=lambda d: d["symbol"])
    movers = []
    for key, c in cur.items():
        row = prev.get(key)
        if not row:
            continue
        pb, ps = num(row.get("bid")), num(row.get("score"))
        if not pb:
            continue
        chg = (c["bid"] - pb) / pb * 100
        sdiff = (c["score"] - ps) if (c.get("score") is not None and ps is not None) else 0
        if abs(chg) >= 15 or abs(sdiff) >= 8:
            movers.append({"cand": c, "prev_bid": pb, "bid_chg": chg, "prev_score": ps, "score_diff": sdiff})
    movers.sort(key=lambda m: -abs(m["bid_chg"]))
    unchanged = len(cur) - len(new) - len(movers)
    return {"prev_date": prev_days[0], "new": new, "dropped": dropped, "movers": movers,
            "unchanged": max(0, unchanged)}


# --------------------------------------------------------------------------- Wheel: pozisyon takibi
def occ_symbol(symbol: str, expiry: dt.date, side: str, strike: float) -> str:
    """OCC opsiyon sembolü, örn. AAPL261218P00200000."""
    return f"{symbol}{expiry:%y%m%d}{'P' if side == 'put' else 'C'}{int(round(strike * 1000)):08d}"


def load_positions(path: Path, today: dt.date) -> dict | None:
    """positions.yaml'ı okur; hatalı satırları 'problems' listesine yazar, asla çökmez. Dosya yoksa None."""
    if not path.exists():
        return None
    out = {"puts": [], "calls": [], "shares": [], "problems": []}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        out["problems"].append(f"positions.yaml could not be read: {str(exc).splitlines()[0]}")
        return out
    if not isinstance(raw, dict):
        out["problems"].append("positions.yaml must contain the sections puts:, shares:, calls:")
        return out

    def fnum(row, key, required=True, positive=True):
        v = row.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            if required:
                raise ValueError(f"'{key}' missing or not a number")
            return None
        if positive and v <= 0:
            raise ValueError(f"'{key}' must be positive")
        return float(v)

    def sym(row):
        v = row.get("symbol")
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'symbol' missing (quote symbols like ON, NO: \"ON\")")
        return v.strip().upper()

    for kind in ("puts", "calls"):
        for i, row in enumerate(raw.get(kind) or [], 1):
            try:
                if not isinstance(row, dict):
                    raise ValueError("entry must be key: value lines")
                raw_exp = row.get("expiry")
                expiry = raw_exp if isinstance(raw_exp, dt.date) and not isinstance(raw_exp, dt.datetime) else (
                    raw_exp.date() if isinstance(raw_exp, dt.datetime) else to_date(raw_exp, NY))
                if expiry is None:
                    raise ValueError("'expiry' missing (write it as 2026-12-18)")
                n = int(fnum(row, "contracts"))
                out[kind].append({"symbol": sym(row), "expiry": expiry, "strike": fnum(row, "strike"),
                                  "contracts": n, "premium": fnum(row, "premium"),
                                  "expired": expiry < today, "side": kind[:-1], "note": str(row.get("note") or "")})
            except (ValueError, TypeError) as exc:
                out["problems"].append(f"{kind}[{i}]: {exc}")
    for i, row in enumerate(raw.get("shares") or [], 1):
        try:
            if not isinstance(row, dict):
                raise ValueError("entry must be key: value lines")
            out["shares"].append({"symbol": sym(row), "shares": fnum(row, "shares"), "cost": fnum(row, "cost"),
                                  "premium_collected": fnum(row, "premium_collected", False, False) or 0.0,
                                  "note": str(row.get("note") or "")})
        except (ValueError, TypeError) as exc:
            out["problems"].append(f"shares[{i}]: {exc}")
    return out


def positions_exposure(pos: dict | None) -> float:
    """Açık (vadesi geçmemiş) putların assignment tutarı: strike x 100 x kontrat."""
    if not pos:
        return 0.0
    return sum(p["strike"] * 100 * p["contracts"] for p in pos["puts"] if not p["expired"])


def wheel_settings(cfg: dict) -> dict:
    w = cfg.get("wheel") or {}
    return {"enabled": w.get("enabled", True), "profit_take_pct": float(w.get("profit_take_pct", 50)),
            "near_strike_pct": float(w.get("near_strike_pct", 3)), "expiry_warn_days": int(w.get("expiry_warn_days", 7))}


def mark_held(cands: list[dict], wheel: dict | None) -> None:
    """Adaylara 'held' yazar: o hissede zaten hisse veya açık put varsa (aynı hissede riski katlamamak için)."""
    held: dict[str, list[str]] = {}
    if wheel:
        for sh in wheel["shares"]:
            held.setdefault(sh["symbol"], []).append(f"{sh['shares']:,.0f} shares")
        for r in wheel["options"]:
            if r["kind"] == "puts" and not r["expired"]:
                held.setdefault(r["symbol"], []).append(f"{r['contracts']} open put" + ("s" if r["contracts"] > 1 else ""))
    for c in cands:
        c["held"] = " + ".join(held.get(c["symbol"], []))


def cc_settings(cfg: dict) -> dict:
    c = (cfg.get("wheel") or {}).get("cc") or {}
    return {"dte_min": int(c.get("dte_min", 20)), "dte_max": int(c.get("dte_max", 45)),
            "delta_min": float(c.get("delta_min", 0.15)), "delta_max": float(c.get("delta_max", 0.35)),
            "min_oi": float(c.get("min_oi", 100)), "min_bid": float(c.get("min_bid", 0.20)),
            "max_spread_pct": float(c.get("max_spread_pct", 10)), "top_n": int(c.get("top_n", 4))}


def cc_candidates(rows: list[dict], spot: float, net_cost: float, today: dt.date, cc: dict,
                  next_earn: dt.date | None, is_etf: bool) -> dict:
    """Covered call adayları: net maliyetin altında zarara kilitleyenler ayrı tutulur."""
    good, loss = [], []
    for row in rows:
        exp = to_date(row.get("expiration"), NY)
        strike, delta = num(row.get("strike")), num(row.get("delta"))
        bid, ask, oi = num(row.get("bid")), num(row.get("ask")), num(row.get("openInterest"))
        if exp is None or strike is None or strike <= 0 or delta is None or bid is None or not ask or ask <= 0:
            continue
        if str(row.get("side", "call")).lower() != "call":
            continue
        dte = (exp - today).days
        spread = 100 * (ask - bid) / ask
        if not (cc["dte_min"] <= dte <= cc["dte_max"]) or not (cc["delta_min"] <= abs(delta) <= cc["delta_max"]):
            continue
        if bid < cc["min_bid"] or spread >= cc["max_spread_pct"] or oi is None or oi < cc["min_oi"]:
            continue
        static = bid / net_cost * 100 if net_cost > 0 else 0.0
        called = (strike + bid - net_cost) / net_cost * 100 if net_cost > 0 else 0.0
        item = {"expiry": exp, "dte": dte, "strike": strike, "bid": bid, "ask": ask, "delta": abs(delta), "oi": oi,
                "spread_pct": spread, "otm_pct": (strike - spot) / spot * 100, "static_pct": static,
                "annualized_pct": static * 365 / dte, "called_pct": called, "called_annual_pct": called * 365 / dte,
                "spans_earnings": bool(next_earn and exp >= next_earn and not is_etf), "loss_if_called": strike + bid < net_cost}
        (loss if item["loss_if_called"] else good).append(item)
    good.sort(key=lambda r: -r["annualized_pct"])
    loss.sort(key=lambda r: -r["annualized_pct"])
    return {"good": good[: cc["top_n"]], "good_total": len(good), "loss": loss[:3], "loss_total": len(loss)}


def _option_status(p: dict, spot, ask, captured, today: dt.date, ws: dict, next_earn) -> list[tuple[str, str]]:
    """(rozet metni, renk türü) listesi. renk: red / amber / green / grey"""
    tags = []
    dte = (p["expiry"] - today).days
    if p["expired"]:
        return [("EXPIRED - remove or update in positions.yaml", "red")]
    if spot is not None:
        if p["side"] == "put":
            itm, dist = spot < p["strike"], (spot - p["strike"]) / spot * 100
        else:
            itm, dist = spot > p["strike"], (p["strike"] - spot) / spot * 100
        if itm:
            tags.append(("ITM: assignment risk" if p["side"] == "put" else "ITM: shares may be called away", "red"))
        elif dist < ws["near_strike_pct"]:
            tags.append((f"near strike ({dist:.1f}%)", "amber"))
    if captured is not None and captured >= ws["profit_take_pct"]:
        tags.append((f"{captured:.0f}% of profit captured: consider closing", "green"))
    if dte <= ws["expiry_warn_days"]:
        tags.append((f"expires in {dte}d", "amber"))
    if next_earn and next_earn <= p["expiry"]:
        tags.append((f"earnings {next_earn:%d %b} before expiry", "amber"))
    return tags


def run_wheel(cfg: dict, md, pos: dict | None, today: dt.date) -> dict | None:
    """Pozisyonlar için canlı fiyatları çeker. Tek pozisyon hatası diğerlerini durdurmaz."""
    if not pos or not wheel_settings(cfg)["enabled"]:
        return None
    ws = wheel_settings(cfg)
    rcfg = earnings_rules(cfg)
    symbols = sorted({p["symbol"] for k in ("puts", "calls", "shares") for p in pos[k]})
    spots, earns, errors = {}, {}, []
    etfs = etf_set(cfg)
    for symbol in symbols:
        try:
            q = md.quote(symbol)
            spots[symbol] = num(q.get("last")) if q else None
        except MarketDataAuthError:
            raise
        except MarketDataError as exc:
            errors.append((symbol, f"quote failed: {exc}"))
            spots[symbol] = None
        if symbol in etfs:
            continue
        try:
            dates = [d for d in md.earnings_dates(symbol, today, today + dt.timedelta(days=rcfg["lookahead"])) if d >= today]
            earns[symbol] = min(dates) if dates else None
        except MarketDataAuthError:
            raise
        except MarketDataError:
            earns[symbol] = None
    rows = []
    for kind in ("puts", "calls"):
        for p in pos[kind]:
            row = dict(p, kind=kind, spot=spots.get(p["symbol"]), bid=None, ask=None, delta=None, error="",
                       dte=(p["expiry"] - today).days)
            if not p["expired"]:
                try:
                    q = md.option_quote(occ_symbol(p["symbol"], p["expiry"], p["side"], p["strike"]))
                    if q:
                        row["bid"], row["ask"], row["delta"] = num(q.get("bid")), num(q.get("ask")), num(q.get("delta"))
                    else:
                        row["error"] = "no quote returned for this contract (check strike/expiry)"
                except MarketDataAuthError:
                    raise
                except MarketDataError as exc:
                    row["error"] = f"quote failed: {exc}"
            # geri alma maliyeti = ask (satıcı kapatmak için alıcı olur)
            ask = row["ask"] if row["ask"] is not None else row["bid"]
            row["close_cost"] = ask
            row["captured_pct"] = (p["premium"] - ask) / p["premium"] * 100 if ask is not None else None
            row["pnl_usd"] = (p["premium"] - ask) * 100 * p["contracts"] if ask is not None else None
            row["tags"] = _option_status(row, row["spot"], ask, row["captured_pct"], today, ws, earns.get(p["symbol"]))
            rows.append(row)
    shares = []
    for sh in pos["shares"]:
        spot = spots.get(sh["symbol"])
        net = sh["cost"] - sh["premium_collected"]
        shares.append(dict(sh, spot=spot, net_cost=net,
                           pnl_usd=(spot - sh["cost"]) * sh["shares"] if spot is not None else None,
                           pnl_with_prem_usd=(spot - net) * sh["shares"] if spot is not None else None,
                           pnl_with_prem_pct=(spot - net) / net * 100 if spot is not None and net > 0 else None,
                           below_net=spot is not None and spot < net))
    cc_cfg = cc_settings(cfg)
    cc_out = []
    for sh in shares:
        sym = sh["symbol"]
        covered = sum(c["contracts"] for c in pos["calls"] if c["symbol"] == sym and not c["expired"])
        whole = int(sh["shares"] // 100)
        avail = whole - covered
        item = {"symbol": sym, "shares": sh["shares"], "spot": sh["spot"], "net_cost": sh["net_cost"], "cost": sh["cost"],
                "whole": whole, "covered": covered, "avail": avail, "error": "", "cands": None,
                "next_earn": earns.get(sym)}
        if whole < 1:
            item["error"] = "fewer than 100 shares: a covered call needs at least 100"
        elif avail < 1:
            item["error"] = f"all {whole} contract(s) of this stock already have a call sold"
        elif sh["spot"] is None:
            item["error"] = "no stock price"
        else:
            try:
                chain = md.option_chain(sym, today + dt.timedelta(days=cc_cfg["dte_min"]),
                                        today + dt.timedelta(days=cc_cfg["dte_max"]), "call")
                item["cands"] = cc_candidates(chain, sh["spot"], sh["net_cost"], today, cc_cfg,
                                              earns.get(sym), sym in etfs)
            except MarketDataAuthError:
                raise
            except MarketDataError as exc:
                item["error"] = f"call chain failed: {exc}"
        cc_out.append(item)
    live_puts = [r for r in rows if r["kind"] == "puts" and not r["expired"]]
    return {"options": rows, "shares": shares, "cc": cc_out, "errors": errors, "problems": list(pos["problems"]),
            "exposure": positions_exposure(pos),
            "open_put_premium": sum(r["premium"] * 100 * r["contracts"] for r in live_puts),
            "unrealized": sum(r["pnl_usd"] for r in rows if r["pnl_usd"] is not None and not r["expired"])}


# --------------------------------------------------------------------------- filtreler
def check_underlying(quote: dict | None, ucfg: dict) -> tuple[dict, str | None]:
    info = {"last": None, "volume": None, "change_pct": None}
    if not quote:
        return info, "no_quote"
    last, volume, change = num(quote.get("last")), num(quote.get("volume")), num(quote.get("change"))
    info.update(last=last, volume=volume)
    if last is not None and change is not None and last - change > 0:
        info["change_pct"] = change / (last - change) * 100
    if last is None:
        return info, "no_quote"
    if price_out_of_range(last, ucfg):
        return info, "price_range"
    if volume is None or not volume > ucfg["volume_gt"]:
        return info, "underlying_volume"
    change_pct = info["change_pct"]
    if change_pct is None or not (ucfg["daily_change_pct_min"] <= change_pct <= ucfg["daily_change_pct_max"]):
        return info, "daily_change"
    return info, None


def underlying_detail(reason: str, info: dict) -> str:
    if reason == "price_range" and info.get("last") is not None:
        return f"price ${info['last']:,.2f}"
    if reason == "underlying_volume":
        vol = info.get("volume")
        return "no volume" if vol is None else f"volume {vol / 1e6:.2f}M"
    if reason == "daily_change":
        chg = info.get("change_pct")
        return "no change data" if chg is None else f"daily {chg:+.1f}%"
    return UNDERLYING_LABELS.get(reason, reason)


def evaluate_contracts(rows: list[dict], symbol: str, spot: float, today: dt.date,
                       cfg: dict, funnel: Counter, near: list | None = None) -> list[dict]:
    """Filters contracts and returns those passing all filters.

    If `near` is given, contracts failing exactly ONE contract filter (for delta: just
    outside the band) are appended there as near-misses.
    """
    ocfg, rcfg = cfg["option"], cfg["return"]
    delta_tol = cfg.get("output", {}).get("near_miss_delta_tolerance", 0.05)
    out = []
    for row in rows:
        funnel["contracts_total"] += 1
        exp = to_date(row.get("expiration"), NY)
        strike, delta = num(row.get("strike")), num(row.get("delta"))
        bid, ask = num(row.get("bid")), num(row.get("ask"))
        missing = []
        if exp is None:
            missing.append("expiry")
        if strike is None or strike <= 0:
            missing.append("strike")
        if delta is None:
            missing.append("delta")
        if bid is None:
            missing.append("bid")
        if ask is None or ask <= 0:
            missing.append("ask")
        if missing:
            funnel["rej_missing"] += 1
            for name in missing:   # bir kontrat birden fazla alanda eksik olabilir
                funnel["missing_" + name] += 1
            continue
        if str(row.get("side", "put")).lower() != "put":
            funnel["rej_missing"] += 1
            continue
        dte = (exp - today).days
        if dte <= 0:
            funnel["rej_dte"] += 1
            continue
        delta = -abs(delta)  # put delta'sı negatif kabul edilir
        oi, volume = num(row.get("openInterest")), num(row.get("volume"))
        spread_pct = 100 * (ask - bid) / ask
        premium = bid if rcfg["premium_basis"] == "bid" else (bid + ask) / 2
        base = strike if rcfg["base"] == "strike" else strike - premium
        if base <= 0:
            funnel["rej_missing"] += 1
            continue
        static_return = premium / base
        annualized = static_return * 365 / dte * 100

        fails: dict[str, str] = {}
        if not (ocfg["dte_min"] <= dte <= ocfg["dte_max"]):
            fails["dte"] = f"DTE {dte}"
        if not (ocfg["delta_min"] <= delta <= ocfg["delta_max"]):
            fails["delta"] = f"delta {delta:.2f} (band {ocfg['delta_min']}…{ocfg['delta_max']})"
        if oi is None or oi < ocfg["open_interest_min"]:
            fails["oi"] = "no OI" if oi is None else f"OI {oi:,.0f} < {ocfg['open_interest_min']}"
        if volume is None or volume < ocfg["option_volume_min"]:
            fails["volume"] = "no volume" if volume is None else f"volume {volume:,.0f} < {ocfg['option_volume_min']}"
        if not bid > ocfg["bid_gt"]:
            fails["bid"] = f"bid {bid:.2f} ≤ {ocfg['bid_gt']}"
        if not spread_pct < ocfg["spread_pct_lt"]:
            fails["spread"] = f"spread {spread_pct:.1f}% ≥ {ocfg['spread_pct_lt']}%"
        if not annualized > rcfg["annualized_gt_pct"]:
            fails["return"] = f"annualized {annualized:.1f}% ≤ {rcfg['annualized_gt_pct']}%"

        cand = {
            "symbol": symbol, "option_symbol": row.get("optionSymbol", ""), "spot": spot,
            "expiry": exp, "dte": dte, "strike": strike, "bid": bid, "ask": ask,
            "delta": delta, "iv": num(row.get("iv")), "open_interest": oi, "volume": volume,
            "spread_pct": spread_pct, "otm_pct": (spot - strike) / spot * 100,
            "premium": premium, "static_return_pct": static_return * 100,
            "annualized_pct": annualized, "breakeven": strike - premium,
            "collateral": strike * 100, "earnings_date": None, "last_earnings": None, "is_etf": False,
            "iv_status": None, "iv_rank": None, "iv_pct": None, "iv_n": None,
            "earnings_note": "", "spans_earnings": False, "score": None, "score_parts": {},
            "miss_code": None, "miss_detail": "",
            "score_cap": "", "hv": None, "iv_hv": None, "group": "", "is_new": None, "streak": 1,
        }
        if not fails:
            out.append(cand)
            continue

        first = next(code for code in CONTRACT_FILTER_ORDER if code in fails)
        funnel["rej_" + first] += 1   # huni sayımı: ilk takılan filtre

        if near is not None and len(fails) == 1:
            code = first
            if code == "dte":
                continue
            if code == "delta" and not (ocfg["delta_min"] - delta_tol <= delta <= ocfg["delta_max"] + delta_tol):
                continue
            cand["miss_code"], cand["miss_detail"] = code, fails[code]
            near.append(cand)
    return out


# --------------------------------------------------------------------------- tarama
@dataclass
class ScanResult:
    candidates: list[dict] = field(default_factory=list)
    near_misses: list[dict] = field(default_factory=list)
    funnel: Counter = field(default_factory=Counter)
    underlying_rejects: dict[str, str] = field(default_factory=dict)
    underlying_info: dict[str, dict] = field(default_factory=dict)
    earnings_unknown: list[str] = field(default_factory=list)
    last_earnings: dict[str, dt.date] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    symbols_total: int = 0
    symbols_scanned: int = 0
    api_calls: int = 0
    iv_building: bool = False
    listed: list[dict] = field(default_factory=list)      # top_n kesilmeden önceki tüm geçenler (hisse sınırı sonrası)
    hv: dict[str, float] = field(default_factory=dict)
    changes: dict | None = None
    wheel: dict | None = None


def run_scan(cfg: dict, md, history: IVHistory, today: dt.date, symbols: list[str]) -> ScanResult:
    result = ScanResult(symbols_total=len(symbols))
    ocfg, icfg = cfg["option"], cfg["iv"]
    rules = earnings_rules(cfg)
    etfs = etf_set(cfg)
    all_candidates: list[dict] = []

    for symbol in symbols:
        try:
            quote = md.quote(symbol)
            rows = md.put_chain(symbol, today + dt.timedelta(days=ocfg["dte_min"]),
                                today + dt.timedelta(days=ocfg["dte_max"]))
        except MarketDataAuthError:
            raise
        except MarketDataError as exc:
            result.errors.append((symbol, str(exc)))
            continue
        result.symbols_scanned += 1

        # IV geçmişini her hisse için biriktir (hisse filtresini geçmese bile)
        current_iv = iv_sample(rows, today, ocfg, icfg)
        metrics = iv_metrics(history, symbol, today, current_iv, icfg)
        if current_iv is not None:
            history.set(symbol, today, current_iv)

        info, reason = check_underlying(quote, cfg["underlying"])
        if reason:
            result.underlying_rejects[symbol] = reason
            result.underlying_info[symbol] = info
            near_code = UNDERLYING_NEAR_CODE.get(reason)
            if near_code and info["last"] is not None:
                # Sadece hisse filtresi yüzünden elenen kontratlar (ana huniyi etkilemez, ek API çağrısı yok)
                for cand in evaluate_contracts(rows, symbol, info["last"], today, cfg, Counter()):
                    cand["miss_code"] = near_code
                    cand["miss_detail"] = underlying_detail(reason, info)
                    result.near_misses.append(cand)
            continue

        near_contracts: list[dict] = []
        candidates = evaluate_contracts(rows, symbol, info["last"], today, cfg, result.funnel, near_contracts)
        if not candidates and not near_contracts:
            continue

        # Earnings kuralı (sembol başına tek çağrı)
        next_earnings = None
        last_earnings = None
        earnings_failed = False
        is_etf = symbol.upper() in etfs
        if not is_etf:
            try:
                dates = md.earnings_dates(symbol, today - dt.timedelta(days=rules["lookback"]),
                                          today + dt.timedelta(days=rules["lookahead"]))
                upcoming = [d for d in dates if d >= today]
                past = [d for d in dates if d < today]
                next_earnings = min(upcoming) if upcoming else None
                last_earnings = max(past) if past else None
            except MarketDataAuthError:
                raise
            except MarketDataError as exc:
                earnings_failed = True
                result.errors.append((symbol, f"earnings lookup failed: {exc}"))
            if next_earnings is None:
                result.earnings_unknown.append(symbol)
                if last_earnings:
                    result.last_earnings[symbol] = last_earnings
        unknown_note = "lookup failed" if earnings_failed else "unknown"

        def earnings_status(cand):
            if is_etf:
                return None
            if next_earnings is None:
                return "earnings_unknown" if rules["unknown"] == "reject" else None
            if (next_earnings - today).days < rules["min_days"]:
                return "earnings"
            if rules["expiry_before"] and cand["expiry"] >= next_earnings:
                return "earnings"
            return None

        def earnings_detail(cand, status):
            if status == "earnings":
                days = (next_earnings - today).days
                if days < rules["min_days"]:
                    return f"earnings {next_earnings:%d %b %y} (in {days} days) < {rules['min_days']} days"
                return f"expiry {cand['expiry']:%d %b %y} ≥ earnings {next_earnings:%d %b %y}"
            return "earnings date not found"

        kept = []
        for cand in candidates:
            status = earnings_status(cand)
            if status:
                result.funnel["rej_" + status] += 1
                cand["miss_code"], cand["miss_detail"] = status, earnings_detail(cand, status)
                result.near_misses.append(cand)
                continue
            mark_earnings(cand, next_earnings, unknown_note, last_earnings, is_etf)
            if next_earnings is None and not is_etf:
                result.funnel["warn_earnings_unknown"] += 1
            kept.append(cand)

        # IV rank / percentile kuralı
        for cand in kept:
            apply_iv(cand, metrics)
            if metrics["status"] != "OK":
                result.iv_building = True
            if not iv_passes(metrics, icfg):
                result.funnel["rej_iv"] += 1
                if metrics["status"] == "OK":
                    detail = f"IVR {metrics['rank']:.0f} / IVP {metrics['pct']:.0f}"
                else:
                    detail = f"IV history insufficient ({metrics.get('n')} days)"
                cand["miss_code"], cand["miss_detail"] = "iv", detail
                result.near_misses.append(cand)
                continue
            all_candidates.append(cand)

        # Tek kontrat filtresine takılanlar: earnings ve IV'yi de geçiyorlarsa gerçek "yakın kaçan"
        for cand in near_contracts:
            if earnings_status(cand) or not iv_passes(metrics, icfg):
                continue
            mark_earnings(cand, next_earnings, unknown_note, last_earnings, is_etf)
            apply_iv(cand, metrics)
            result.near_misses.append(cand)

    # Sırala: yıllık getiri; hisse başına sınır; ilk N
    for cand in all_candidates:
        cand["score"], cand["score_parts"] = score_candidate(cand, cfg)
    if (cfg.get("output", {}) or {}).get("sort_by", "score") == "annualized":
        all_candidates.sort(key=lambda c: (-c["annualized_pct"], c["spread_pct"]))
    else:
        all_candidates.sort(key=lambda c: (-(c["score"] if c["score"] is not None else -1),
                                           -c["annualized_pct"], c["symbol"]))
    per_symbol: Counter = Counter()
    selected = []
    for cand in all_candidates:
        if per_symbol[cand["symbol"]] >= cfg["output"]["max_per_symbol"]:
            continue
        per_symbol[cand["symbol"]] += 1
        selected.append(cand)
    result.funnel["passed"] = len(all_candidates)
    result.candidates = selected[: cfg["output"]["top_n"]]
    result.listed = selected

    gmap = group_map(cfg)
    for cand in selected:
        cand["group"] = gmap.get(cand["symbol"].upper(), "Other")

    # HV (tarihsel volatilite): yalnızca raporda gösterilen hisseler için (1000 mum = 1 kredi)
    hcfg = cfg.get("hv") or {}
    fetch = getattr(md, "daily_closes", None)
    if hcfg.get("enabled", True) and fetch is not None:
        days = int(hcfg.get("days", 20))
        for symbol in sorted({c["symbol"] for c in result.candidates}):
            try:
                closes = fetch(symbol, today - dt.timedelta(days=days * 2 + 12), today)
            except MarketDataAuthError:
                raise
            except MarketDataError:
                continue
            hv = hv_from_closes(closes, days)
            if hv:
                result.hv[symbol] = hv
        for cand in result.candidates:
            hv = result.hv.get(cand["symbol"])
            cand["hv"] = hv
            if hv and cand.get("iv"):
                cand["iv_hv"] = cand["iv"] * 100 / hv
    result.api_calls = getattr(md, "calls", 0)
    return result


def group_near_misses(result: ScanResult, cfg: dict) -> list[tuple[str, str, int, list[dict]]]:
    """Returns [(code, title, total, contracts_to_show)]."""
    per_reason = cfg.get("output", {}).get("near_miss_per_reason", 4)
    per_symbol_cap = cfg.get("output", {}).get("near_miss_max_per_symbol", 2)
    groups = []
    for code, label in NEAR_LABELS:
        items = [c for c in result.near_misses if c["miss_code"] == code]
        if not items:
            continue
        items.sort(key=lambda c: (-c["annualized_pct"], c["spread_pct"]))
        shown, count = [], Counter()
        for c in items:
            if count[c["symbol"]] >= per_symbol_cap:
                continue
            count[c["symbol"]] += 1
            shown.append(c)
            if len(shown) >= per_reason:
                break
        groups.append((code, label, len(items), shown))
    return groups


# --------------------------------------------------------------------------- rapor
def fmt(value, digits=2, suffix=""):
    return "-" if value is None else f"{value:,.{digits}f}{suffix}"


def iv_text(c: dict) -> str:
    if c["iv_status"] == "OK":
        return f"{c['iv_rank']:.0f} / {c['iv_pct']:.0f}"
    if c["iv_status"] == "BUILDING":
        return f"building ({c['iv_n']})"
    return "n/a"


def underlying_reject_groups(result: ScanResult) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for symbol, reason in sorted(result.underlying_rejects.items(), key=lambda kv: str(kv[0])):
        detail = underlying_detail(reason, result.underlying_info.get(symbol, {}))
        grouped.setdefault(UNDERLYING_LABELS.get(reason, reason), []).append(f"{symbol} ({detail})")
    return grouped


def build_text(result: ScanResult, cfg: dict, today: dt.date) -> str:
    lines = [f"CSP Scanner - {today:%d %b %Y}",
             f"{len(result.candidates)} candidates | {result.symbols_scanned}/{result.symbols_total} stocks scanned | "
             f"{result.api_calls} API calls", ""]
    for c in result.candidates:
        earnings = "ETF" if c.get("is_etf") else (f"{c['earnings_date']:%d %b %y}" if c["earnings_date"] else "-")
        score = f"[{c['score']:>3}] " if c.get("score") is not None else ""
        lines.append(
            f"{score}{c['symbol']:<6} {c['expiry']:%d %b %y} ({c['dte']}d) strike {c['strike']:.2f} "
            f"bid {c['bid']:.2f} delta {c['delta']:.2f} annualized {c['annualized_pct']:.1f}% "
            f"OTM {c['otm_pct']:.1f}% collateral ${c['collateral']:,.0f} "
            f"IVR/IVP {iv_text(c)} earnings {earnings}"
            + (f" !! {c['earnings_note']}" if c.get("earnings_note") else "")
            + (f" IV/HV {c['iv_hv']:.2f}" if c.get("iv_hv") else "")
            + (" NEW" if c.get("is_new") else (f" day{c['streak']}" if c.get("streak", 1) > 1 else ""))
            + (f" [capped: {c['score_cap']}]" if c.get("score_cap") else "")
            + (f" !! ALREADY HOLD: {c['held']}" if c.get("held") else ""))
    w = result.wheel
    if w:
        lines.append("")
        lines.append(f"WHEEL: {len([r for r in w['options'] if r['kind'] == 'puts' and not r['expired']])} open puts, "
                     f"exposure ${w['exposure']:,.0f}, open P/L {_usd(w['unrealized'], True)}")
        for r in w["options"]:
            tags = "; ".join(t for t, _ in r["tags"])
            cap = "-" if r["captured_pct"] is None else f"{r['captured_pct']:.0f}%"
            lines.append(f"  {r['symbol']:<6} {r['side']} {r['expiry']:%d %b %y} ({r['dte']}d) strike {r['strike']:.2f} "
                         f"x{r['contracts']} sold {r['premium']:.2f} buy-back "
                         f"{'-' if r['close_cost'] is None else format(r['close_cost'], '.2f')} captured {cap}"
                         + (f" !! {tags}" if tags else "") + (f" ({r['error']})" if r["error"] else ""))
        for sh in w["shares"]:
            lines.append(f"  {sh['symbol']:<6} shares {sh['shares']:.0f} cost {sh['cost']:.2f} net {sh['net_cost']:.2f} "
                         f"P/L with premium {_usd(sh['pnl_with_prem_usd'], True)}")
        for it in w.get("cc", []):
            if it["error"] or not it["cands"]:
                lines.append(f"  CC {it['symbol']}: {it['error'] or 'none'}")
                continue
            for r in (it["cands"]["good"] or it["cands"]["loss"]):
                lines.append(f"  CC {it['symbol']:<6} {r['expiry']:%d %b %y} ({r['dte']}d) strike {r['strike']:.2f} "
                             f"bid {r['bid']:.2f} delta {r['delta']:.2f} annual {r['annualized_pct']:.1f}% "
                             f"if called {r['called_pct']:+.1f}%" + (" !! loss if called" if r["loss_if_called"] else "")
                             + (" !! spans earnings" if r["spans_earnings"] else ""))
        for x in w["problems"]:
            lines.append(f"  !! positions.yaml: {x}")
    ch = result.changes
    if ch:
        lines += ["", f"Changes since {ch['prev_date']:%d %b}: {len(ch['new'])} new, {len(ch['dropped'])} dropped, "
                      f"{len(ch['movers'])} moved, {ch['unchanged']} unchanged"]
        for c in ch["new"][:8]:
            lines.append(f"  NEW  {c['symbol']:<6} {c['expiry']:%d %b %y} {c['strike']:.2f} bid {c['bid']:.2f}")
        for d in ch["dropped"][:8]:
            lines.append(f"  GONE {d['symbol']:<6} {d['expiry']} {d['strike']:.2f} - {d['why']}")
    basket = build_basket(result.candidates, cfg)
    if basket:
        lines += ["", f"Budget: limit ${basket['limit']:,.0f}, used ${basket['used']:,.0f}, "
                      f"available ${basket['available']:,.0f}. Example basket (by score, 1 per stock):"]
        for c in basket["picks"]:
            lines.append(f"  {c['symbol']:<6} {c['expiry']:%d %b %y} strike {c['strike']:.2f} "
                         f"collateral ${c['collateral']:,.0f} premium ${c['premium'] * 100:,.0f}")
        lines.append(f"  Total collateral ${basket['total']:,.0f}, premium ${basket['income']:,.0f}, left ${basket['left']:,.0f}")
    lines += ["", "Filter summary:"]
    for key, label in FUNNEL_LABELS:
        lines.append(f"  {label}: {result.funnel.get(key, 0)}")
    detail = missing_breakdown(result)
    if detail:
        lines.append(f"  (Missing-data breakdown; one contract can miss several fields: {detail})")

    groups = group_near_misses(result, cfg)
    lines += ["", "Near misses (contracts that failed exactly one filter):"]
    if not groups:
        lines.append("  none")
    for _, label, total, shown in groups:
        lines.append(f"  {label} - {total} contracts total")
        for c in shown:
            lines.append(f"    {c['symbol']:<6} {c['expiry']:%d %b %y} ({c['dte']}d) strike {c['strike']:.2f} "
                         f"bid {c['bid']:.2f} delta {c['delta']:.2f} annualized {c['annualized_pct']:.1f}% "
                         f"-> {c['miss_detail']}")
    if result.earnings_unknown:
        lines += ["", "Stocks with no earnings date found: " + ", ".join(
            s_ + (f" (last {result.last_earnings[s_]:%d %b %y})" if s_ in result.last_earnings else "")
            for s_ in sorted(result.earnings_unknown, key=str))]
    grouped = underlying_reject_groups(result)
    if grouped:
        lines += ["", "Rejected at stock level:"]
        lines += [f"  {label}: {', '.join(items)}" for label, items in grouped.items()]
    if result.errors:
        lines += ["", "Errors:"] + [f"  {s}: {m}" for s, m in result.errors]
    lines += ["", "This list is research output, not investment advice."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTML tasarımı
# Email-client safe (Gmail etc.): table layout + inline styles, no scripts.
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK, MUTED, LINE, PAGE, NAVY = "#1c2733", "#6b7785", "#e3e8ee", "#eef1f5", "#0f2740"


def tr_date(day: dt.date) -> str:
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"]
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{days[day.weekday()]}, {months[day.month - 1]} {day.day}, {day.year}"


def _chip(text: str, bg: str, fg: str, title: str = "", extra: str = "") -> str:
    t = f' title="{html.escape(title, quote=True)}"' if title else ""
    return (f'<span{t} style="display:inline-block;padding:1px 8px;border-radius:10px;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:600;line-height:17px;white-space:nowrap;margin:0 4px 4px 0;{extra}">'
            f'{html.escape(text)}</span>')


def _cell(top: str, sub: str = "", align: str = "right", wrap: bool = False) -> str:
    sub_html = (f'<div style="font-size:11px;line-height:14px;color:{MUTED};margin-top:1px">{sub}</div>'
                if sub else "")
    nowrap = "" if wrap else "white-space:nowrap;"
    return (f'<td align="{align}" style="padding:7px 8px;text-align:{align};vertical-align:middle;{nowrap}'
            f'border-bottom:1px solid {LINE}"><div style="font-size:13px;line-height:17px;color:{INK}">{top}</div>'
            f'{sub_html}</td>')


def _th(label: str, align: str = "right") -> str:
    return (f'<th align="{align}" style="padding:8px;text-align:{align};font-size:10.5px;font-weight:700;color:{MUTED};'
            f'text-transform:uppercase;letter-spacing:.6px;background:#f6f8fb;border-bottom:1px solid {LINE};'
            f'white-space:nowrap">{html.escape(label)}</th>')


def _card(title: str, body: str, subtitle: str = "") -> str:
    sub = f'<div style="font-size:12px;color:{MUTED};margin-top:3px;line-height:17px">{subtitle}</div>' if subtitle else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;'
            f'border:1px solid {LINE};border-radius:10px;margin:0 0 14px"><tr><td style="padding:16px 18px">'
            f'<div style="font-size:15px;font-weight:700;color:{INK}">{title}</div>{sub}'
            f'<div style="margin-top:12px">{body}</div></td></tr></table>')


def _kpi(label: str, value: str, sub: str = "", pad: str = "0 3px") -> str:
    sub_html = f'<div style="font-size:11px;color:{MUTED};margin-top:2px;line-height:14px">{sub}</div>' if sub else ""
    return (f'<td width="25%" valign="top" style="padding:{pad}"><div style="background:#ffffff;border:1px solid {LINE};'
            f'border-radius:10px;padding:12px 14px"><div style="font-size:10.5px;font-weight:700;color:{MUTED};'
            f'letter-spacing:.7px;text-transform:uppercase">{html.escape(label)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{INK};margin-top:4px;line-height:26px">{value}</div>'
            f'{sub_html}</div></td>')


def _warn_chip(note: str) -> str:
    if "spans" in note:
        return _chip("⚠ expiry spans earnings", "#fff3d6", "#8a5a00", note)
    if "failed" in note:
        return _chip("⚠ date lookup failed", "#fde8e8", "#a12626", note)
    return _chip("⚠ date unknown", "#fff3d6", "#8a5a00", note)


def _score_colors(score: int) -> tuple[str, str, str]:
    """(badge background, text color, bar color)"""
    if score >= 70:
        return "#1f9d57", "#ffffff", "#1f9d57"
    if score >= 55:
        return "#cdeedb", "#11693f", "#5cbf86"
    if score >= 40:
        return "#fdefc3", "#7a5a00", "#e3b53d"
    return "#f9d5d5", "#9a2222", "#e07a7a"


def _score_cell(c: dict) -> str:
    score = c.get("score")
    if score is None:
        return _cell("—", "", "center")
    bg, fg, bar = _score_colors(score)
    tip = " · ".join(f"{SCORE_LABELS[k]} {v}" for k, v in (c.get("score_parts") or {}).items() if k in SCORE_LABELS)
    chip = (f'<span title="{html.escape(tip, quote=True)}" style="display:inline-block;min-width:36px;padding:3px 8px;'
            f'border-radius:7px;background:{bg};color:{fg};font-weight:700;font-size:14px;text-align:center">{score}</span>')
    meter = (f'<div style="height:3px;border-radius:2px;background:#e8edf2;width:44px;margin:5px auto 0">'
             f'<div style="height:3px;border-radius:2px;background:{bar};width:{score}%"></div></div>')
    cap = c.get("score_cap")
    if cap:
        meter += (f'<div title="{html.escape("Score capped: " + cap, quote=True)}" style="font-size:9.5px;color:#8a5a00;'
                  f'margin-top:3px;letter-spacing:.3px">CAPPED</div>')
    return _cell(chip, meter, "center")


def score_legend_html(cfg: dict) -> str:
    w = score_settings(cfg)["weights"]
    chips = "".join(_chip(label, *_score_colors(sample)[:2]) for label, sample in
                    (("70–100", 80), ("55–69", 60), ("40–54", 45), ("0–39", 20)))
    parts = " · ".join(f"{label} {w[key]:g}" for key, label in SCORE_LABELS.items() if w.get(key))
    return (f"<b>Score (0–100):</b> {chips} weighted average: {html.escape(parts)}. "
            "While IV history builds, the IV component is dropped and does not lower the score. "
            f"{_chip('CAPPED', '#fff3d6', '#8a5a00')} score limited because IV is very high or the expiry spans earnings.")


def _candidate_row(c: dict, idx: int, min_hist: int, today: dt.date | None = None, ex: dict | None = None) -> str:
    esc = html.escape
    bg = "#ffffff" if idx % 2 == 0 else "#fafbfd"
    earn = f"{c['earnings_date']:%d %b %y}" if c["earnings_date"] else "—"
    note = c.get("earnings_note", "")
    earn_sub = _warn_chip(note) if note else ""
    if c.get("is_etf"):
        earn, earn_sub = "", _chip("ETF", "#e8eefc", "#2c4a9a")
    elif c.get("last_earnings") and not c["earnings_date"]:
        ago = f" ({(today - c['last_earnings']).days} days ago)" if today else ""
        earn_sub += (f'<div style="font-size:10.5px;color:{MUTED};margin-top:3px">'
                     f'last report {c["last_earnings"]:%d %b %y}{ago}</div>')
    if c["iv_status"] == "OK":
        iv_top, iv_sub = f"{c['iv_rank']:.0f} / {c['iv_pct']:.0f}", f"{c['iv_n']} days"
    elif c["iv_status"] == "BUILDING":
        iv_top, iv_sub = f'<span style="color:{MUTED}">building</span>', f"{c['iv_n']}/{min_hist} days"
    else:
        iv_top, iv_sub = "—", ""
    iv_pct = f"IV {c['iv'] * 100:.0f}%" if c.get("iv") is not None else "IV —"
    if c.get("hv"):
        iv_pct += f" · HV {c['hv']:.0f}%"
    if c.get("iv_hv"):
        r = c["iv_hv"]
        rc = "#146c3a" if r >= 1.2 else ("#8a5a00" if r < 0.9 else MUTED)
        iv_pct += f'<div style="color:{rc};font-weight:700;margin-top:2px" title="IV divided by 20-day HV">IV/HV {r:.2f}</div>'
    badge = ""
    if c.get("is_new") is True:
        badge = " " + _chip("NEW", "#e3f4ea", "#146c3a")
    elif c.get("streak", 1) > 1:
        badge = " " + _chip(f"day {c['streak']}", "#eef2f7", "#44566c", "Days in a row on the list")
    grp = f'<div style="color:{MUTED};margin-top:2px">{esc(c.get("group") or "")}</div>' if c.get("group") else ""
    if ex:
        pct = c["collateral"] / ex["available"] * 100 if ex["available"] else 999
        if c["collateral"] > ex["available"]:
            coll_sub = _chip("over budget", "#fde8e8", "#a12626", f"Exceeds ${ex['available']:,.0f} available")
        else:
            coll_sub = f"{pct:.0f}% of budget"
        coll_sub += f'<div>breakeven {c["breakeven"]:,.2f}</div>'
    else:
        coll_sub = f"breakeven {c['breakeven']:,.2f}"
    if c.get("held"):
        grp += f'<div style="margin-top:3px">{_chip("already hold", "#fde8e8", "#a12626", c["held"])}</div>' \
               f'<div style="color:{MUTED};font-size:10.5px">{html.escape(c["held"])}</div>'
    pill = (f'<span style="display:inline-block;padding:2px 9px;border-radius:6px;background:#e3f4ea;color:#146c3a;'
            f'font-weight:700;font-size:13px">{c["annualized_pct"]:.1f}%</span>')
    cells = [
        _cell(f'<b style="font-size:14px">{esc(c["symbol"])}</b>{badge}', f"${c['spot']:,.2f}" + grp, "left"),
        _score_cell(c),
        _cell(f"{c['expiry']:%d %b %y}", f"{c['dte']} days", "left"),
        _cell(f"<b>{c['strike']:,.2f}</b>", f"OTM {c['otm_pct']:.1f}%"),
        _cell(f"{c['bid']:.2f} / {c['ask']:.2f}", f"spread {c['spread_pct']:.1f}%"),
        _cell(f"{c['delta']:.2f}", iv_pct),
        _cell(fmt(c["open_interest"], 0), f"vol {fmt(c['volume'], 0)}"),
        _cell(pill, f"period {c['static_return_pct']:.2f}%"),
        _cell(f"${c['collateral']:,.0f}", coll_sub),
        _cell(earn, earn_sub, "left"),
        _cell(iv_top, iv_sub),
    ]
    return f'<tr style="background:{bg}">' + "".join(cells) + "</tr>"


def _mini_table(headers: list[tuple[str, str]], rows: list[list[str]]) -> str:
    head = "".join(_th(label, al) for label, al in headers)
    body = ""
    for i, r in enumerate(rows):
        bg = "#ffffff" if i % 2 == 0 else "#fafbfd"
        body += f'<tr style="background:{bg}">' + "".join(
            _cell(v, "", al) for v, (_, al) in zip(r, headers)) + "</tr>"
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid {LINE};border-radius:8px;border-collapse:separate;border-spacing:0">'
            f'<tr>{head}</tr>{body}</table>')


def _sub_title(text: str, chip: str = "") -> str:
    return (f'<div style="margin:16px 0 6px;font-size:12.5px;font-weight:700;color:{INK}">{html.escape(text)} {chip}</div>')


def _changes_card(result: ScanResult, cfg: dict, today: dt.date) -> str:
    esc = html.escape
    ch = result.changes
    if not ch:
        return _card("Changes since last scan",
                     f'<div style="font-size:12.5px;color:{MUTED}">History starts today. The comparison with the previous '
                     f'scan appears from the next run.</div>')
    prev = ch["prev_date"]
    stats = (_chip(f"{len(ch['new'])} new", "#e3f4ea", "#146c3a")
             + _chip(f"{len(ch['dropped'])} dropped", "#fde8e8", "#a12626")
             + _chip(f"{len(ch['movers'])} moved", "#fff3d6", "#8a5a00")
             + _chip(f"{ch['unchanged']} unchanged", "#eef2f7", "#44566c"))
    body = f'<div style="margin-bottom:4px">{stats}</div>'
    if ch["new"]:
        shown = ch["new"][:8]
        rows = [[f"<b>{esc(c['symbol'])}</b>", f"{c['expiry']:%d %b %y}", f"{c['strike']:,.2f}", f"{c['bid']:.2f}",
                 f"{c['annualized_pct']:.1f}%", "—" if c.get("score") is None else str(c["score"])] for c in shown]
        extra = f" · top {len(shown)} of {len(ch['new'])}" if len(ch["new"]) > len(shown) else ""
        body += _sub_title("New today", _chip(f"{len(ch['new'])}{extra}", "#eef2f7", "#44566c"))
        body += _mini_table([("Stock", "left"), ("Expiry", "left"), ("Strike", "right"), ("Bid", "right"),
                             ("Annual", "right"), ("Score", "right")], rows)
    if ch["dropped"]:
        shown = ch["dropped"][:8]
        rows = [[f"<b>{esc(d['symbol'])}</b>", esc(str(d["expiry"])), f"{d['strike']:,.2f}", esc(d["why"])] for d in shown]
        extra = f" · top {len(shown)} of {len(ch['dropped'])}" if len(ch["dropped"]) > len(shown) else ""
        body += _sub_title("Dropped since last scan", _chip(f"{len(ch['dropped'])}{extra}", "#eef2f7", "#44566c"))
        body += _mini_table([("Stock", "left"), ("Expiry", "left"), ("Strike", "right"), ("Why it left", "left")], rows)
    if ch["movers"]:
        shown = ch["movers"][:6]
        rows = []
        for m in shown:
            c = m["cand"]
            col = "#146c3a" if m["bid_chg"] >= 0 else "#a12626"
            ps = "—" if m["prev_score"] is None else f"{m['prev_score']:.0f}"
            ns = "—" if c.get("score") is None else str(c["score"])
            rows.append([f"<b>{esc(c['symbol'])}</b>", f"{c['expiry']:%d %b %y}", f"{c['strike']:,.2f}",
                         f'{m["prev_bid"]:.2f} → {c["bid"]:.2f} <span style="color:{col};font-weight:700">'
                         f'({m["bid_chg"]:+.0f}%)</span>', f"{ps} → {ns}"])
        body += _sub_title("Biggest movers", _chip("bid ±15% or score ±8", "#eef2f7", "#44566c"))
        body += _mini_table([("Stock", "left"), ("Expiry", "left"), ("Strike", "right"), ("Bid", "right"),
                             ("Score", "right")], rows)
    return _card(f"Changes since last scan ({prev:%d %b})", body,
                 "Compared with the previous scan day. Same stock + expiry + strike = same contract.")


def _budget_card(result: ScanResult, cfg: dict) -> str:
    esc = html.escape
    basket = build_basket(result.candidates, cfg)
    if not basket:
        return ""
    over = [c for c in result.candidates if c["collateral"] > basket["available"]]
    tiles = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 6px"><tr>'
             + _kpi("Limit", f"${basket['limit']:,.0f}", "assignment exposure (naked puts)", "0 3px 0 0")
             + _kpi("Already used", f"${basket['used']:,.0f}", _used_sub(basket), "0 3px")
             + _kpi("Available", f"${basket['available']:,.0f}", "for new positions", "0 0 0 3px")
             + "</tr></table>")
    if basket["picks"]:
        rows = []
        running = 0.0
        for c in basket["picks"]:
            running += c["collateral"]
            warn = " ⚠" if c.get("earnings_note") else ""
            if c.get("held"):
                warn += " " + _chip("already hold", "#fde8e8", "#a12626", c["held"])
            rows.append([f"<b>{esc(c['symbol'])}</b>{warn}", esc(c.get("group") or ""), f"{c['expiry']:%d %b %y}",
                         f"{c['strike']:,.2f}", f"${c['collateral']:,.0f}", f"${c['premium'] * 100:,.0f}",
                         "—" if c.get("score") is None else str(c["score"]), f"${running:,.0f}"])
        table = _mini_table([("Stock", "left"), ("Group", "left"), ("Expiry", "left"), ("Strike", "right"),
                             ("Collateral", "right"), ("Premium", "right"), ("Score", "right"),
                             ("Running total", "right")], rows)
        yield_pct = basket["income"] / basket["total"] * 100 if basket["total"] else 0
        summary = (f'<div style="margin-top:8px;font-size:12.5px;color:{INK}"><b>Total collateral ${basket["total"]:,.0f}</b> '
                   f'({basket["total"] / basket["limit"] * 100:.0f}% of the limit) · premium ${basket["income"]:,.0f} '
                   f'({yield_pct:.1f}% of collateral, before time) · ${basket["left"]:,.0f} left</div>')
        body = _sub_title("Example basket within the budget") + table.replace(
            "margin:16px 0 6px", "margin:0 0 6px") + summary
    else:
        body = f'<div style="font-size:12.5px;color:{MUTED}">No candidate fits the available budget.</div>'
    if over:
        names = ", ".join(sorted({f"{c['symbol']} (${c['collateral']:,.0f})" for c in over}, key=str)[:8])
        body += (f'<div style="margin-top:8px;font-size:12px;color:#8a5a00">{len(over)} listed contract(s) exceed the available '
                 f'budget on their own: {esc(names)}. You said you can flex the limit; they are flagged, not removed.</div>')
    # concentration by group
    by_group: dict[str, list[str]] = {}
    for c in result.candidates:
        by_group.setdefault(c.get("group") or "Other", [])
        if c["symbol"] not in by_group[c.get("group") or "Other"]:
            by_group[c.get("group") or "Other"].append(c["symbol"])
    if by_group:
        chips = ""
        for grp, syms in sorted(by_group.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            chips += (f'<div style="margin:0 0 6px;font-size:12px;color:{INK}"><b>{esc(grp)}</b> '
                      f'<span style="color:{MUTED}">· {len(syms)} stock(s)</span><br>'
                      + "".join(_chip(x, "#eef2f7", "#44566c") for x in syms) + "</div>")
        body += _sub_title("Concentration: stocks in today's list by group") + chips
    return _card("Budget and concentration", tiles + body,
                 f"Naked-put assignment exposure = strike × 100 per contract. The basket is picked by score: one contract "
                 f"per stock, at most {basket['max_per_group']} per group, never above the available budget. It is an "
                 "illustration, not advice; correlated groups can fall together.")


def _used_sub(basket: dict) -> str:
    if basket.get("from_positions"):
        extra = f" + ${basket['manual']:,.0f} manual" if basket.get("manual") else ""
        return f"open puts in positions.yaml{extra}"
    return "set in config: exposure.used_usd"


_TAG_COLORS = {"red": ("#fde8e8", "#a12626"), "amber": ("#fff3d6", "#8a5a00"),
               "green": ("#e3f4ea", "#146c3a"), "grey": ("#eef2f7", "#44566c")}


def _usd(v, signed=False) -> str:
    if v is None:
        return "-"
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{'-' if v < 0 else ''}${abs(v):,.0f}"


def _pnl_html(v, pct=None) -> str:
    if v is None:
        return "-"
    color = "#146c3a" if v >= 0 else "#a12626"
    extra = f" ({pct:+.1f}%)" if pct is not None else ""
    return f'<span style="color:{color};font-weight:700">{_usd(v, True)}{extra}</span>'


def _wheel_card(result: ScanResult, cfg: dict, today: dt.date) -> str:
    w = result.wheel
    if not w:
        return ""
    esc = html.escape
    ws = wheel_settings(cfg)
    body = ""
    if w["problems"]:
        body += ('<div style="background:#fdf0f0;border:1px solid #f1c6c6;border-radius:8px;padding:10px 12px;margin:0 0 10px;'
                 'font-size:12px;color:#7a2020"><b>positions.yaml problems (these rows were skipped):</b><ul style="margin:6px 0 0;'
                 'padding-left:18px">' + "".join(f"<li>{esc(x)}</li>" for x in w["problems"]) + "</ul></div>")
    if w["errors"]:
        body += (f'<div style="margin:0 0 8px;font-size:12px;color:#8a5a00">Price lookup problems: '
                 + esc("; ".join(f"{s}: {m}" for s, m in w["errors"])) + "</div>")
    puts = [r for r in w["options"] if r["kind"] == "puts"]
    calls = [r for r in w["options"] if r["kind"] == "calls"]

    def opt_table(rows, title):
        out = []
        for r in sorted(rows, key=lambda x: (x["expired"], x["expiry"], x["symbol"])):
            tags = "".join(_chip(t, *_TAG_COLORS[c]) + " " for t, c in r["tags"])
            if r["error"]:
                tags += _chip(r["error"], *_TAG_COLORS["amber"])
            dist = ""
            if r["spot"] is not None:
                d = (r["spot"] - r["strike"]) / r["spot"] * 100 if r["side"] == "put" else (r["strike"] - r["spot"]) / r["spot"] * 100
                dist = f"{d:+.1f}% to strike"
            out.append([
                f"<b>{esc(r['symbol'])}</b>" + (f'<div style="color:{MUTED};font-size:11px">{esc(r["note"])}</div>' if r["note"] else ""),
                f"{r['expiry']:%d %b %y}" + f'<div style="color:{MUTED};font-size:11px">{r["dte"]}d</div>',
                f"{r['strike']:,.2f}" + f'<div style="color:{MUTED};font-size:11px">x{r["contracts"]}</div>',
                f"{r['spot']:,.2f}" + f'<div style="color:{MUTED};font-size:11px">{dist}</div>' if r["spot"] is not None else "-",
                f"{r['premium']:.2f}",
                "-" if r["close_cost"] is None else f"{r['close_cost']:.2f}" + (
                    f'<div style="color:{MUTED};font-size:11px">delta {r["delta"]:.2f}</div>' if r["delta"] is not None else ""),
                "-" if r["captured_pct"] is None else f"{r['captured_pct']:.0f}%",
                _pnl_html(r["pnl_usd"]),
                tags or _chip("OK", *_TAG_COLORS["grey"]),
            ])
        return (_sub_title(title) + _mini_table([("Stock", "left"), ("Expiry", "left"), ("Strike", "right"), ("Spot", "right"),
                                                 ("Sold at", "right"), ("Buy back", "right"), ("Captured", "right"),
                                                 ("P/L now", "right"), ("Status", "left")], out))

    live_puts = [r for r in puts if not r["expired"]]
    if puts or calls:
        tiles = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 6px"><tr>'
                 + _kpi("Open puts", f"{len(live_puts)}", f"${w['exposure']:,.0f} assignment exposure", "0 3px 0 0")
                 + _kpi("Premium received", f"${w['open_put_premium']:,.0f}", "open puts, before buy-back", "0 3px")
                 + _kpi("Open P/L", _pnl_html(w["unrealized"]), "premium minus buy-back cost", "0 0 0 3px")
                 + "</tr></table>")
        body += tiles
    if puts:
        body += opt_table(puts, "Open short puts")
    if calls:
        body += opt_table(calls, "Open covered calls")
    if w["shares"]:
        out = []
        for sh in w["shares"]:
            tag = _chip("below net cost", *_TAG_COLORS["amber"]) if sh["below_net"] else ""
            out.append([f"<b>{esc(sh['symbol'])}</b>", f"{sh['shares']:,.0f}", f"{sh['cost']:,.2f}",
                        f"{sh['premium_collected']:.2f}", f"{sh['net_cost']:,.2f}",
                        "-" if sh["spot"] is None else f"{sh['spot']:,.2f}",
                        _pnl_html(sh["pnl_usd"]), _pnl_html(sh["pnl_with_prem_usd"], sh["pnl_with_prem_pct"]), tag])
        body += _sub_title("Shares from assignment") + _mini_table(
            [("Stock", "left"), ("Shares", "right"), ("Cost", "right"), ("Premium", "right"), ("Net cost", "right"),
             ("Spot", "right"), ("Stock P/L", "right"), ("P/L with premium", "right"), ("", "left")], out)
    if not (puts or calls or w["shares"]) and not w["problems"]:
        body += f'<div style="font-size:12.5px;color:{MUTED}">positions.yaml has no positions yet.</div>'
    return _card("Wheel tracker", body,
                 f"From positions.yaml (you maintain it). Buy back = current ask. Captured = share of the premium you could "
                 f"keep by closing now; flagged from {ws['profit_take_pct']:.0f}%. Net cost = cost minus premium collected.")


def _cc_card(result: ScanResult, cfg: dict) -> str:
    w = result.wheel
    if not w or not w.get("cc"):
        return ""
    esc = html.escape
    cc = cc_settings(cfg)
    body = ""
    for it in w["cc"]:
        spot = "-" if it["spot"] is None else f"{it['spot']:,.2f}"
        head = (f"{it['symbol']} · {it['shares']:,.0f} shares · net cost {it['net_cost']:,.2f} · spot {spot}"
                + (f" · {it['avail']} contract(s) free" if it["avail"] >= 1 else ""))
        body += _sub_title(head)
        if it["error"]:
            body += f'<div style="font-size:12.5px;color:{MUTED}">{esc(it["error"])}</div>'
            continue
        c = it["cands"]
        if not c["good"] and not c["loss"]:
            body += f'<div style="font-size:12.5px;color:{MUTED}">No call passed the filters.</div>'
            continue

        def table(items, tint=False):
            out = []
            for r in items:
                tags = ""
                if r["loss_if_called"]:
                    tags += _chip("locks in a loss if called", *_TAG_COLORS["red"]) + " "
                if r["spans_earnings"]:
                    tags += _chip("⚠ spans earnings", *_TAG_COLORS["amber"]) + " "
                total = r["bid"] * 100 * it["avail"]
                out.append([f"{r['expiry']:%d %b %y}" + f'<div style="color:{MUTED};font-size:11px">{r["dte"]}d</div>',
                            f"<b>{r['strike']:,.2f}</b>" + f'<div style="color:{MUTED};font-size:11px">OTM {r["otm_pct"]:.1f}%</div>',
                            f"{r['bid']:.2f} / {r['ask']:.2f}" + f'<div style="color:{MUTED};font-size:11px">spread {r["spread_pct"]:.1f}%</div>',
                            f"{r['delta']:.2f}" + f'<div style="color:{MUTED};font-size:11px">OI {r["oi"]:,.0f}</div>',
                            f"${total:,.0f}" + f'<div style="color:{MUTED};font-size:11px">{r["static_pct"]:.2f}% of net cost</div>',
                            f'<b style="color:#146c3a">{r["annualized_pct"]:.1f}%</b>',
                            f"{r['called_pct']:+.1f}%" + f'<div style="color:{MUTED};font-size:11px">{r["called_annual_pct"]:+.0f}% annual</div>',
                            tags])
            return _mini_table([("Expiry", "left"), ("Strike", "right"), ("Bid / Ask", "right"), ("Delta", "right"),
                                (f"Premium x{it['avail']}", "right"), ("Annual", "right"), ("If called", "right"),
                                ("Flags", "left")], out)

        if c["good"]:
            body += table(c["good"]).replace("margin:16px 0 6px", "margin:0 0 6px")
            if c["good_total"] > len(c["good"]):
                body += f'<div style="margin-top:4px;font-size:11.5px;color:{MUTED}">{c["good_total"] - len(c["good"])} more passed; top {len(c["good"])} shown.</div>'
            if c["loss_total"]:
                body += (f'<div style="margin-top:4px;font-size:11.5px;color:{MUTED}">{c["loss_total"]} more strike(s) would lock in a loss '
                         "(strike + premium below your net cost) and are hidden.</div>")
        else:
            body += (f'<div style="font-size:12px;color:#a12626;margin-bottom:6px">Every call that passed the filters has a strike '
                     "below your net cost. Selling one means accepting a loss if the shares are called away. Shown for reference:</div>"
                     + table(c["loss"]))
    return _card("Covered call candidates", body,
                 f"For shares in positions.yaml with a free 100-share block. DTE {cc['dte_min']}-{cc['dte_max']}, delta "
                 f"{cc['delta_min']:.2f}-{cc['delta_max']:.2f}, OI ≥ {cc['min_oi']:.0f}, bid ≥ {cc['min_bid']:.2f}, spread < "
                 f"{cc['max_spread_pct']:.0f}%. Annual = premium / net cost × 365 / DTE. If called = return if the shares are "
                 "sold at the strike, premium included. Candidates only; not advice.")


def _near_table(shown: list[dict]) -> str:
    esc = html.escape
    widths = (11, 12, 11, 9, 9, 9, 10, 29)   # column widths (%), same in all near-miss tables
    head = "".join(_th(lbl, al).replace("<th ", f'<th width="{w}%" ', 1) for (lbl, al), w in zip(
        [("Stock", "left"), ("Expiry", "left"), ("Strike", "right"), ("Bid", "right"), ("Delta", "right"),
         ("OTM", "right"), ("Annual", "right"), ("Missed on", "left")], widths))
    rows = []
    for i, c in enumerate(shown):
        bg = "#ffffff" if i % 2 == 0 else "#fafbfd"
        cells = [
            _cell(f"<b>{esc(c['symbol'])}</b>", "", "left"),
            _cell(f"{c['expiry']:%d %b %y}", f"{c['dte']} days", "left"),
            _cell(f"{c['strike']:,.2f}"), _cell(f"{c['bid']:.2f}"), _cell(f"{c['delta']:.2f}"),
            _cell(f"{c['otm_pct']:.1f}%"), _cell(f"<b>{c['annualized_pct']:.1f}%</b>"),
            _cell(f'<span style="color:#8a5a00">{esc(c["miss_detail"])}</span>', "", "left", wrap=True),
        ]
        rows.append(f'<tr style="background:{bg}">' + "".join(cells) + "</tr>")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="table-layout:fixed;border:1px solid {LINE};border-radius:8px;border-collapse:separate;border-spacing:0">'
            f'<tr>{head}</tr>{"".join(rows)}</table>')


def _funnel(result: "ScanResult") -> str:
    esc = html.escape
    total = max(result.funnel.get("contracts_total", 0), 1)
    rows = []
    for key, label in FUNNEL_LABELS:
        n = result.funnel.get(key, 0)
        if key == "contracts_total":
            color = "#9fb3c8"
        elif key == "passed":
            color = "#2e9e63"
        elif key.startswith("warn_"):
            color = "#e0a100"
        else:
            color = "#c3cfdb"
        pct = max(n / total * 100, 1.0) if n else 0
        bar = f'<div style="height:8px;border-radius:4px;background:{color};width:{pct:.1f}%"></div>' if n else ""
        strong = key in ("passed", "contracts_total")
        tint = INK if (n or strong) else "#9aa5b1"
        weight = "700" if strong else "400"
        rows.append(
            f'<tr><td style="padding:5px 0;font-size:12px;color:{tint};font-weight:{weight}">{esc(label)}</td>'
            f'<td align="right" style="padding:5px 12px;font-size:12px;color:{tint};font-weight:{weight}">{n:,}</td>'
            f'<td width="40%" style="padding:5px 0"><div style="height:8px;border-radius:4px;background:#f1f4f8">{bar}</div></td></tr>')
    out = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'
    detail = missing_breakdown(result)
    if detail:
        out += (f'<div style="margin-top:8px;font-size:11.5px;color:{MUTED}">Missing-data breakdown '
                f'(one contract can miss several fields): {esc(detail)}</div>')
    return out


def build_html(result: ScanResult, cfg: dict, today: dt.date) -> str:
    esc = html.escape
    min_hist = cfg["iv"]["min_history_days"]
    passed = result.funnel.get("passed", 0)
    evaluated = result.symbols_scanned - len(result.underlying_rejects)
    warn_rows = sum(1 for c in result.candidates if c.get("earnings_note"))

    # ---- header band + summary tiles
    header = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{NAVY};'
        f'border-radius:10px;margin:0 0 12px"><tr><td style="padding:22px 24px">'
        f'<div style="font-size:11px;letter-spacing:1.6px;color:#8fb3d9;font-weight:700">CASH SECURED PUT SCAN</div>'
        f'<div style="font-size:26px;font-weight:700;color:#ffffff;margin-top:4px;line-height:30px">CSP Scanner</div>'
        f'<div style="font-size:13px;color:#c5d5e6;margin-top:4px">{tr_date(today)} · {result.api_calls} API calls</div>'
        f'</td><td align="right" valign="middle" style="padding:22px 24px">'
        f'<div style="font-size:38px;font-weight:700;color:#ffffff;line-height:40px">{len(result.candidates)}</div>'
        f'<div style="font-size:11px;letter-spacing:1.2px;color:#8fb3d9;font-weight:700">CANDIDATES</div></td></tr></table>')
    kpis = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px"><tr>'
        + _kpi("Stocks scanned", f"{result.symbols_scanned}<span style='font-size:14px;color:{MUTED}'>/{result.symbols_total}</span>", "", "0 3px 0 0")
        + _kpi("Evaluated", f"{evaluated}", "passed stock filters", "0 3px")
        + _kpi("Contracts checked", f"{result.funnel.get('contracts_total', 0):,}", "puts, all expiries and deltas", "0 3px")
        + _kpi("Passed filters", f"{passed:,}",
               f"top {len(result.candidates)} shown" if passed > len(result.candidates) else "contracts",
               "0 0 0 3px")
        + "</tr></table>")

    # ---- candidates
    if result.candidates:
        head = "".join([_th("Stock", "left"), _th("Score", "center"), _th("Expiry", "left"), _th("Strike"),
                        _th("Bid / Ask"), _th("Delta / IV"), _th("OI / Vol"), _th("Annual"), _th("Collateral"),
                        _th("Earnings", "left"), _th("IVR / IVP")])
        ex = exposure_settings(cfg)
        rows = "".join(_candidate_row(c, i, min_hist, today, ex) for i, c in enumerate(result.candidates))
        body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="border:1px solid {LINE};border-radius:8px;border-collapse:separate;border-spacing:0">'
                f'<tr>{head}</tr>{rows}</table>')
        legend = []
        if any(c.get("score") is not None for c in result.candidates):
            legend.append(score_legend_html(cfg))
        if warn_rows:
            legend.append(f"{_chip('⚠ expiry spans earnings', '#fff3d6', '#8a5a00')} the option expires after the earnings date, "
                          "so the position is held through the report.")
            legend.append(f"{_chip('⚠ date unknown', '#fff3d6', '#8a5a00')} MarketData did not return the next earnings date; "
                          "verify it yourself.")
        if result.earnings_unknown:
            chips = "".join(
                _chip(s + (f" · last {result.last_earnings[s]:%d %b %y}" if s in result.last_earnings else ""),
                      "#fff3d6", "#8a5a00") for s in sorted(result.earnings_unknown, key=str))
            legend.append(f"<b>Stocks with no earnings date found:</b> {chips}")
            if result.last_earnings:
                legend.append("A stock that reported recently usually reports again in ~3 months; "
                              "still verify it yourself.")
        if any(c.get("held") for c in result.candidates):
            legend.append(f"{_chip('already hold', '#fde8e8', '#a12626')} you already hold shares or an open put on this stock "
                          "(positions.yaml); a new put adds to the same risk.")
        if any(c.get("is_etf") for c in result.candidates):
            legend.append(f"{_chip('ETF', '#e8eefc', '#2c4a9a')} exempt from the earnings rule; no earnings component in the score.")
        if legend:
            body += (f'<div style="margin-top:10px;font-size:11.5px;line-height:20px;color:{MUTED}">'
                     + "<br>".join(legend) + "</div>")
        order = ("Sorted by annualized return." if (cfg.get("output", {}) or {}).get("sort_by", "score") == "annualized"
                 else "Sorted by score.")
        cards = _card("Candidates", body, order + " Annualized = bid / strike × 365 / DTE · "
                                              "Collateral = strike × 100 · Breakeven = strike − bid")
    else:
        cards = _card("Candidates",
                      f'<div style="background:#f6f8fb;border:1px dashed #c3cfdb;border-radius:8px;padding:16px;'
                      f'font-size:13px;color:{INK}"><b>No candidate passed all filters today.</b> '
                      f'The near misses and filter summary below show why.</div>')

    cards = _wheel_card(result, cfg, today) + _cc_card(result, cfg) + _changes_card(result, cfg, today) + cards + _budget_card(result, cfg)

    # ---- near misses
    groups = group_near_misses(result, cfg)
    if groups:
        near = ""
        for _, label, total, shown in groups:
            extra = "" if total == len(shown) else f" · top {len(shown)}"
            near += (f'<div style="margin:16px 0 6px;font-size:12.5px;font-weight:700;color:{INK}">{esc(label)} '
                     f'{_chip(f"{total} total{extra}", "#eef2f7", "#44566c")}</div>{_near_table(shown)}')
    else:
        near = f'<div style="font-size:12.5px;color:{MUTED}">None.</div>'
    cards += _card("Near misses", near.replace('margin:16px 0 6px', 'margin:0 0 6px', 1) if groups else near,
                   "Contracts that failed <b>exactly one</b> filter. Shows which contracts would qualify "
                   "if you relaxed that rule.")

    # ---- filter summary + stock-level filters
    cards += _card("Filter summary", _funnel(result), "Contract level: each contract is counted under the first filter it fails.")
    grouped = underlying_reject_groups(result)
    if grouped:
        blocks = ""
        for label, items in grouped.items():
            chips = "".join(_chip(i, "#eef2f7", "#44566c") for i in items)
            blocks += (f'<div style="margin:0 0 8px"><div style="font-size:12px;font-weight:700;color:{INK};'
                       f'margin-bottom:4px">{esc(label)} <span style="color:{MUTED};font-weight:400">· {len(items)}</span></div>'
                       f'{chips}</div>')
        cards += _card("Rejected at stock level", blocks, "The contracts of these stocks were not evaluated.")

    # ---- errors
    if result.errors:
        errs = "".join(f"<li>{esc(str(s))}: {esc(m)}</li>" for s, m in result.errors)
        cards += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fdf0f0;'
                  f'border:1px solid #f1c6c6;border-radius:10px;margin:0 0 14px"><tr><td style="padding:14px 18px">'
                  f'<div style="font-size:14px;font-weight:700;color:#a12626">Errors</div>'
                  f'<ul style="margin:8px 0 0;padding-left:18px;font-size:12px;color:#7a2020">{errs}</ul></td></tr></table>')

    # ---- notes
    icfg = cfg["iv"]
    notes = [
        "Data: MarketData cached mode (can be up to ~20 min old). Check the live price at your broker before placing an order.",
        earnings_note_text(cfg),
        score_note_text(cfg),
        "HV = 20-day historical volatility from daily closes (annualized). IV/HV above 1 means the option is priced above "
        "the stock's recent real movement; shown only for stocks in the list.",
        f"IVR/IVP: computed from our own accumulated IV history (needs at least {icfg['min_history_days']} days). "
        "While it says 'building', the IV filter is not applied yet.",
        "Near miss = a contract that failed exactly one filter. Stocks rejected at stock level are not checked for earnings/IV.",
        "Market-cap filter ($5B) is not applied; the universe is your own watchlist.",
    ]
    note_html = "".join(f"<li style='margin-bottom:3px'>{esc(n)}</li>" for n in notes)
    footer = (f'<div style="padding:4px 6px 0;font-size:11.5px;line-height:17px;color:{MUTED}">'
              f'<div style="font-weight:700;margin-bottom:4px">Notes</div>'
              f'<ul style="margin:0 0 12px;padding-left:18px">{note_html}</ul>'
              f'<div style="border-top:1px solid {LINE};padding-top:10px">CSP Scanner · MarketData data · '
              f'This list is research output, not investment advice.</div></div>')

    inner = header + kpis + cards + footer
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
            f'<title>CSP Scanner {today:%d %b %Y}</title></head>'
            f'<body style="margin:0;padding:0;background:{PAGE}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}">'
            f'<tr><td align="center" style="padding:20px 12px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:900px;font-family:{FONT};color:{INK}"><tr><td>{inner}</td></tr></table>'
            f'</td></tr></table></body></html>')


def write_outputs(result: ScanResult, cfg: dict, today: dt.date, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.html").write_text(build_html(result, cfg, today), encoding="utf-8")
    (outdir / "report.txt").write_text(build_text(result, cfg, today), encoding="utf-8")
    cols = ["symbol", "score", "score_return", "score_safety", "score_liquidity", "score_earnings", "score_iv",
            "option_symbol", "spot", "expiry", "dte", "strike", "bid", "ask", "delta", "iv",
            "open_interest", "volume", "spread_pct", "otm_pct", "annualized_pct", "breakeven",
            "collateral", "earnings_date", "last_earnings", "is_etf", "earnings_note", "iv_status", "iv_rank", "iv_pct",
            "hv", "iv_hv", "group", "streak", "is_new", "score_cap"]
    with open(outdir / "candidates.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for c in result.candidates:
            writer.writerow({**c, **{"score_" + k: v for k, v in (c.get("score_parts") or {}).items()}})
    with open(outdir / "near_misses.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols + ["miss_code", "miss_detail"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.near_misses)


# --------------------------------------------------------------------------- ana akış
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily CSP scanner")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--symbols", default=os.environ.get("SYMBOLS_OVERRIDE", ""),
                        help="Comma-separated symbols (for testing)")
    parser.add_argument("--no-mail", action="store_true", help="Do not send mail; write to out/ only")
    parser.add_argument("--force", action="store_true", help="Run even if the market is closed")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    token = os.environ.get("MARKETDATA_TOKEN", "").strip()
    if not token:
        print("ERROR: MARKETDATA_TOKEN is not set (GitHub Secrets).", file=sys.stderr)
        return 2

    today = dt.datetime.now(NY).date()
    override: list[str] = []
    try:
        override = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        symbols = override or normalise_universe(cfg["universe"])
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    md = MarketData(token, max_calls=cfg["safety"]["max_api_calls"],
                    mode=cfg["marketdata"]["mode"], maxage=cfg["marketdata"]["maxage"])

    try:
        if cfg["safety"]["skip_when_market_closed"] and not args.force:
            if md.market_is_open(today) is False:
                print(f"{today}: market closed, scan skipped.")
                return 0
        history = IVHistory(ROOT / "data" / "iv_history.csv")
        positions = load_positions(ROOT / "positions.yaml", today)
        cfg["_positions_used"] = positions_exposure(positions)
        result = run_scan(cfg, md, history, today, symbols)
        try:
            result.wheel = run_wheel(cfg, md, positions, today)
        except MarketDataError as exc:                      # takip kartı taramayı asla düşürmesin
            if isinstance(exc, MarketDataAuthError):
                raise
            result.errors.append(("positions", str(exc)))
        mark_held(result.listed, result.wheel)
    except MarketDataAuthError as exc:
        print(f"ERROR: MarketData authentication error: {exc}", file=sys.stderr)
        return 2

    history.save()
    failed_all = result.symbols_scanned == 0
    clog = CandidateLog(ROOT / "data" / "candidates_history.csv")
    result.changes = analyze_changes(result, clog, today)
    if not failed_all and not override:        # test çalıştırmaları (--symbols) geçmişi bozmasın
        clog.record(today, result.listed)
        clog.save()
    write_outputs(result, cfg, today, ROOT / "out")
    print(build_text(result, cfg, today))

    if not args.no_mail:
        user = os.environ.get("GMAIL_USER", "")
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        to = os.environ.get("MAIL_TO", "") or user
        if not (user and password):
            print("ERROR: GMAIL_USER / GMAIL_APP_PASSWORD is not set.", file=sys.stderr)
            return 2
        subject = (f"CSP Scanner {today:%d %b %Y}: SCAN FAILED" if failed_all
                   else f"CSP Scanner {today:%d %b %Y}: {len(result.candidates)} candidates")
        send_mail(subject, build_text(result, cfg, today), build_html(result, cfg, today), user, password, to)
        print(f"Mail sent: {to}")
    return 1 if failed_all else 0


if __name__ == "__main__":
    sys.exit(main())
