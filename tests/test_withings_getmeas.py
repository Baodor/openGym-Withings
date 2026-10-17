"""Regression test for the documented Withings weight/body-composition endpoint.

No real Withings credentials or network requests are used.
"""
import asyncio
from urllib.parse import parse_qs

import httpx


def test_getmeas_calls_measure_not_v2_measure(monkeypatch, tmp_path):
    # app.main creates its local DB and session keys at import time.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import main

    seen = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "wbsapi.withings.net"
        assert request.url.path == "/measure"
        assert request.headers.get("Authorization") == "Bearer test-access-token"
        form = parse_qs(request.content.decode("utf-8"))
        assert form["action"] == ["getmeas"]
        assert form["category"] == ["1"]
        assert "1" in form["meastypes"][0].split(",")
        return httpx.Response(200, json={
            "status": 0,
            "body": {"measuregrps": [
                {"grpid": 42, "date": 1792242000, "measures": [
                    {"type": 1, "value": 88750, "unit": -3}
                ]}
            ], "more": 0}
        })

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(reply)

    def mocked_client(**kwargs):
        return original_client(transport=transport, **kwargs)

    async def access_token():
        return "test-access-token"

    monkeypatch.setattr(main.httpx, "AsyncClient", mocked_client)
    monkeypatch.setattr(main, "fresh_access_token", access_token)

    rows = asyncio.run(main.fetch_measures())
    assert len(seen) == 1
    assert len(rows) == 1
    assert rows[0]["weight_kg"] == 88.75
