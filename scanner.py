#!/usr/bin/env python3
"""Günlük Cash Secured Put (CSP) tarayıcısı.

Kullanım:
    python scanner.py                      # tara + mail at
    python scanner.py --no-mail            # sadece tara, out/ klasörüne yaz
    python scanner.py --symbols AAPL,MSFT  # sadece bu hisseleri tara (test)

Gerekli ortam değişkenleri:
    MARKETDATA_TOKEN, GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
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
    "no_quote": "Fiyat verisi alınamadı",
    "price_range": "Fiyat aralığı dışında",
    "underlying_volume": "Hisse hacmi düşük",
    "daily_change": "Günlük değişim sınır dışında",
}

# Hisse filtresi kodu -> "yakın kaçan" kodu
UNDERLYING_NEAR_CODE = {
    "price_range": "underlying_price_range",
    "underlying_volume": "underlying_volume",
    "daily_change": "underlying_daily_change",
}

FUNNEL_LABELS = [
    ("contracts_total", "Taranan put kontratı (hisse filtrelerini geçen hisselerde)"),
    ("rej_missing", "Eksik / geçersiz veri"),
    ("rej_dte", "DTE aralığı dışında"),
    ("rej_delta", "Delta aralığı dışında"),
    ("rej_oi", "Open interest düşük"),
    ("rej_volume", "Opsiyon hacmi düşük"),
    ("rej_bid", "Bid yetersiz"),
    ("rej_spread", "Bid-ask spread geniş"),
    ("rej_return", "Yıllık getiri düşük"),
    ("rej_earnings", "Vade, earnings tarihinden önce değil"),
    ("rej_earnings_unknown", "Earnings tarihi bilinmiyor"),
    ("rej_iv", "IV rank / percentile yetersiz"),
    ("passed", "TÜM FİLTRELERİ GEÇEN"),
]

# Yakın kaçan gruplarının başlıkları (gösterim sırası bu listedir)
NEAR_LABELS = [
    ("earnings", "Sadece earnings kuralına takılanlar (vade earnings tarihinden önce değil)"),
    ("earnings_unknown", "Sadece earnings tarihi bulunamadığı için elenenler"),
    ("iv", "Sadece IV rank / percentile'a takılanlar"),
    ("oi", "Sadece open interest düşük olanlar"),
    ("volume", "Sadece opsiyon hacmi düşük olanlar"),
    ("bid", "Sadece bid yetersiz olanlar"),
    ("spread", "Sadece bid-ask spread geniş olanlar"),
    ("return", "Sadece yıllık getirisi düşük olanlar"),
    ("delta", "Sadece delta bandının hemen dışında kalanlar"),
    ("underlying_price_range", "Sadece hisse fiyat aralığı yüzünden elenenler"),
    ("underlying_daily_change", "Sadece hisse günlük değişimi yüzünden elenenler"),
    ("underlying_volume", "Sadece hisse hacmi yüzünden elenenler"),
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
                f"config.yaml 'universe' listesinde sembol olmayan bir giriş var ({item!r}). "
                "ON, NO, Y, N, OFF gibi semboller tırnak içinde yazılmalı, örneğin: - \"ON\""
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
    if not (ucfg["price_min"] <= last <= ucfg["price_max"]):
        return info, "price_range"
    if volume is None or not volume > ucfg["volume_gt"]:
        return info, "underlying_volume"
    change_pct = info["change_pct"]
    if change_pct is None or not (ucfg["daily_change_pct_min"] <= change_pct <= ucfg["daily_change_pct_max"]):
        return info, "daily_change"
    return info, None


def underlying_detail(reason: str, info: dict) -> str:
    if reason == "price_range" and info.get("last") is not None:
        return f"fiyat ${info['last']:,.2f}"
    if reason == "underlying_volume":
        vol = info.get("volume")
        return "hacim yok" if vol is None else f"hacim {vol / 1e6:.2f}M"
    if reason == "daily_change":
        chg = info.get("change_pct")
        return "değişim verisi yok" if chg is None else f"günlük {chg:+.1f}%"
    return UNDERLYING_LABELS.get(reason, reason)


def evaluate_contracts(rows: list[dict], symbol: str, spot: float, today: dt.date,
                       cfg: dict, funnel: Counter, near: list | None = None) -> list[dict]:
    """Kontratları filtreler. Tüm filtreleri geçenleri döndürür.

    `near` verilirse, yalnızca TEK bir kontrat filtresine takılan kontratlar (delta için
    bandın hemen dışındakiler) oraya 'yakın kaçan' olarak eklenir.
    """
    ocfg, rcfg = cfg["option"], cfg["return"]
    delta_tol = cfg.get("output", {}).get("near_miss_delta_tolerance", 0.05)
    out = []
    for row in rows:
        funnel["contracts_total"] += 1
        exp = to_date(row.get("expiration"), NY)
        strike, delta = num(row.get("strike")), num(row.get("delta"))
        bid, ask = num(row.get("bid")), num(row.get("ask"))
        if exp is None or strike is None or strike <= 0 or delta is None or bid is None or ask is None or ask <= 0:
            funnel["rej_missing"] += 1
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
            fails["delta"] = f"delta {delta:.2f} (bant {ocfg['delta_min']}…{ocfg['delta_max']})"
        if oi is None or oi < ocfg["open_interest_min"]:
            fails["oi"] = "OI yok" if oi is None else f"OI {oi:,.0f} < {ocfg['open_interest_min']}"
        if volume is None or volume < ocfg["option_volume_min"]:
            fails["volume"] = "hacim yok" if volume is None else f"hacim {volume:,.0f} < {ocfg['option_volume_min']}"
        if not bid > ocfg["bid_gt"]:
            fails["bid"] = f"bid {bid:.2f} ≤ {ocfg['bid_gt']}"
        if not spread_pct < ocfg["spread_pct_lt"]:
            fails["spread"] = f"spread %{spread_pct:.1f} ≥ %{ocfg['spread_pct_lt']}"
        if not annualized > rcfg["annualized_gt_pct"]:
            fails["return"] = f"yıllık %{annualized:.1f} ≤ %{rcfg['annualized_gt_pct']}"

        cand = {
            "symbol": symbol, "option_symbol": row.get("optionSymbol", ""), "spot": spot,
            "expiry": exp, "dte": dte, "strike": strike, "bid": bid, "ask": ask,
            "delta": delta, "iv": num(row.get("iv")), "open_interest": oi, "volume": volume,
            "spread_pct": spread_pct, "otm_pct": (spot - strike) / spot * 100,
            "premium": premium, "static_return_pct": static_return * 100,
            "annualized_pct": annualized, "breakeven": strike - premium,
            "collateral": strike * 100, "earnings_date": None,
            "iv_status": None, "iv_rank": None, "iv_pct": None, "iv_n": None,
            "miss_code": None, "miss_detail": "",
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
    errors: list[tuple[str, str]] = field(default_factory=list)
    symbols_total: int = 0
    symbols_scanned: int = 0
    api_calls: int = 0
    iv_building: bool = False


def run_scan(cfg: dict, md, history: IVHistory, today: dt.date, symbols: list[str]) -> ScanResult:
    result = ScanResult(symbols_total=len(symbols))
    ocfg, icfg, ecfg = cfg["option"], cfg["iv"], cfg["earnings"]
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
        earnings_failed = False
        try:
            dates = md.earnings_dates(symbol, today, today + dt.timedelta(days=ecfg["lookahead_days"]))
            upcoming = [d for d in dates if d >= today]
            next_earnings = min(upcoming) if upcoming else None
        except MarketDataAuthError:
            raise
        except MarketDataError as exc:
            earnings_failed = True
            result.errors.append((symbol, f"earnings alınamadı: {exc}"))
        if next_earnings is None:
            result.earnings_unknown.append(symbol)

        def earnings_status(cand):
            if next_earnings is None:
                return "earnings_unknown" if (ecfg["require_known_date"] or earnings_failed) else None
            return "earnings" if cand["expiry"] >= next_earnings else None

        def earnings_detail(cand, status):
            if status == "earnings":
                return f"vade {cand['expiry']:%d.%m.%y} ≥ earnings {next_earnings:%d.%m.%y}"
            return "earnings tarihi bulunamadı"

        kept = []
        for cand in candidates:
            status = earnings_status(cand)
            if status:
                result.funnel["rej_" + status] += 1
                cand["miss_code"], cand["miss_detail"] = status, earnings_detail(cand, status)
                result.near_misses.append(cand)
                continue
            cand["earnings_date"] = next_earnings
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
                    detail = f"IV geçmişi yetersiz ({metrics.get('n')} gün)"
                cand["miss_code"], cand["miss_detail"] = "iv", detail
                result.near_misses.append(cand)
                continue
            all_candidates.append(cand)

        # Tek kontrat filtresine takılanlar: earnings ve IV'yi de geçiyorlarsa gerçek "yakın kaçan"
        for cand in near_contracts:
            if earnings_status(cand) or not iv_passes(metrics, icfg):
                continue
            cand["earnings_date"] = next_earnings
            apply_iv(cand, metrics)
            result.near_misses.append(cand)

    # Sırala: yıllık getiri; hisse başına sınır; ilk N
    all_candidates.sort(key=lambda c: (-c["annualized_pct"], c["spread_pct"]))
    per_symbol: Counter = Counter()
    selected = []
    for cand in all_candidates:
        if per_symbol[cand["symbol"]] >= cfg["output"]["max_per_symbol"]:
            continue
        per_symbol[cand["symbol"]] += 1
        selected.append(cand)
    result.funnel["passed"] = len(all_candidates)
    result.candidates = selected[: cfg["output"]["top_n"]]
    result.api_calls = getattr(md, "calls", 0)
    return result


def group_near_misses(result: ScanResult, cfg: dict) -> list[tuple[str, str, int, list[dict]]]:
    """[(kod, başlık, toplam, gösterilecek kontratlar)] döndürür."""
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
        return f"birikiyor ({c['iv_n']})"
    return "yok"


def underlying_reject_groups(result: ScanResult) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for symbol, reason in sorted(result.underlying_rejects.items(), key=lambda kv: str(kv[0])):
        detail = underlying_detail(reason, result.underlying_info.get(symbol, {}))
        grouped.setdefault(UNDERLYING_LABELS.get(reason, reason), []).append(f"{symbol} ({detail})")
    return grouped


def build_text(result: ScanResult, cfg: dict, today: dt.date) -> str:
    lines = [f"CSP Scanner - {today:%d.%m.%Y}",
             f"{len(result.candidates)} aday | {result.symbols_scanned}/{result.symbols_total} hisse tarandı | "
             f"{result.api_calls} API çağrısı", ""]
    for c in result.candidates:
        earnings = f"{c['earnings_date']:%d.%m.%y}" if c["earnings_date"] else "-"
        lines.append(
            f"{c['symbol']:<6} {c['expiry']:%d.%m.%y} ({c['dte']}g) strike {c['strike']:.2f} "
            f"bid {c['bid']:.2f} delta {c['delta']:.2f} yıllık %{c['annualized_pct']:.1f} "
            f"OTM %{c['otm_pct']:.1f} teminat ${c['collateral']:,.0f} "
            f"IVR/IVP {iv_text(c)} earnings {earnings}")
    lines += ["", "Filtre özeti:"]
    for key, label in FUNNEL_LABELS:
        lines.append(f"  {label}: {result.funnel.get(key, 0)}")

    groups = group_near_misses(result, cfg)
    lines += ["", "Yakın kaçanlar (yalnızca tek bir filtreye takılanlar):"]
    if not groups:
        lines.append("  yok")
    for _, label, total, shown in groups:
        lines.append(f"  {label} - toplam {total} kontrat")
        for c in shown:
            lines.append(f"    {c['symbol']:<6} {c['expiry']:%d.%m.%y} ({c['dte']}g) strike {c['strike']:.2f} "
                         f"bid {c['bid']:.2f} delta {c['delta']:.2f} yıllık %{c['annualized_pct']:.1f} "
                         f"-> {c['miss_detail']}")
    if result.earnings_unknown:
        lines += ["", "Earnings tarihi bulunamayan hisseler: " + ", ".join(sorted(result.earnings_unknown, key=str))]
    grouped = underlying_reject_groups(result)
    if grouped:
        lines += ["", "Hisse filtresinde elenenler:"]
        lines += [f"  {label}: {', '.join(items)}" for label, items in grouped.items()]
    if result.errors:
        lines += ["", "Hatalar:"] + [f"  {s}: {m}" for s, m in result.errors]
    lines += ["", "Bu liste bir araştırma çıktısıdır, yatırım tavsiyesi değildir."]
    return "\n".join(lines)


def build_html(result: ScanResult, cfg: dict, today: dt.date) -> str:
    esc = html.escape
    th = "padding:6px 8px;text-align:right;background:#1f3a5f;color:#fff;font-weight:600;white-space:nowrap"
    th2 = th.replace("#1f3a5f", "#5b6b7f")
    td = "padding:5px 8px;text-align:right;border-bottom:1px solid #e3e6ea;white-space:nowrap"
    head = ["Hisse", "Fiyat", "Vade", "DTE", "Strike", "OTM%", "Bid", "Ask", "Spread%", "Delta", "IV",
            "IVR / IVP", "OI", "Hacim", "Yıllık%", "Başabaş", "Teminat", "Earnings"]
    parts = [
        "<div style=\"font-family:Arial,Helvetica,sans-serif;color:#222;font-size:13px\">",
        f"<h2 style=\"margin:0 0 4px\">CSP Scanner — {today:%d.%m.%Y}</h2>",
        f"<p style=\"margin:0 0 12px;color:#555\"><b>{len(result.candidates)}</b> aday · "
        f"{result.symbols_scanned}/{result.symbols_total} hisse tarandı · {result.api_calls} API çağrısı</p>",
    ]
    if result.candidates:
        parts.append("<table style=\"border-collapse:collapse;font-size:12px\"><tr>")
        parts += [f"<th style=\"{th}\">{h}</th>" for h in head]
        parts.append("</tr>")
        for c in result.candidates:
            earnings = f"{c['earnings_date']:%d.%m.%y}" if c["earnings_date"] else "-"
            cells = [
                f"<b>{esc(c['symbol'])}</b>", fmt(c["spot"]), f"{c['expiry']:%d.%m.%y}", str(c["dte"]),
                fmt(c["strike"]), fmt(c["otm_pct"], 1), fmt(c["bid"]), fmt(c["ask"]),
                fmt(c["spread_pct"], 1), fmt(c["delta"]),
                fmt(c["iv"] * 100 if c["iv"] is not None else None, 0, "%"),
                esc(iv_text(c)), fmt(c["open_interest"], 0), fmt(c["volume"], 0),
                f"<b>{c['annualized_pct']:.1f}</b>", fmt(c["breakeven"]), f"${c['collateral']:,.0f}", earnings,
            ]
            parts.append("<tr>" + "".join(f"<td style=\"{td}\">{v}</td>" for v in cells) + "</tr>")
        parts.append("</table>")
    else:
        parts.append("<p><b>Bugün tüm filtreleri geçen aday bulunamadı.</b> Aşağıdaki özet ve yakın kaçanlar nedenini gösterir.</p>")

    # Yakın kaçanlar
    parts.append("<h3 style=\"margin:20px 0 4px\">Yakın kaçanlar</h3>"
                 "<p style=\"margin:0 0 8px;color:#555\">Yalnızca <b>tek bir</b> filtreye takılıp elenen kontratlar. "
                 "Hangi kuralı gevşetirsen hangi kontratların girebileceğini gösterir.</p>")
    groups = group_near_misses(result, cfg)
    if not groups:
        parts.append("<p style=\"color:#555\">Yok.</p>")
    near_head = ["Hisse", "Vade", "DTE", "Strike", "Bid", "Delta", "OTM%", "Yıllık%", "Takıldığı yer"]
    for _, label, total, shown in groups:
        parts.append(f"<p style=\"margin:12px 0 4px\"><b>{esc(label)}</b> — toplam {total} kontrat"
                     f"{'' if total == len(shown) else f' (en iyi {len(shown)} gösteriliyor)'}</p>")
        parts.append("<table style=\"border-collapse:collapse;font-size:12px\"><tr>")
        parts += [f"<th style=\"{th2}\">{h}</th>" for h in near_head]
        parts.append("</tr>")
        for c in shown:
            cells = [f"<b>{esc(c['symbol'])}</b>", f"{c['expiry']:%d.%m.%y}", str(c["dte"]), fmt(c["strike"]),
                     fmt(c["bid"]), fmt(c["delta"]), fmt(c["otm_pct"], 1), f"<b>{c['annualized_pct']:.1f}</b>",
                     esc(c["miss_detail"])]
            parts.append("<tr>" + "".join(f"<td style=\"{td}\">{v}</td>" for v in cells) + "</tr>")
        parts.append("</table>")
    if result.earnings_unknown:
        parts.append("<p style=\"margin:12px 0 0\"><b>Earnings tarihi bulunamayan hisseler:</b> "
                     + esc(", ".join(sorted(result.earnings_unknown, key=str))) + "</p>")

    parts.append("<h3 style=\"margin:20px 0 6px\">Filtre özeti</h3><table style=\"border-collapse:collapse;font-size:12px\">")
    for key, label in FUNNEL_LABELS:
        weight = "font-weight:700" if key == "passed" else ""
        parts.append(f"<tr><td style=\"padding:3px 12px 3px 0;{weight}\">{esc(label)}</td>"
                     f"<td style=\"padding:3px 0;text-align:right;{weight}\">{result.funnel.get(key, 0)}</td></tr>")
    parts.append("</table>")

    grouped = underlying_reject_groups(result)
    if grouped:
        parts.append("<h3 style=\"margin:18px 0 6px\">Hisse filtresinde elenenler</h3><ul style=\"margin:0;padding-left:18px\">")
        parts += [f"<li><b>{esc(label)}:</b> {esc(', '.join(items))}</li>" for label, items in grouped.items()]
        parts.append("</ul>")

    if result.errors:
        parts.append("<h3 style=\"margin:18px 0 6px;color:#a33\">Hatalar</h3><ul style=\"margin:0;padding-left:18px\">")
        parts += [f"<li>{esc(str(s))}: {esc(m)}</li>" for s, m in result.errors]
        parts.append("</ul>")

    icfg = cfg["iv"]
    notes = [
        "Veri: MarketData cached modu (en fazla ~20 dk gecikmeli olabilir). Emir vermeden önce güncel fiyatı broker'ında kontrol et.",
        "Yıllık getiri = bid / strike × 365 / DTE. Teminat = strike × 100. Başabaş = strike − bid.",
        "Earnings: sonraki tarih biliniyor ve vade bu tarihten kesinlikle önce olmalı.",
        f"IVR/IVP: kendi biriktirdiğimiz IV geçmişinden hesaplanır (en az {icfg['min_history_days']} gün gerekir). "
        "'birikiyor (n)' yazıyorsa filtre henüz uygulanmıyor, değer birikme aşamasında.",
        "Yakın kaçan = yalnızca tek bir filtreye takılan kontrat. Hisse filtresi yüzünden elenenlerde earnings/IV kontrol edilmez.",
        "Piyasa değeri (5 milyar $) filtresi uygulanmıyor; evren önceden seçilmiş büyük şirketlerden oluşuyor.",
        "Bu liste bir araştırma çıktısıdır, yatırım tavsiyesi değildir.",
    ]
    parts.append("<h3 style=\"margin:18px 0 6px\">Notlar</h3><ul style=\"margin:0;padding-left:18px;color:#555\">")
    parts += [f"<li>{esc(n)}</li>" for n in notes]
    parts.append("</ul></div>")
    return "".join(parts)


def write_outputs(result: ScanResult, cfg: dict, today: dt.date, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.html").write_text(build_html(result, cfg, today), encoding="utf-8")
    (outdir / "report.txt").write_text(build_text(result, cfg, today), encoding="utf-8")
    cols = ["symbol", "option_symbol", "spot", "expiry", "dte", "strike", "bid", "ask", "delta", "iv",
            "open_interest", "volume", "spread_pct", "otm_pct", "annualized_pct", "breakeven",
            "collateral", "earnings_date", "iv_status", "iv_rank", "iv_pct"]
    with open(outdir / "candidates.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.candidates)
    with open(outdir / "near_misses.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols + ["miss_code", "miss_detail"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.near_misses)


# --------------------------------------------------------------------------- ana akış
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Günlük CSP tarayıcısı")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--symbols", default=os.environ.get("SYMBOLS_OVERRIDE", ""),
                        help="Virgülle ayrılmış semboller (test için)")
    parser.add_argument("--no-mail", action="store_true", help="Mail gönderme, sadece out/ klasörüne yaz")
    parser.add_argument("--force", action="store_true", help="Piyasa kapalı olsa da çalış")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    token = os.environ.get("MARKETDATA_TOKEN", "").strip()
    if not token:
        print("HATA: MARKETDATA_TOKEN tanımlı değil (GitHub Secrets).", file=sys.stderr)
        return 2

    today = dt.datetime.now(NY).date()
    try:
        override = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        symbols = override or normalise_universe(cfg["universe"])
    except ValueError as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 2
    md = MarketData(token, max_calls=cfg["safety"]["max_api_calls"],
                    mode=cfg["marketdata"]["mode"], maxage=cfg["marketdata"]["maxage"])

    try:
        if cfg["safety"]["skip_when_market_closed"] and not args.force:
            if md.market_is_open(today) is False:
                print(f"{today}: piyasa kapalı, tarama atlandı.")
                return 0
        history = IVHistory(ROOT / "data" / "iv_history.csv")
        result = run_scan(cfg, md, history, today, symbols)
    except MarketDataAuthError as exc:
        print(f"HATA: MarketData yetki hatası: {exc}", file=sys.stderr)
        return 2

    history.save()
    write_outputs(result, cfg, today, ROOT / "out")
    print(build_text(result, cfg, today))

    failed_all = result.symbols_scanned == 0
    if not args.no_mail:
        user = os.environ.get("GMAIL_USER", "")
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        to = os.environ.get("MAIL_TO", "") or user
        if not (user and password):
            print("HATA: GMAIL_USER / GMAIL_APP_PASSWORD tanımlı değil.", file=sys.stderr)
            return 2
        subject = (f"CSP Scanner {today:%d.%m.%Y}: TARAMA BAŞARISIZ" if failed_all
                   else f"CSP Scanner {today:%d.%m.%Y}: {len(result.candidates)} aday")
        send_mail(subject, build_text(result, cfg, today), build_html(result, cfg, today), user, password, to)
        print(f"Mail gönderildi: {to}")
    return 1 if failed_all else 0


if __name__ == "__main__":
    sys.exit(main())
