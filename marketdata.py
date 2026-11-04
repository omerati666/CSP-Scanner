"""MarketData.app API istemcisi (sadece bu projede gereken uçlar)."""
from __future__ import annotations

import datetime as dt
import time

import requests

BASE_URL = "https://api.marketdata.app/v1"
UTC = dt.timezone.utc


class MarketDataError(Exception):
    """Genel API hatası."""


class MarketDataAuthError(MarketDataError):
    """Token hatalı veya yetkisiz. Tüm taramayı durdurur."""


def columnar_rows(data: dict) -> list[dict]:
    """MarketData'nın sütun biçimindeki cevabını satır listesine çevirir."""
    cols = {k: v for k, v in data.items() if isinstance(v, list)}
    if not cols:
        return []
    n = min(len(v) for v in cols.values())
    return [{k: v[i] for k, v in cols.items()} for i in range(n)]


class MarketData:
    def __init__(self, token: str, max_calls: int = 400, mode: str = "cached",
                 maxage: str = "20min", session=None, timeout: int = 30, sleep=time.sleep):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.max_calls = max_calls
        self.mode = mode
        self.maxage = maxage
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sleep = sleep
        self.calls = 0

    # ------------------------------------------------------------------ düşük seviye
    def _get(self, path: str, params: dict | None = None) -> dict | None:
        url = BASE_URL + path
        last_err = "unknown error"
        for attempt in range(4):
            if self.calls >= self.max_calls:
                raise MarketDataError("API call limit (safety.max_api_calls) exceeded")
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, headers=self.headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = f"connection error: {exc.__class__.__name__}"
                self._sleep(2 ** attempt)
                continue
            if resp.status_code in (401, 403):
                raise MarketDataAuthError(f"HTTP {resp.status_code}: token invalid or unauthorized")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                self._sleep(int(retry_after) if retry_after.isdigit() else 2 ** attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                raise MarketDataError(f"HTTP {resp.status_code}: non-JSON response")
            if resp.status_code in (200, 203):  # 203 = cached veri
                status = body.get("s")
                if status == "no_data":
                    return None
                if status == "error":
                    raise MarketDataError(str(body.get("errmsg", "API error")))
                return body
            if resp.status_code == 404 and body.get("s") == "no_data":
                return None
            raise MarketDataError(f"HTTP {resp.status_code}: {body.get('errmsg', '')}")
        raise MarketDataError(f"Retries failed ({last_err})")

    # ------------------------------------------------------------------ uçlar
    def quote(self, symbol: str) -> dict | None:
        body = self._get(f"/stocks/quotes/{symbol}/", {"extended": "false"})
        rows = columnar_rows(body) if body else []
        return rows[0] if rows else None

    def put_chain(self, symbol: str, date_from: dt.date, date_to: dt.date) -> list[dict]:
        body = self._get(
            f"/options/chain/{symbol}/",
            {"from": date_from.isoformat(), "to": date_to.isoformat(), "side": "put",
             "mode": self.mode, "maxage": self.maxage},
        )
        return columnar_rows(body) if body else []

    def earnings_dates(self, symbol: str, date_from: dt.date, date_to: dt.date) -> list[dt.date]:
        body = self._get(f"/stocks/earnings/{symbol}/",
                         {"from": date_from.isoformat(), "to": date_to.isoformat()})
        dates = []
        for row in (columnar_rows(body) if body else []):
            value = row.get("reportDate")
            if isinstance(value, (int, float)):
                dates.append(dt.datetime.fromtimestamp(value, tz=UTC).date())
            elif isinstance(value, str):
                try:
                    dates.append(dt.date.fromisoformat(value[:10]))
                except ValueError:
                    pass
        return sorted(set(dates))

    def market_is_open(self, day: dt.date) -> bool | None:
        """True/False döner; belirlenemezse None (tarama yine de devam eder)."""
        try:
            body = self._get("/markets/status/", {"date": day.isoformat()})
        except MarketDataAuthError:
            raise
        except MarketDataError:
            return None
        if not body:
            return None
        status = body.get("status")
        if isinstance(status, list) and status:
            return str(status[0]).lower() == "open"
        return None
