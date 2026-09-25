"""The hosted server's front door: the sign-in pointer, and who gets through."""
from __future__ import annotations

import asyncio
import json

import httpx

from ruralcheck_mcp import http

S = {"resource": "https://ruralcheck.eu/mcp", "auth": "https://ruralcheck.eu",
     "public": "https://ruralcheck.eu"}


async def inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


def gate(api_status: int = 200):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(api_status, json={})

    return http.TokenGate(inner, S, transport=httpx.MockTransport(handle)), seen


def call(app, path="/mcp", token=None, method="POST"):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {"type": "http", "path": path, "method": method, "headers": headers}
    out = {}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
        else:
            out["body"] = msg["body"]

    asyncio.run(app(scope, receive, send))
    return out


def test_no_token_points_at_the_sign_in():
    app, seen = gate()
    r = call(app)
    assert r["status"] == 401 and not seen
    assert r["headers"]["www-authenticate"] == (
        'Bearer resource_metadata="https://ruralcheck.eu/.well-known/oauth-protected-resource/mcp"')


def test_the_metadata_names_ruralcheck_as_the_authorization_server():
    app, _ = gate()
    r = call(app, "/.well-known/oauth-protected-resource/mcp", method="GET")
    meta = json.loads(r["body"])
    assert r["status"] == 200
    assert meta["resource"] == "https://ruralcheck.eu/mcp"
    assert meta["authorization_servers"] == ["https://ruralcheck.eu"]


def test_a_good_key_gets_through_and_is_checked_once():
    app, seen = gate(200)
    assert call(app, token="rc_good")["body"] == b"inner"
    assert call(app, token="rc_good")["body"] == b"inner"
    assert len(seen) == 1 and seen[0].headers["authorization"] == "Bearer rc_good"


def test_a_free_accounts_key_gets_through_and_the_tools_say_why_not():
    app, _ = gate(402)
    assert call(app, token="rc_free")["status"] == 200


def test_a_bad_key_is_sent_back_to_sign_in():
    app, _ = gate(401)
    r = call(app, token="rc_revoked")
    assert r["status"] == 401 and 'error="invalid_token"' in r["headers"]["www-authenticate"]


def test_anything_else_is_not_found():
    app, _ = gate()
    assert call(app, "/admin", token="rc_good")["status"] == 404


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
