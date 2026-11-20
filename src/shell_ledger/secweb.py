"""Polite access to SEC EDGAR: declared contact, rate limit, stop on refusal."""
from __future__ import annotations

import gzip
import os
import threading
import time
import urllib.error
import urllib.request


class SecError(RuntimeError):
    pass


_last = [0.0]
_lock = threading.Lock()
MIN_INTERVAL = 0.125  # 8 request starts a second across all threads; the SEC's ceiling is 10


def _pace() -> None:
    with _lock:
        wait = _last[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise SecError(
            "SEC_USER_AGENT is not set. The SEC requires every automated request to name a contact. "
            "Put a line like  SEC_USER_AGENT=Your Name you@example.com  in the .env file."
        )
    return ua


def get(url: str, retries: int = 4, timeout: int = 90):
    """Fetch a URL. Returns bytes, or None for 404. Raises SecError on refusal."""
    ua = user_agent()
    for attempt in range(retries):
        _pace()
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 403:
                raise SecError(
                    f"The SEC refused {url} (403). Stopping instead of retrying. "
                    "Check that SEC_USER_AGENT names a real contact, then wait ten minutes."
                ) from e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 3)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2 ** attempt * 3)
    raise SecError(f"Gave up on {url} after {retries} tries.")
