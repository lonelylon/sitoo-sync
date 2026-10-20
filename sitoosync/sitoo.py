"""Klient för Sitoo REST API v2.

Följer Sitoos regler för anropsbegränsning:
- Bara ett anrop åt gången (klienten är synkron och används från en tråd).
- Vid 429 väntar klienten de sekunder som headern X-Rate-Limit-Reset anger.
- Vid 5xx kan API-nyckeln vara låst resten av 10-minutersfönstret, så
  klienten backar av upp till 11 minuter innan nästa försök.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)


class SitooError(Exception):
    def __init__(self, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class SitooClient:
    def __init__(
        self,
        base_url: str,
        account_id: str,
        api_id: str,
        password: str,
        page_size: int = 100,
        timeout: int = 300,
        max_retries: int = 8,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.root = f"{base_url.rstrip('/')}/accounts/{account_id}"
        token = base64.b64encode(f"{api_id}:{password}".encode("utf-8")).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "sitoo-sync/1.0",
        }
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self.request_count = 0

    # ------------------------------------------------------------------ http
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.root + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, safe=",")

        attempt = 0
        while True:
            attempt += 1
            self.request_count += 1
            request = urllib.request.Request(url, headers=self._headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    body = resp.read()
                    return json.loads(body.decode("utf-8")) if body else None

            except urllib.error.HTTPError as err:
                body = _safe_body(err)
                request_id = err.headers.get("Sitoo-Request-ID") if err.headers else None
                retryable = err.code == 429 or err.code >= 500
                if not retryable or attempt > self.max_retries:
                    raise SitooError(
                        f"Sitoo svarade HTTP {err.code} på {path}: {body[:500]}"
                        + (f" (Sitoo-Request-ID {request_id})" if request_id else ""),
                        status=err.code,
                        request_id=request_id,
                    ) from None
                wait = self._wait_seconds(err, attempt)
                log.warning(
                    "Sitoo HTTP %s på %s, väntar %ss (försök %s/%s)%s",
                    err.code, path, wait, attempt, self.max_retries,
                    f" Sitoo-Request-ID {request_id}" if request_id else "",
                )
                self._sleep(wait)

            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                if attempt > self.max_retries:
                    raise SitooError(f"Kunde inte nå Sitoo ({path}): {err}") from None
                wait = min(30 * attempt, 600)
                log.warning("Nätverksfel mot Sitoo (%s), väntar %ss (försök %s/%s)", err, wait, attempt, self.max_retries)
                self._sleep(wait)

    @staticmethod
    def _wait_seconds(err: urllib.error.HTTPError, attempt: int) -> int:
        if err.code == 429:
            reset = (err.headers.get("X-Rate-Limit-Reset") or "").strip() if err.headers else ""
            if reset.isdigit():
                return int(reset) + 2
            return 60
        # 5xx: nyckeln kan vara låst tills 10-minutersfönstret passerat
        return min(60 * attempt, 660)

    # ------------------------------------------------------------ pagination
    def pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[list[dict]]:
        """Bläddrar igenom en lista med start/num. Svaret är {totalcount, items}."""
        start = 0
        while True:
            query = dict(params or {})
            query["start"] = start
            query["num"] = self.page_size
            data = self.get(path, query) or {}
            items = data.get("items") or []
            total = int(data.get("totalcount") or 0)
            if items:
                yield items
            start += len(items)
            if not items or start >= total:
                return

    def all_items(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        result: list[dict] = []
        for page in self.pages(path, params):
            result.extend(page)
        return result


def _safe_body(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
