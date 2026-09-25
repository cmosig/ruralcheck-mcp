"""The hosted server: streamable HTTP with OAuth sign-in in front of it.

claude.ai, ChatGPT and other MCP clients connect to a remote server by following
the MCP authorization spec: a request without a token gets a 401 pointing at this
server's protected-resource metadata (RFC 9728), which names RuralCheck's own
sign-in (`/.well-known/oauth-authorization-server` on ruralcheck.eu). The client
registers, sends the reader through the consent page, and comes back with a token.
That token is an ordinary RuralCheck API key, so a key pasted into Claude Code or
Cursor is accepted here too.

This server keeps no keys. It asks the API whether a token is good, remembers the
answer for a few minutes, and passes the token on with every call.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import httpx
from mcp.server.transport_security import TransportSecuritySettings

from .api import USER_AGENT, Config

PATH = "/mcp"
# How long a token's answer from the API is trusted. A revoked key keeps working
# here for at most this long, and the API refuses it at once anyway.
TOKEN_TTL_S = 300


def settings() -> dict:
    public = os.environ.get("RURALCHECK_PUBLIC_URL", "https://ruralcheck.eu").rstrip("/")
    return {
        "resource": public + PATH,
        "auth": os.environ.get("RURALCHECK_AUTH_URL", public).rstrip("/"),
        "public": public,
    }


def resource_metadata(s: dict) -> dict:
    return {
        "resource": s["resource"],
        "authorization_servers": [s["auth"]],
        "bearer_methods_supported": ["header"],
        "resource_name": "RuralCheck",
        "resource_documentation": s["auth"] + "/agents",
    }


class TokenGate:
    """ASGI middleware: the metadata document, and a 401 for a missing or bad token."""

    def __init__(self, app, s: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.app = app
        self.s = s
        self.transport = transport
        self._seen: dict[str, tuple[float, bool]] = {}
        meta_url = s["public"] + "/.well-known/oauth-protected-resource" + PATH
        self._challenge = f'Bearer resource_metadata="{meta_url}"'

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith("/.well-known/oauth-protected-resource"):
            return await _json(send, 200, resource_metadata(self.s))
        if path.rstrip("/") != PATH:
            return await _json(send, 404, {"error": "not found"})
        if scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        token = _bearer(scope)
        if not token:
            return await _json(send, 401, {"error": "invalid_token",
                                           "error_description": "Sign in to RuralCheck."},
                               {"www-authenticate": self._challenge})
        if not await self._good(token):
            return await _json(send, 401, {"error": "invalid_token",
                                           "error_description": "This key is not valid."},
                               {"www-authenticate": self._challenge + ', error="invalid_token"'})
        return await self.app(scope, receive, send)

    async def _good(self, token: str) -> bool:
        h = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._seen.get(h)
        if hit and now - hit[0] < TOKEN_TTL_S:
            return hit[1]
        config = Config.from_env()
        async with httpx.AsyncClient(base_url=config.base_url, timeout=10,
                                     transport=self.transport,
                                     headers={"User-Agent": USER_AGENT,
                                              "Authorization": f"Bearer {token}",
                                              # So the API does not count this as the
                                              # reader asking for the check list.
                                              "X-RuralCheck-Token-Check": "1"}) as c:
            r = await c.get("/api/v1/checks")
        # 402 is a real key on a free account: the sign-in worked, and each tool
        # says what the plan does not include. Only 401 means the token is wrong.
        good = r.status_code != 401
        if r.status_code < 500:
            if len(self._seen) > 10_000:
                self._seen.clear()
            self._seen[h] = (now, good)
        return good


def _bearer(scope) -> str:
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            v = v.decode("latin-1")
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return ""


async def _json(send, status: int, body: dict, headers: dict | None = None) -> None:
    raw = json.dumps(body).encode()
    h = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
         (b"cache-control", b"no-store")]
    h += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": h})
    await send({"type": "http.response.body", "body": raw})


def build(mcp, host: str, port: int):
    """The ASGI app to serve: the MCP transport behind the token gate."""
    s = settings()
    public_host = s["public"].split("://", 1)[-1]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[s["public"], f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
    )
    inner = mcp.streamable_http_app(streamable_http_path=PATH, stateless_http=True,
                                    transport_security=security, host=host)
    return TokenGate(inner, s)
