"""
Shared HTTP client for NEXUS SCADA components.

Use this instead of modifying the installed requests library.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:
    requests = None
    HTTPAdapter = None
    Retry = None


BASE_URL = os.environ.get("NEXUS_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("NEXUS_API_KEY", "nexus-dev-key-change-in-prod")
DEFAULT_TIMEOUT = float(os.environ.get("NEXUS_HTTP_TIMEOUT", "2.0"))


def _make_session():
    if requests is None:
        return None

    session = requests.Session()

    if Retry is not None and HTTPAdapter is not None:
        retry = Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_maxsize=10,
            pool_block=False,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)

    session.headers.update(
        {
            "X-API-Key": API_KEY,
            "Accept": "application/json",
        }
    )

    return session


SESSION = _make_session()


def get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if SESSION is None:
        return None

    try:
        resp = SESSION.get(
            f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}",
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )

        if resp.ok:
            return resp.json()

    except Exception:
        return None

    return None


def post_json(
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if SESSION is None:
        return None

    try:
        resp = SESSION.post(
            f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}",
            json=payload,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )

        if resp.ok:
            return resp.json()

    except Exception:
        return None

    return None