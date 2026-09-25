"""The RuralCheck MCP server: location analysis for rural plots in Europe, as tools.

Run it locally over stdio with an API key in RURALCHECK_API_KEY, or as a remote
server over streamable HTTP, where each caller sends their own key as
`Authorization: Bearer <key>`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import httpx

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from .api import APIError, Config, RuralCheck

INSTRUCTIONS = """\
RuralCheck analyses a point in rural Europe for someone thinking of buying land or a
house there: sun hours with the hills taken into account, modelled road and rail
noise, flood and wildfire history, protected areas, the land-register plot, slope,
climate now and around 2050, drive times and more.

Typical flow: find_place to turn a name into coordinates, check_coverage to see
whether the point is served, then analyse_plot. Ask only for the checks the
question needs, from list_checks: a whole report is large. Every tool needs a paid
RuralCheck plan, which includes every check with no limit on reports.

Everything is computed from public data and is an estimate, not a survey, a
valuation or legal advice. Pass on the caveats each check carries.
"""

LAT = Annotated[float, Field(ge=27, le=72, description="Latitude in degrees (WGS84).")]
LON = Annotated[float, Field(ge=-32, le=45, description="Longitude in degrees (WGS84).")]

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

# How long one tool call waits for a report before handing back an id to poll
# with get_report. A full report is 10-30 s, and the common clients give a tool
# call 60 s or more, so this leaves room.
WAIT_S = int(os.environ.get("RURALCHECK_WAIT_S", "45"))
# The API holds a GET open for up to 30 s itself; a shorter slice lets this
# server send progress to the client between answers.
POLL_S = 5

# Tests swap in an httpx.MockTransport here; nothing else sets it.
TRANSPORT: httpx.AsyncBaseTransport | None = None

# httpx logs every request at INFO; over stdio that is noise on the client's stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer(
    name="ruralcheck",
    title="RuralCheck",
    description="Location analysis for rural property in Europe.",
    instructions=INSTRUCTIONS,
    website_url="https://ruralcheck.eu",
    version=__version__,
)


def _client(ctx: Context | None) -> RuralCheck:
    """A client carrying this caller's key.

    Over HTTP the key is the caller's own Authorization header, so one hosted
    server serves many accounts and holds none of their keys. Over stdio there
    are no headers and the key comes from the environment.
    """
    config = Config.from_env()
    headers = ctx.headers if ctx is not None else None
    if headers:
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            config.api_key = auth[7:].strip() or None
    return RuralCheck(config, transport=TRANSPORT)


async def _call(ctx: Context, fn: Callable[[RuralCheck], Awaitable[Any]]) -> str:
    """Run one API call, turning a refusal into a tool error the model can read.

    The answer goes back as compact JSON: the SDK would indent a dict, and on a
    report that is three quarters of the text the model has to read.
    """
    api = _client(ctx)
    if not api.config.api_key:
        # Checked here as well as by the API, so the refusal is the same sentence
        # whichever tool was called. Listing the tools needs no key: a client has to
        # see what the server offers before anyone can decide to pay for it.
        await api.aclose()
        raise ToolError(NO_KEY)
    try:
        return json.dumps(await fn(api), separators=(",", ":"), ensure_ascii=False)
    except APIError as exc:
        extra = {k: v for k, v in exc.body.items() if k != "detail"} \
            if isinstance(exc.body, dict) else {}
        # A 422 lists the valid check names, a 429 when to come back.
        raise ToolError(str(exc) + (" " + json.dumps(extra) if extra else "")) from None
    except httpx.TransportError as exc:
        raise ToolError(f"Could not reach RuralCheck ({type(exc).__name__}). Try again shortly.") from None
    finally:
        await api.aclose()


NO_KEY = ("RuralCheck needs a paid plan. Set RURALCHECK_API_KEY to a key from the "
          "account page at ruralcheck.eu, or connect to the hosted server, which signs "
          "you in.")

CHECKS = Annotated[list[str] | None, Field(
    description="Check names from list_checks, e.g. ['flood', 'noise', 'sun']. "
                "Omit for every check.")]


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def find_place(
    query: Annotated[str, Field(max_length=200, description="A place name, or coordinates as 'lat, lon'.")],
    ctx: Context,
) -> str:
    """Turn a village, town or region name in Europe into coordinates.

    Street addresses are not searchable: an address is
    reduced to the settlement it names and marked approximate.
    """
    return await _call(ctx, lambda api: api.search(query))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def check_coverage(lat: LAT, lon: LON, ctx: Context) -> str:
    """Whether RuralCheck can analyse this point, and how finely the ground is measured.

    `served: false` means analyse_plot will refuse the point, and the answer says
    why.
    """
    return await _call(ctx, lambda api: api.coverage(lat, lon))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def list_checks(ctx: Context) -> str:
    """Every check a report can run: its name, what it answers and roughly how long
    it takes. Pass names from here to analyse_plot.
    """
    return await _call(ctx, lambda api: api.checks())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True, idempotentHint=False),
          structured_output=False)
async def analyse_plot(lat: LAT, lon: LON, ctx: Context, checks: CHECKS = None) -> str:
    """Analyse a plot of rural land in Europe and return the result of each check.

    Checks a chosen one depends on run too and are returned with it. Most answers take a few seconds; if one runs longer than this
    call waits, the answer carries an id to pass to get_report.

    Show the `attribution` lines to whoever reads the results: the data licences
    require it.
    """
    async def run(api: RuralCheck) -> dict:
        deadline = time.monotonic() + WAIT_S
        status, body = await api.start(lat, lon, checks, wait_s=POLL_S)
        if status == 409:
            return {"served": False, "detail": body.get("detail"), "coverage": body.get("coverage")}
        if body.get("status") == "running":
            body = await _wait(api, body["id"], ctx, deadline)
        return _shape(body)
    return await _call(ctx, run)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def get_report(
    report_id: Annotated[str, Field(max_length=64, description="The id analyse_plot returned.")],
    ctx: Context,
) -> str:
    """Fetch a report analyse_plot started, by its id. Waits if it is still running."""
    async def run(api: RuralCheck) -> dict:
        return _shape(await _wait(api, report_id, ctx, time.monotonic() + WAIT_S))
    return await _call(ctx, run)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def sample_report(ctx: Context, checks: CHECKS = None) -> str:
    """The public sample: a real full report for a plot in the Black Forest.

    Use it to see what each check returns before asking for a report of your own.
    """
    async def run(api: RuralCheck) -> dict:
        payload = await api.sample()
        if checks:
            wanted = set(checks)
            payload["modules"] = {k: v for k, v in (payload.get("modules") or {}).items()
                                  if k in wanted}
        return _shape(payload)
    return await _call(ctx, run)


async def _wait(api: RuralCheck, report_id: str, ctx: Context, deadline: float) -> dict:
    """Long-poll until the report is finished or this call has waited long enough."""
    while True:
        left = deadline - time.monotonic()
        body = await api.report(report_id, wait_s=int(max(1, min(POLL_S, left))))
        if body.get("status") != "running" or left <= 0:
            return body
        progress = body.get("progress")
        if isinstance(progress, (int, float)):
            await ctx.report_progress(float(progress), 1.0, "running")


# What a report payload carries that an agent can use. `horizon` and `dem` are
# drawing data for the web page and would only cost tokens.
KEEP = ("id", "status", "location", "checks", "runs", "coverage", "errors",
        "detail", "version", "disclaimer", "attribution")


def _shape(payload: dict[str, Any]) -> dict:
    out = {k: payload[k] for k in KEEP if k in payload}
    out["results"] = payload.get("modules") or {}
    if payload.get("status") == "running":
        out["note"] = "Still running. Call get_report with this id."
    return out


def main() -> None:
    parser = argparse.ArgumentParser(prog="ruralcheck-mcp", description=__doc__)
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.http:
        import uvicorn

        from .http import build
        uvicorn.run(build(mcp, args.host, args.port), host=args.host, port=args.port,
                    proxy_headers=True, log_level="warning")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
