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
    ("rej_earnings", "Earnings kuralına takılan"),
    ("rej_earnings_unknown", "Earnings tarihi bilinmiyor (elendi)"),
    ("rej_iv", "IV rank / percentile yetersiz"),
    ("warn_earnings_unknown", "Earnings tarihi bilinmiyor (uyarıyla listeye alındı)"),
    ("passed", "TÜM FİLTRELERİ GEÇEN"),
]

# Yakın kaçan gruplarının başlıkları (gösterim sırası bu listedir)
NEAR_LABELS = [
    ("earnings", "Sadece earnings kuralına takılanlar"),
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


# --------------------------------------------------------------------------- earnings kuralları
def earnings_rules(cfg: dict) -> dict:
    """Earnings ayarları. Eski config.yaml'da yeni anahtarlar yoksa varsayılanlar geçerli olur."""
    e = cfg.get("earnings", {}) or {}
    return {
        "min_days": e.get("min_days_to_earnings", 30),          # tarama tarihi ile earnings arası en az bu kadar gün
        "expiry_before": e.get("expiry_before_earnings", False),  # True: vade de earnings'ten önce olmalı
        "unknown": e.get("when_date_unknown", "warn"),            # warn = uyarıyla listele, reject = ele
        "lookahead": e.get("lookahead_days", 180),
    }


def mark_earnings(cand: dict, next_earnings: dt.date | None, unknown_note: str) -> None:
    cand["earnings_date"] = next_earnings
    cand["spans_earnings"] = bool(next_earnings and cand["expiry"] >= next_earnings)
    if next_earnings is None:
        cand["earnings_note"] = f"earnings {unknown_note}"
    elif cand["spans_earnings"]:
        cand["earnings_note"] = "vade earnings'i kapsıyor"
    else:
        cand["earnings_note"] = ""


def earnings_note_text(cfg: dict) -> str:
    r = earnings_rules(cfg)
    text = f"Earnings: tarama tarihi ile sonraki earnings arası en az {r['min_days']} gün olmalı."
    if r["expiry_before"]:
        text += " Ayrıca vade earnings tarihinden önce olmalı."
    else:
        text += " Vade earnings'i kapsayabilir; böyle satırlar 'vade earnings'i kapsıyor' diye işaretlenir."
    text += (" Tarihi bulunamayan hisseler 'earnings bilinmiyor' uyarısıyla listelenir."
             if r["unknown"] == "warn" else " Tarihi bulunamayan hisseler elenir.")
    return text


def missing_breakdown(result) -> str:
    labels = [("missing_vade", "vade yok"), ("missing_strike", "strike yok"), ("missing_delta", "delta yok"),
              ("missing_bid", "bid yok"), ("missing_ask", "ask yok/0")]
    parts = [f"{label} {result.funnel[key]}" for key, label in labels if result.funnel.get(key)]
    return ", ".join(parts)


def price_out_of_range(last: float, ucfg: dict) -> bool:
    """Hisse fiyat sınırı varsayılan olarak KAPALI (liste senin kontrolünde).
    Açmak için config.yaml: underlying.use_price_limits: true"""
    if not ucfg.get("use_price_limits", False):
        return False
    lo, hi = ucfg.get("price_min"), ucfg.get("price_max")
    return (lo is not None and last < lo) or (hi is not None and last > hi)


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
        missing = []
        if exp is None:
            missing.append("vade")
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
            "earnings_note": "", "spans_earnings": False,
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
    ocfg, icfg = cfg["option"], cfg["iv"]
    rules = earnings_rules(cfg)
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
            dates = md.earnings_dates(symbol, today, today + dt.timedelta(days=rules["lookahead"]))
            upcoming = [d for d in dates if d >= today]
            next_earnings = min(upcoming) if upcoming else None
        except MarketDataAuthError:
            raise
        except MarketDataError as exc:
            earnings_failed = True
            result.errors.append((symbol, f"earnings alınamadı: {exc}"))
        if next_earnings is None:
            result.earnings_unknown.append(symbol)
        unknown_note = "alınamadı" if earnings_failed else "bilinmiyor"

        def earnings_status(cand):
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
                    return f"earnings {next_earnings:%d.%m.%y} ({days} gün sonra) < {rules['min_days']} gün"
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
            mark_earnings(cand, next_earnings, unknown_note)
            if next_earnings is None:
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
                    detail = f"IV geçmişi yetersiz ({metrics.get('n')} gün)"
                cand["miss_code"], cand["miss_detail"] = "iv", detail
                result.near_misses.append(cand)
                continue
            all_candidates.append(cand)

        # Tek kontrat filtresine takılanlar: earnings ve IV'yi de geçiyorlarsa gerçek "yakın kaçan"
        for cand in near_contracts:
            if earnings_status(cand) or not iv_passes(metrics, icfg):
                continue
            mark_earnings(cand, next_earnings, unknown_note)
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
            f"IVR/IVP {iv_text(c)} earnings {earnings}"
            + (f" !! {c['earnings_note']}" if c.get("earnings_note") else ""))
    lines += ["", "Filtre özeti:"]
    for key, label in FUNNEL_LABELS:
        lines.append(f"  {label}: {result.funnel.get(key, 0)}")
    detail = missing_breakdown(result)
    if detail:
        lines.append(f"  (Eksik veri dökümü, bir kontrat birden fazla alanda eksik olabilir: {detail})")

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


# --------------------------------------------------------------------------- HTML tasarımı
# E-posta istemcileri (Gmail vb.) için güvenli: tablo düzeni + satır içi (inline) stiller, script yok.
TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
             "Kasım", "Aralık"]
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK, MUTED, LINE, PAGE, NAVY = "#1c2733", "#6b7785", "#e3e8ee", "#eef1f5", "#0f2740"


def tr_date(day: dt.date) -> str:
    return f"{day.day} {TR_MONTHS[day.month - 1]} {day.year}, {TR_DAYS[day.weekday()]}"


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


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div style="font-size:11px;color:{MUTED};margin-top:2px;line-height:14px">{sub}</div>' if sub else ""
    return (f'<td width="25%" valign="top" style="padding:0 4px"><div style="background:#ffffff;border:1px solid {LINE};'
            f'border-radius:10px;padding:12px 14px"><div style="font-size:10.5px;font-weight:700;color:{MUTED};'
            f'letter-spacing:.7px;text-transform:uppercase">{html.escape(label)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{INK};margin-top:4px;line-height:26px">{value}</div>'
            f'{sub_html}</div></td>')


def _warn_chip(note: str) -> str:
    if "kapsıyor" in note:
        return _chip("⚠ vade kapsıyor", "#fff3d6", "#8a5a00", note)
    if "alınamadı" in note:
        return _chip("⚠ tarih alınamadı", "#fde8e8", "#a12626", note)
    return _chip("⚠ tarih bilinmiyor", "#fff3d6", "#8a5a00", note)


def _candidate_row(c: dict, idx: int, min_hist: int) -> str:
    esc = html.escape
    bg = "#ffffff" if idx % 2 == 0 else "#fafbfd"
    earn = f"{c['earnings_date']:%d.%m.%y}" if c["earnings_date"] else "—"
    note = c.get("earnings_note", "")
    if c["iv_status"] == "OK":
        iv_top, iv_sub = f"{c['iv_rank']:.0f} / {c['iv_pct']:.0f}", f"{c['iv_n']} gün"
    elif c["iv_status"] == "BUILDING":
        iv_top, iv_sub = f'<span style="color:{MUTED}">birikiyor</span>', f"{c['iv_n']}/{min_hist} gün"
    else:
        iv_top, iv_sub = "—", ""
    iv_pct = f"IV %{c['iv'] * 100:.0f}" if c.get("iv") is not None else "IV —"
    pill = (f'<span style="display:inline-block;padding:2px 9px;border-radius:6px;background:#e3f4ea;color:#146c3a;'
            f'font-weight:700;font-size:13px">%{c["annualized_pct"]:.1f}</span>')
    cells = [
        _cell(f'<b style="font-size:14px">{esc(c["symbol"])}</b>', f"${c['spot']:,.2f}", "left"),
        _cell(f"{c['expiry']:%d.%m.%y}", f"{c['dte']} gün", "left"),
        _cell(f"<b>{c['strike']:,.2f}</b>", f"OTM %{c['otm_pct']:.1f}"),
        _cell(f"{c['bid']:.2f} / {c['ask']:.2f}", f"spread %{c['spread_pct']:.1f}"),
        _cell(f"{c['delta']:.2f}", iv_pct),
        _cell(fmt(c["open_interest"], 0), f"hacim {fmt(c['volume'], 0)}"),
        _cell(pill, f"dönem %{c['static_return_pct']:.2f}"),
        _cell(f"${c['collateral']:,.0f}", f"başabaş {c['breakeven']:,.2f}"),
        _cell(earn, _warn_chip(note) if note else "", "left"),
        _cell(iv_top, iv_sub),
    ]
    return f'<tr style="background:{bg}">' + "".join(cells) + "</tr>"


def _near_table(shown: list[dict]) -> str:
    esc = html.escape
    widths = (11, 12, 11, 9, 9, 9, 10, 29)   # sütun genişlikleri (%), tüm yakın kaçan tablolarında aynı
    head = "".join(_th(lbl, al).replace("<th ", f'<th width="{w}%" ', 1) for (lbl, al), w in zip(
        [("Hisse", "left"), ("Vade", "left"), ("Strike", "right"), ("Bid", "right"), ("Delta", "right"),
         ("OTM", "right"), ("Yıllık", "right"), ("Takıldığı yer", "left")], widths))
    rows = []
    for i, c in enumerate(shown):
        bg = "#ffffff" if i % 2 == 0 else "#fafbfd"
        cells = [
            _cell(f"<b>{esc(c['symbol'])}</b>", "", "left"),
            _cell(f"{c['expiry']:%d.%m.%y}", f"{c['dte']} gün", "left"),
            _cell(f"{c['strike']:,.2f}"), _cell(f"{c['bid']:.2f}"), _cell(f"{c['delta']:.2f}"),
            _cell(f"%{c['otm_pct']:.1f}"), _cell(f"<b>%{c['annualized_pct']:.1f}</b>"),
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
        out += (f'<div style="margin-top:8px;font-size:11.5px;color:{MUTED}">Eksik veri dökümü '
                f'(bir kontrat birden fazla alanda eksik olabilir): {esc(detail)}</div>')
    return out


def build_html(result: ScanResult, cfg: dict, today: dt.date) -> str:
    esc = html.escape
    min_hist = cfg["iv"]["min_history_days"]
    passed = result.funnel.get("passed", 0)
    evaluated = result.symbols_scanned - len(result.underlying_rejects)
    warn_rows = sum(1 for c in result.candidates if c.get("earnings_note"))

    # ---- başlık bandı + özet kutuları
    header = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{NAVY};'
        f'border-radius:10px;margin:0 0 12px"><tr><td style="padding:22px 24px">'
        f'<div style="font-size:11px;letter-spacing:1.6px;color:#8fb3d9;font-weight:700">CASH SECURED PUT TARAMASI</div>'
        f'<div style="font-size:26px;font-weight:700;color:#ffffff;margin-top:4px;line-height:30px">CSP Scanner</div>'
        f'<div style="font-size:13px;color:#c5d5e6;margin-top:4px">{tr_date(today)} · {result.api_calls} API çağrısı</div>'
        f'</td><td align="right" valign="middle" style="padding:22px 24px">'
        f'<div style="font-size:38px;font-weight:700;color:#ffffff;line-height:40px">{len(result.candidates)}</div>'
        f'<div style="font-size:11px;letter-spacing:1.2px;color:#8fb3d9;font-weight:700">ADAY</div></td></tr></table>')
    kpis = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 -4px 14px;'
        f'width:calc(100% + 8px)"><tr>'
        + _kpi("Taranan hisse", f"{result.symbols_scanned}<span style='font-size:14px;color:{MUTED}'>/{result.symbols_total}</span>")
        + _kpi("Değerlendirilen", f"{evaluated}", "hisse filtresini geçti")
        + _kpi("İncelenen kontrat", f"{result.funnel.get('contracts_total', 0):,}", "put, vade ve delta dahil")
        + _kpi("Filtreyi geçen", f"{passed:,}",
               f"mailde en iyi {len(result.candidates)}" if passed > len(result.candidates) else "kontrat")
        + "</tr></table>")

    # ---- adaylar
    if result.candidates:
        head = "".join([_th("Hisse", "left"), _th("Vade", "left"), _th("Strike"), _th("Bid / Ask"),
                        _th("Delta / IV"), _th("OI / Hacim"), _th("Yıllık"), _th("Teminat"),
                        _th("Earnings", "left"), _th("IVR / IVP")])
        rows = "".join(_candidate_row(c, i, min_hist) for i, c in enumerate(result.candidates))
        body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="border:1px solid {LINE};border-radius:8px;border-collapse:separate;border-spacing:0">'
                f'<tr>{head}</tr>{rows}</table>')
        legend = []
        if warn_rows:
            legend.append(f"{_chip('⚠ vade kapsıyor', '#fff3d6', '#8a5a00')} opsiyonun vadesi earnings tarihinden sonra, "
                          "pozisyon açıklamayı açık taşır.")
            legend.append(f"{_chip('⚠ tarih bilinmiyor', '#fff3d6', '#8a5a00')} MarketData sonraki earnings tarihini "
                          "vermedi, kendin doğrula.")
        if result.earnings_unknown:
            chips = "".join(_chip(s, "#fff3d6", "#8a5a00") for s in sorted(result.earnings_unknown, key=str))
            legend.append(f"<b>Earnings tarihi bulunamayan hisseler:</b> {chips}")
        if legend:
            body += (f'<div style="margin-top:10px;font-size:11.5px;line-height:20px;color:{MUTED}">'
                     + "<br>".join(legend) + "</div>")
        cards = _card("Adaylar", body, "Yıllık getiriye göre sıralı. Yıllık = bid / strike × 365 / DTE · "
                                       "Teminat = strike × 100 · Başabaş = strike − bid")
    else:
        cards = _card("Adaylar",
                      f'<div style="background:#f6f8fb;border:1px dashed #c3cfdb;border-radius:8px;padding:16px;'
                      f'font-size:13px;color:{INK}"><b>Bugün tüm filtreleri geçen aday bulunamadı.</b> '
                      f'Aşağıdaki yakın kaçanlar ve filtre özeti nedenini gösterir.</div>')

    # ---- yakın kaçanlar
    groups = group_near_misses(result, cfg)
    if groups:
        near = ""
        for _, label, total, shown in groups:
            extra = "" if total == len(shown) else f" · en iyi {len(shown)}"
            near += (f'<div style="margin:16px 0 6px;font-size:12.5px;font-weight:700;color:{INK}">{esc(label)} '
                     f'{_chip(f"toplam {total}{extra}", "#eef2f7", "#44566c")}</div>{_near_table(shown)}')
    else:
        near = f'<div style="font-size:12.5px;color:{MUTED}">Yok.</div>'
    cards += _card("Yakın kaçanlar", near.replace('margin:16px 0 6px', 'margin:0 0 6px', 1) if groups else near,
                   "Yalnızca <b>tek bir</b> filtreye takılıp elenen kontratlar. Hangi kuralı gevşetirsen hangi "
                   "kontratların girebileceğini gösterir.")

    # ---- filtre özeti + hisse filtresi
    cards += _card("Filtre özeti", _funnel(result), "Kontrat düzeyinde: her kontrat takıldığı ilk filtreye sayılır.")
    grouped = underlying_reject_groups(result)
    if grouped:
        blocks = ""
        for label, items in grouped.items():
            chips = "".join(_chip(i, "#eef2f7", "#44566c") for i in items)
            blocks += (f'<div style="margin:0 0 8px"><div style="font-size:12px;font-weight:700;color:{INK};'
                       f'margin-bottom:4px">{esc(label)} <span style="color:{MUTED};font-weight:400">· {len(items)}</span></div>'
                       f'{chips}</div>')
        cards += _card("Hisse filtresinde elenenler", blocks, "Bu hisselerin kontratları hiç değerlendirilmedi.")

    # ---- hatalar
    if result.errors:
        errs = "".join(f"<li>{esc(str(s))}: {esc(m)}</li>" for s, m in result.errors)
        cards += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fdf0f0;'
                  f'border:1px solid #f1c6c6;border-radius:10px;margin:0 0 14px"><tr><td style="padding:14px 18px">'
                  f'<div style="font-size:14px;font-weight:700;color:#a12626">Hatalar</div>'
                  f'<ul style="margin:8px 0 0;padding-left:18px;font-size:12px;color:#7a2020">{errs}</ul></td></tr></table>')

    # ---- notlar
    icfg = cfg["iv"]
    notes = [
        "Veri: MarketData cached modu (en fazla ~20 dk gecikmeli olabilir). Emir vermeden önce güncel fiyatı broker'ında kontrol et.",
        earnings_note_text(cfg),
        f"IVR/IVP: kendi biriktirdiğimiz IV geçmişinden hesaplanır (en az {icfg['min_history_days']} gün gerekir). "
        "'birikiyor' yazıyorsa filtre henüz uygulanmıyor.",
        "Yakın kaçan = yalnızca tek bir filtreye takılan kontrat. Hisse filtresi yüzünden elenenlerde earnings/IV kontrol edilmez.",
        "Piyasa değeri (5 milyar $) filtresi uygulanmıyor; evren sana ait takip listesinden oluşuyor.",
    ]
    note_html = "".join(f"<li style='margin-bottom:3px'>{esc(n)}</li>" for n in notes)
    footer = (f'<div style="padding:4px 6px 0;font-size:11.5px;line-height:17px;color:{MUTED}">'
              f'<div style="font-weight:700;margin-bottom:4px">Notlar</div>'
              f'<ul style="margin:0 0 12px;padding-left:18px">{note_html}</ul>'
              f'<div style="border-top:1px solid {LINE};padding-top:10px">CSP Scanner · MarketData verisi · '
              f'Bu liste bir araştırma çıktısıdır, yatırım tavsiyesi değildir.</div></div>')

    inner = header + kpis + cards + footer
    return ('<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
            f'<title>CSP Scanner {today:%d.%m.%Y}</title></head>'
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
    cols = ["symbol", "option_symbol", "spot", "expiry", "dte", "strike", "bid", "ask", "delta", "iv",
            "open_interest", "volume", "spread_pct", "otm_pct", "annualized_pct", "breakeven",
            "collateral", "earnings_date", "earnings_note", "iv_status", "iv_rank", "iv_pct"]
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
