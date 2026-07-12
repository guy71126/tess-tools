from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAST_INVOKE_URL = "https://mast.stsci.edu/api/v0/invoke"
USER_AGENT = "tess-where/0.1"


def invoke_mast_service(
    service: str,
    params: dict[str, Any],
    *,
    timeout_sec: float = 12.0,
    pagesize: int = 2000,
    page: int = 1,
) -> dict[str, Any]:
    request_payload = {
        "service": service,
        "params": params,
        "format": "json",
        "pagesize": pagesize,
        "page": page,
    }
    encoded = urlencode({"request": json.dumps(request_payload)}).encode("utf-8")
    request = Request(
        MAST_INVOKE_URL,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urlopen(request, timeout=timeout_sec) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") not in (None, "COMPLETE"):
        raise RuntimeError(payload.get("msg") or f"MAST status {payload.get('status')}")
    return payload


def mast_data_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("MAST response did not contain a data list")
    return [row for row in data if isinstance(row, dict)]
