"""A small async client for the RuralCheck HTTP API.

Everything the MCP tools do goes through here, so the tools stay a thin layer of
descriptions and the API stays the one place that decides what a report says.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://ruralcheck.eu"
USER_AGENT = "ruralcheck-mcp"


class APIError(Exception):
    """The API refused or failed. `status` is the HTTP status, `body` what it said."""

    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            base_url=os.environ.get("RURALCHECK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=os.environ.get("RURALCHECK_API_KEY") or None,
        )


class RuralCheck:
    """One API key, one connection pool."""

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.AsyncClient(
            base_url=config.base_url, headers=headers,
            timeout=config.timeout_s, transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, *, ok: tuple[int, ...] = (200,),
                    **kwargs: Any) -> tuple[int, Any]:
        r = await self._http.request(method, path, **kwargs)
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code in ok:
            return r.status_code, body
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, str):
            detail = None
        raise APIError(r.status_code, _explain(r.status_code, detail), body)

    # Free, no key needed.

    async def search(self, query: str) -> dict:
        return (await self._call("GET", "/api/v1/search", params={"q": query, "committed": "true"}))[1]

    async def coverage(self, lat: float, lon: float) -> dict:
        return (await self._call("GET", "/api/v1/coverage", params={"lat": lat, "lon": lon}))[1]

    async def checks(self) -> dict:
        return (await self._call("GET", "/api/v1/checks"))[1]

    async def sample(self) -> dict:
        return (await self._call("GET", "/api/v1/sample"))[1]

    # Needs a key.

    async def start(self, lat: float, lon: float, checks: list[str] | None,
                    wait_s: int = 0) -> tuple[int, dict]:
        """Ask for a report: 202 running, 200 finished within `wait_s`, 409 not served."""
        payload: dict[str, Any] = {"lat": lat, "lon": lon}
        if checks:
            payload["checks"] = checks
        return await self._call("POST", "/api/v1/reports", json=payload,
                                params=_report_params(wait_s),
                                ok=(200, 202, 409), timeout=wait_s + self.config.timeout_s)

    async def report(self, report_id: str, wait_s: int = 0) -> dict:
        # The id comes from the model, and goes into a path sent with the reader's
        # key: "../../account" would ask the site for a page that is not the API.
        if not _REPORT_ID.fullmatch(report_id):
            raise APIError(404, "There is no report with this id.")
        # 202 while it runs, with what it has so far: an answer, not a refusal.
        # Treated as one, any report longer than one poll failed the tool call.
        return (await self._call("GET", f"/api/v1/reports/{report_id}",
                                 params=_report_params(wait_s), ok=(200, 202),
                                 timeout=wait_s + self.config.timeout_s))[1]

    # My plots: the plots the account keeps, its folders and the Trash. Every id
    # goes into a path sent with the reader's key, so each is checked to be a
    # whole number before it gets there.

    async def plots(self, folder_id: int | None = None, in_trash: bool = False) -> dict:
        params: dict[str, Any] = {"in_trash": "true" if in_trash else "false"}
        if folder_id is not None:
            params["folder_id"] = _id(folder_id, "folder")
        return (await self._call("GET", "/api/v1/plots", params=params))[1]

    async def plot(self, plot_id: int) -> dict:
        return (await self._call("GET", f"/api/v1/plots/{_id(plot_id, 'plot')}",
                                 params={"drawing": "false"}))[1]

    async def keep(self, report_id: str, name: str | None = None,
                   folder_id: int | None = None) -> dict:
        if not _REPORT_ID.fullmatch(report_id):
            raise APIError(404, "There is no report with this id.")
        payload: dict[str, Any] = {"report_id": report_id}
        if name:
            payload["name"] = name
        if folder_id is not None:
            payload["folder_id"] = _id(folder_id, "folder")
        return (await self._call("POST", "/api/v1/plots", json=payload))[1]

    async def edit_plot(self, plot_id: int, fields: dict[str, Any]) -> dict:
        """PATCH the plot. A `folder_id` of None in `fields` is sent as null, which
        takes the plot out of its folder. A field left out is left as it is."""
        if fields.get("folder_id") is not None:
            fields = {**fields, "folder_id": _id(fields["folder_id"], "folder")}
        return (await self._call("PATCH", f"/api/v1/plots/{_id(plot_id, 'plot')}",
                                 json=fields))[1]

    async def trash(self, plot_id: int) -> dict:
        return (await self._call("DELETE", f"/api/v1/plots/{_id(plot_id, 'plot')}"))[1]

    async def restore(self, plot_id: int) -> dict:
        return (await self._call("POST", f"/api/v1/plots/{_id(plot_id, 'plot')}/restore"))[1]

    async def folders(self) -> dict:
        return (await self._call("GET", "/api/v1/folders"))[1]

    async def new_folder(self, name: str) -> dict:
        return (await self._call("POST", "/api/v1/folders", json={"name": name}))[1]

    async def rename_folder(self, folder_id: int, name: str) -> dict:
        return (await self._call("PATCH", f"/api/v1/folders/{_id(folder_id, 'folder')}",
                                 json={"name": name}))[1]

    async def remove_folder(self, folder_id: int) -> dict:
        return (await self._call("DELETE", f"/api/v1/folders/{_id(folder_id, 'folder')}"))[1]

    # Listing uploads: a Professional account's listings, each run as a full report
    # with a saved plot and a client link, one after another on the server.

    async def send_listings(self, listings: list[dict], name: str = "") -> dict:
        payload: dict[str, Any] = {"listings": listings}
        if name:
            payload["name"] = name
        return (await self._call("POST", "/api/v1/listings", json=payload, ok=(202,)))[1]

    async def listing_upload(self, upload_id: int) -> dict:
        # An int from the tool's own schema, so nothing but digits reaches the path.
        return (await self._call("GET", f"/api/v1/listings/{int(upload_id)}"))[1]

    async def listing_uploads(self) -> dict:
        return (await self._call("GET", "/api/v1/listings"))[1]


def _id(value: Any, what: str) -> int:
    """A plot or folder id as a positive whole number, or a 404 before any request."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise APIError(404, f"There is no {what} with this id.")
    return value


_REPORT_ID = re.compile(r"[A-Za-z0-9]{1,64}")


def _report_params(wait_s: int) -> dict:
    # drawing=false leaves out the outlines and pictures the web page draws on its
    # map: three quarters of a flood answer, and nothing a model can read.
    params: dict[str, Any] = {"drawing": "false"}
    if wait_s:
        params["wait"] = wait_s
    return params


def _explain(status: int, detail: str | None) -> str:
    """Say what went wrong in words an agent can relay to a person."""
    base = {
        401: "The RuralCheck API key is missing or not valid. Set RURALCHECK_API_KEY.",
        402: "This account's plan does not include this.",
        403: "This account may not do that.",
        404: "Not found.",
        # analyse_plot reads a 409 as an answer (the point is not served), so one
        # that reaches here is a plot or folder refused, and `detail` says why.
        409: "RuralCheck could not do this.",
        422: "The request was not valid.",
        429: "Too many requests. Wait a minute and try again.",
        503: "RuralCheck is busy or restarting. Try again shortly.",
    }.get(status, f"The RuralCheck API answered {status}.")
    return f"{base} ({detail})" if detail else base
