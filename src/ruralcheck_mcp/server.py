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
from pydantic import BaseModel, Field

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
RuralCheck plan, which includes every check. An account can start one new report a
minute, so ask for every check a question needs in one analyse_plot call; more than
that is the Business plan.

An estate agent on the Professional plan can send their listings with
upload_listings: each becomes a saved plot with a full report and a client link,
run one after another. Follow the upload with get_listing_upload.

Everything is computed from public data and is an estimate, not a survey, a
valuation or legal advice. Pass on the caveats each check carries.

My plots is the account's own list of kept reports. save_plot keeps a report that
ran every check. Plots can be put in folders, renamed and given a note. delete_plot
moves a plot to the Trash, where it can be restored for 30 days. Nothing here
deletes a plot for good. That happens only on the website, or 30 days later.
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

    An account can start one new report a minute, so name every check needed in
    one call. Fetching a report with get_report does not count.

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


class Listing(BaseModel):
    """One property for sale, as the agent knows it."""
    name: str = Field("", max_length=200, description="What the agent calls the listing.")
    lat: float | None = Field(None, description="Latitude in degrees (WGS84).")
    lon: float | None = Field(None, description="Longitude in degrees (WGS84).")
    reference: str = Field("", max_length=255, description="The agent's own reference.")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True, idempotentHint=False),
          structured_output=False)
async def upload_listings(
    listings: Annotated[list[Listing], Field(min_length=1, max_length=2000, description=(
        "The properties, each with name, lat, lon and reference."))],
    ctx: Context,
    name: Annotated[str, Field(max_length=200, description="A name for this upload.")] = "",
) -> str:
    """Send an estate agent's listings to RuralCheck. Professional plan.

    Each listing becomes a saved plot with a full report and a client link the
    agent can put in the listing. Reports run one after another on the server,
    about half a minute each, and wait for anyone using the site, so this returns
    at once with the upload's id. Follow it with get_listing_upload. Up to 100
    listings run from one upload. A listing that is not run comes back with the
    reason, for example no position, outside the area covered, or past the limit.
    A listing already saved with a current report is not run again. One upload
    runs at a time on an account.
    """
    items = [x.model_dump() for x in listings]
    return await _call(ctx, lambda api: api.send_listings(items, name))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def get_listing_upload(
    upload_id: Annotated[int, Field(ge=1, description="The id upload_listings returned.")],
    ctx: Context,
) -> str:
    """An upload's progress: for each listing its status, its client link once its
    report is done, or the reason it was not run. `status` is "done" when nothing
    is left to run.
    """
    return await _call(ctx, lambda api: api.listing_upload(upload_id))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def list_listing_uploads(ctx: Context) -> str:
    """The account's listing uploads, newest first, with how far each has got."""
    return await _call(ctx, lambda api: api.listing_uploads())


# ---------------------------------------------------------------- My plots

PLOT_ID = Annotated[int, Field(ge=1, description="A plot id from list_plots or save_plot.")]
FOLDER_ID = Annotated[int, Field(ge=1, description="A folder id from list_folders.")]
FOLDER_NAME = Annotated[str, Field(min_length=1, max_length=60, description="The folder's name.")]


def _hints(read_only: bool = False, destructive: bool = False, idempotent: bool = True):
    # openWorldHint is false: these tools touch only the reader's own account.
    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive,
                           idempotentHint=idempotent, openWorldHint=False)


@mcp.tool(annotations=_hints(read_only=True), structured_output=False)
async def list_plots(
    ctx: Context,
    folder_id: Annotated[int | None, Field(ge=1, description="Only the plots in this folder.")] = None,
    in_trash: Annotated[bool, Field(description="True lists the Trash instead.")] = False,
) -> str:
    """The plots this account keeps, newest first, with the key figure of each check,
    and the account's folders.

    Plots in the Trash are left out unless in_trash is true. Each one there has a
    `deleted_after` date. It can be restored until then.
    """
    return await _call(ctx, lambda api: api.plots(folder_id, in_trash))


@mcp.tool(annotations=_hints(read_only=True), structured_output=False)
async def get_plot(plot_id: PLOT_ID, ctx: Context) -> str:
    """One kept plot with its report, as it was when it was kept.

    `report.current` is false when the checks have changed since. Running
    analyse_plot again and keeping the result brings it up to date.
    """
    async def run(api: RuralCheck) -> dict:
        body = await api.plot(plot_id)
        body["results"] = body.pop("modules", None) or {}
        return body
    return await _call(ctx, run)


@mcp.tool(annotations=_hints(), structured_output=False)
async def save_plot(
    report_id: Annotated[str, Field(max_length=64, description="The id analyse_plot returned.")],
    ctx: Context,
    name: Annotated[str | None, Field(max_length=80, description="What to call the plot. "
                                      "Defaults to the nearest village.")] = None,
    folder_id: Annotated[int | None, Field(ge=1, description="A folder to put it in.")] = None,
) -> str:
    """Keep a finished report in My plots.

    Only a report that ran every check can be kept, so call analyse_plot without
    naming checks first. Keeping the same point again replaces its report, keeps its
    name and note, and takes it out of the Trash if it was there.
    """
    return await _call(ctx, lambda api: api.keep(report_id, name, folder_id))


@mcp.tool(annotations=_hints(destructive=True), structured_output=False)
async def update_plot(
    plot_id: PLOT_ID,
    ctx: Context,
    name: Annotated[str | None, Field(max_length=80, description="A new name.")] = None,
    note: Annotated[str | None, Field(max_length=4000, description="The note, which "
                                      "replaces the one there.")] = None,
) -> str:
    """Rename a kept plot or replace its note. A plot in the Trash must be restored first."""
    fields = {k: v for k, v in (("name", name), ("note", note)) if v is not None}
    if not fields:
        raise ToolError("Give a new name, a note, or both.")
    return await _call(ctx, lambda api: api.edit_plot(plot_id, fields))


@mcp.tool(annotations=_hints(), structured_output=False)
async def file_plot(
    plot_id: PLOT_ID,
    ctx: Context,
    folder_id: Annotated[int | None, Field(ge=1, description="The folder. Leave out to take "
                                           "the plot out of its folder.")] = None,
) -> str:
    """Put a kept plot in a folder, or take it out of the one it is in."""
    return await _call(ctx, lambda api: api.edit_plot(plot_id, {"folder_id": folder_id}))


@mcp.tool(annotations=_hints(destructive=True), structured_output=False)
async def delete_plot(plot_id: PLOT_ID, ctx: Context) -> str:
    """Move a kept plot to the Trash.

    It can be restored with restore_plot until its `deleted_after` date, 30 days on,
    and is then removed for good. Nothing through this server deletes a plot at once.
    """
    return await _call(ctx, lambda api: api.trash(plot_id))


@mcp.tool(annotations=_hints(), structured_output=False)
async def restore_plot(plot_id: PLOT_ID, ctx: Context) -> str:
    """Take a plot out of the Trash, back into its folder if that still exists."""
    return await _call(ctx, lambda api: api.restore(plot_id))


@mcp.tool(annotations=_hints(read_only=True), structured_output=False)
async def list_folders(ctx: Context) -> str:
    """The account's folders, each with how many plots are in it."""
    return await _call(ctx, lambda api: api.folders())


@mcp.tool(annotations=_hints(idempotent=False), structured_output=False)
async def create_folder(name: FOLDER_NAME, ctx: Context) -> str:
    """Make a folder for kept plots. Each name can be used once, whatever the case."""
    return await _call(ctx, lambda api: api.new_folder(name))


@mcp.tool(annotations=_hints(), structured_output=False)
async def rename_folder(folder_id: FOLDER_ID, name: FOLDER_NAME, ctx: Context) -> str:
    """Rename a folder."""
    return await _call(ctx, lambda api: api.rename_folder(folder_id, name))


@mcp.tool(annotations=_hints(destructive=True), structured_output=False)
async def delete_folder(folder_id: FOLDER_ID, ctx: Context) -> str:
    """Delete a folder. Its plots stay in My plots, in no folder."""
    return await _call(ctx, lambda api: api.remove_folder(folder_id))


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
