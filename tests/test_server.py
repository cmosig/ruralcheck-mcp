"""The MCP tools against a fake API: what each answer and each refusal turns into.

Runs offline. `python -m pytest` or `python tests/test_server.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
from mcp.client import Client

from ruralcheck_mcp import server

DONE = {
    "id": "abc123", "status": "done", "location": {"lat": 48.0, "lon": 8.2},
    "checks": ["flood"], "runs": ["flood"], "errors": {},
    "modules": {"flood": {"available": True, "levels": []}},
    "attribution": ["Flood hazard maps (c) European Union, JRC."],
    "disclaimer": "For information only.", "horizon": {"azimuth_deg": [0.0] * 360},
}


def fake_api(routes: dict):
    """A transport answering (method, path) from `routes`, and recording each request."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = routes[(request.method, request.url.path)]
        if callable(answer):
            answer = answer(request)
        status, body = answer
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle), seen


async def call(transport, tool: str, args: dict, key: str | None = "rc_test"):
    server.TRANSPORT = transport
    if key:
        os.environ["RURALCHECK_API_KEY"] = key
    else:
        os.environ.pop("RURALCHECK_API_KEY", None)
    try:
        async with Client(server.mcp) as client:
            result = await client.call_tool(tool, args)
    finally:
        server.TRANSPORT = None
        os.environ.pop("RURALCHECK_API_KEY", None)
    text = result.content[0].text
    return result.is_error, (text if result.is_error else json.loads(text))


def run(coro):
    return asyncio.run(coro)


PLOT_TOOLS = {"list_plots", "get_plot", "save_plot", "update_plot", "file_plot",
              "delete_plot", "restore_plot", "list_folders", "create_folder",
              "rename_folder", "delete_folder"}
# The tools that change something, on the server or in the account.
WRITES = {"analyse_plot", "save_plot", "update_plot", "file_plot", "delete_plot",
          "restore_plot", "create_folder", "rename_folder", "delete_folder"}


def test_the_instructions_say_what_a_report_is_as_the_site_does():
    # cmosig/ruralcheck#430. The sentence itself is held to the site's /llms.txt
    # by tests/test_live.py.
    assert server.CLAIM in server.INSTRUCTIONS
    assert "estimate" not in server.INSTRUCTIONS.lower()


def test_tools_are_listed_with_honest_hints():
    async def go():
        async with Client(server.mcp) as client:
            return {t.name: t for t in (await client.list_tools()).tools}
    tools = run(go())
    assert set(tools) == {"find_place", "check_coverage", "list_checks",
                          "analyse_plot", "get_report", "sample_report",
                          "upload_listings", "get_listing_upload", "list_listing_uploads"} | PLOT_TOOLS
    # These start work on the server or change something, so they must not claim
    # to be read-only.
    starts = WRITES | {"upload_listings"}
    assert all(tools[n].annotations.read_only_hint is False for n in starts)
    assert all(tools[n].annotations.read_only_hint for n in tools if n not in starts)
    # What loses something the reader wrote says so, so a client can ask first.
    for name in ("delete_plot", "delete_folder", "update_plot"):
        assert tools[name].annotations.destructive_hint is True, name
    assert "Trash" in tools["delete_plot"].description


def test_every_tool_has_a_title_and_every_hint():
    # Claude's directory review wants a title on every tool, and ChatGPT's wants
    # readOnlyHint, destructiveHint and openWorldHint as explicit booleans.
    async def go():
        async with Client(server.mcp) as client:
            return (await client.list_tools()).tools
    for t in run(go()):
        assert t.title and len(t.title.split()) <= 3, t.name
        a = t.annotations
        for hint in (a.read_only_hint, a.destructive_hint, a.open_world_hint):
            assert isinstance(hint, bool), t.name


def test_report_finished_in_one_call():
    transport, seen = fake_api({("POST", "/api/v1/reports"): (200, DONE)})
    err, out = run(call(transport, "analyse_plot", {"lat": 48.0, "lon": 8.2, "checks": ["flood"]}))
    assert not err
    assert out["results"] == DONE["modules"]
    assert out["attribution"] == DONE["attribution"]
    assert "horizon" not in out, "drawing data for the web page is not for the model"
    assert json.loads(seen[0].content) == {"lat": 48.0, "lon": 8.2, "checks": ["flood"]}
    assert seen[0].url.params["drawing"] == "false"


def test_report_still_running_is_polled_to_the_end():
    polls = iter([{"id": "abc123", "status": "running", "progress": 0.5}, DONE])
    transport, seen = fake_api({
        ("POST", "/api/v1/reports"): (202, {"id": "abc123", "status": "running"}),
        # The API answers 202 while the report runs.
        ("GET", "/api/v1/reports/abc123"): lambda r: (
            (lambda b: (202 if b["status"] == "running" else 200, b))(next(polls))),
    })
    err, out = run(call(transport, "analyse_plot", {"lat": 48.0, "lon": 8.2}))
    assert not err and out["status"] == "done"
    assert [r.method for r in seen] == ["POST", "GET", "GET"]
    assert all(r.url.params["drawing"] == "false" for r in seen)


def test_report_that_outlasts_the_call_hands_back_its_id():
    server.WAIT_S, before = 0, server.WAIT_S
    try:
        transport, _ = fake_api({
            ("POST", "/api/v1/reports"): (202, {"id": "abc123", "status": "running"}),
            ("GET", "/api/v1/reports/abc123"): (200, {"id": "abc123", "status": "running"}),
        })
        err, out = run(call(transport, "analyse_plot", {"lat": 48.0, "lon": 8.2}))
    finally:
        server.WAIT_S = before
    assert not err and out["id"] == "abc123" and "get_report" in out["note"]


def test_a_report_id_cannot_leave_the_reports_path():
    transport, seen = fake_api({})
    for bad in ("../../account", "..", "abc/../../job/x", "abc?x=1"):
        err, _ = run(call(transport, "get_report", {"report_id": bad}))
        assert err, bad
    assert not seen, [str(r.url) for r in seen]


def test_point_not_served_is_an_answer_not_an_error():
    body = {"detail": "We do not analyse this point.", "served": False, "coverage": {"served": False}}
    transport, _ = fake_api({("POST", "/api/v1/reports"): (409, body)})
    err, out = run(call(transport, "analyse_plot", {"lat": 60.0, "lon": 5.0}))
    assert not err and out["served"] is False and out["detail"] == body["detail"]


def test_unknown_check_is_an_error_listing_the_valid_names():
    body = {"detail": "Unknown check: noize.", "valid": ["flood", "noise"]}
    transport, _ = fake_api({("POST", "/api/v1/reports"): (422, body)})
    err, text = run(call(transport, "analyse_plot", {"lat": 48.0, "lon": 8.2, "checks": ["noize"]}))
    assert err and "noize" in text and '"noise"' in text


def test_refusals_say_what_to_do():
    for status, words in [(401, "RURALCHECK_API_KEY"), (402, "plan"), (429, "Wait")]:
        transport, _ = fake_api({("POST", "/api/v1/reports"): (status, {"detail": "no."})})
        err, text = run(call(transport, "analyse_plot", {"lat": 48.0, "lon": 8.2}))
        assert err and words in text, (status, text)


def test_coordinates_outside_europe_never_reach_the_api():
    transport, seen = fake_api({})
    err, _ = run(call(transport, "analyse_plot", {"lat": -33.9, "lon": 18.4}))
    assert err and not seen


def test_every_tool_needs_a_key_and_says_so():
    args = {"find_place": {"query": "Furtwangen"}, "check_coverage": {"lat": 48.0, "lon": 8.2},
            "list_checks": {},
            "analyse_plot": {"lat": 48.0, "lon": 8.2}, "get_report": {"report_id": "abc123"},
            "sample_report": {}, "upload_listings": {"listings": [{"lat": 36.6, "lon": -4.7}]},
            "get_listing_upload": {"upload_id": 3}, "list_listing_uploads": {},
            "list_plots": {}, "get_plot": {"plot_id": 1},
            "save_plot": {"report_id": "abc123"}, "update_plot": {"plot_id": 1, "name": "x"},
            "file_plot": {"plot_id": 1, "folder_id": 2}, "delete_plot": {"plot_id": 1},
            "restore_plot": {"plot_id": 1}, "list_folders": {},
            "create_folder": {"name": "Spain"}, "rename_folder": {"folder_id": 2, "name": "x"},
            "delete_folder": {"folder_id": 2}}
    for tool, a in args.items():
        transport, seen = fake_api({})
        err, text = run(call(transport, tool, a, key=None))
        assert err and "paid plan" in text, (tool, text)
        assert not seen, f"{tool} reached the API without a key"


UPLOAD = {"id": 3, "name": "May", "status": "running", "total": 2, "done": 0, "to_run": 1,
          "not_run": 1, "page": "https://ruralcheck.eu/listings/3", "listings": [
              {"position": 1, "name": "Finca", "reference": "C-1", "lat": 36.6, "lon": -4.7,
               "status": "waiting", "note": "", "plot_id": None, "client_link": None},
              {"position": 2, "name": "Plot", "reference": "C-2", "lat": None, "lon": None,
               "status": "not_run", "note": "No position given.", "plot_id": None,
               "client_link": None}]}


def test_listings_are_sent_and_followed():
    transport, seen = fake_api({("POST", "/api/v1/listings"): (202, UPLOAD),
                                ("GET", "/api/v1/listings/3"): (200, UPLOAD),
                                ("GET", "/api/v1/listings"): (200, {"uploads": [UPLOAD]})})
    err, out = run(call(transport, "upload_listings", {"name": "May", "listings": [
        {"name": "Finca", "lat": 36.6, "lon": -4.7, "reference": "C-1"},
        {"name": "Plot", "reference": "C-2"}]}))
    assert not err and out["id"] == 3
    sent = json.loads(seen[0].content)
    assert sent["name"] == "May" and sent["listings"][1] == {
        "name": "Plot", "lat": None, "lon": None, "reference": "C-2"}
    err, out = run(call(transport, "get_listing_upload", {"upload_id": 3}))
    assert not err and out["listings"][1]["note"] == "No position given."
    err, out = run(call(transport, "list_listing_uploads", {}))
    assert not err and out["uploads"][0]["id"] == 3


def test_a_second_upload_or_the_wrong_plan_is_told_why():
    for status, detail in [(409, "An upload is still running on this account."),
                           (402, "Uploading listings comes with the Professional plan.")]:
        transport, _ = fake_api({("POST", "/api/v1/listings"): (status, {"detail": detail})})
        err, text = run(call(transport, "upload_listings", {"listings": [{"lat": 1, "lon": 2}]}))
        assert err and detail in text, text


def test_an_upload_id_is_a_number():
    transport, seen = fake_api({})
    err, _ = run(call(transport, "get_listing_upload", {"upload_id": "../account"}))
    assert err and not seen


def test_key_is_sent():
    transport, seen = fake_api({("GET", "/api/v1/checks"): (200, {"checks": []})})
    run(call(transport, "list_checks", {}))
    assert seen[0].headers["Authorization"] == "Bearer rc_test"


def test_http_callers_key_wins_over_the_environment():
    ctx = SimpleNamespace(headers={"authorization": "Bearer rc_caller"})
    api = server._client(ctx)
    assert api._http.headers["Authorization"] == "Bearer rc_caller"


PLOT = {"id": 5, "name": "Near Testby", "note": "", "lat": 48.0, "lon": 8.2,
        "folder": None, "in_trash": False, "trashed_at": None, "deleted_after": None,
        "report": {"made": "2026-09-26", "every_check": True, "current": True}}


def test_a_report_is_kept_and_filed():
    transport, seen = fake_api({
        ("POST", "/api/v1/plots"): (200, {**PLOT, "created": True}),
        ("PATCH", "/api/v1/plots/5"): (200, {**PLOT, "folder": {"id": 2, "name": "Spain"}}),
    })
    err, out = run(call(transport, "save_plot", {"report_id": "abc123", "folder_id": 2}))
    assert not err and out["created"]
    assert json.loads(seen[0].content) == {"report_id": "abc123", "folder_id": 2}
    err, out = run(call(transport, "file_plot", {"plot_id": 5, "folder_id": 2}))
    assert not err and out["folder"]["id"] == 2
    assert json.loads(seen[1].content) == {"folder_id": 2}


def test_taking_a_plot_out_of_its_folder_sends_null():
    transport, seen = fake_api({("PATCH", "/api/v1/plots/5"): (200, PLOT)})
    err, _ = run(call(transport, "file_plot", {"plot_id": 5}))
    assert not err and json.loads(seen[0].content) == {"folder_id": None}


def test_renaming_sends_only_what_changes():
    transport, seen = fake_api({("PATCH", "/api/v1/plots/5"): (200, PLOT)})
    err, _ = run(call(transport, "update_plot", {"plot_id": 5, "note": "south slope"}))
    assert not err and json.loads(seen[0].content) == {"note": "south slope"}
    err, text = run(call(transport, "update_plot", {"plot_id": 5}))
    assert err and "name" in text and len(seen) == 1


def test_delete_moves_to_the_trash_and_restore_brings_it_back():
    binned = {**PLOT, "in_trash": True, "deleted_after": "2026-10-26"}
    transport, seen = fake_api({
        ("DELETE", "/api/v1/plots/5"): (200, binned),
        ("POST", "/api/v1/plots/5/restore"): (200, PLOT),
        ("GET", "/api/v1/plots"): (200, {"plots": [binned], "folders": [], "trash_days": 30}),
    })
    err, out = run(call(transport, "delete_plot", {"plot_id": 5}))
    assert not err and out["in_trash"] and out["deleted_after"]
    err, out = run(call(transport, "list_plots", {"in_trash": True}))
    assert not err and out["plots"][0]["id"] == 5
    assert seen[1].url.params["in_trash"] == "true"
    err, out = run(call(transport, "restore_plot", {"plot_id": 5}))
    assert not err and out["in_trash"] is False
    # Nothing this server sends deletes a plot for good.
    assert [r.method for r in seen] == ["DELETE", "GET", "POST"]
    assert all("forever" not in r.url.path and not r.url.path.endswith("/trash") for r in seen)


def test_a_kept_report_comes_back_as_results_without_drawing():
    transport, seen = fake_api({("GET", "/api/v1/plots/5"): (
        200, {**PLOT, "modules": {"flood": {"available": True}}, "attribution": ["x"]})})
    err, out = run(call(transport, "get_plot", {"plot_id": 5}))
    assert not err and out["results"] == {"flood": {"available": True}} and "modules" not in out
    assert seen[0].url.params["drawing"] == "false"


def test_folders():
    transport, seen = fake_api({
        ("GET", "/api/v1/folders"): (200, {"folders": [{"id": 2, "name": "Spain", "plots": 1}]}),
        ("POST", "/api/v1/folders"): (200, {"id": 3, "name": "France", "plots": 0}),
        ("PATCH", "/api/v1/folders/3"): (200, {"id": 3, "name": "Lot", "plots": 0}),
        ("DELETE", "/api/v1/folders/3"): (200, {"removed": True}),
    })
    assert run(call(transport, "list_folders", {}))[1]["folders"][0]["name"] == "Spain"
    assert run(call(transport, "create_folder", {"name": "France"}))[1]["id"] == 3
    assert run(call(transport, "rename_folder", {"folder_id": 3, "name": "Lot"}))[1]["name"] == "Lot"
    assert run(call(transport, "delete_folder", {"folder_id": 3}))[1]["removed"]
    assert json.loads(seen[1].content) == {"name": "France"}


def test_a_plot_or_folder_id_is_a_number():
    transport, seen = fake_api({})
    for tool, args in (("get_plot", {"plot_id": "../account"}), ("delete_plot", {"plot_id": 0}),
                       ("file_plot", {"plot_id": 5, "folder_id": "1/../x"}),
                       ("delete_folder", {"folder_id": -1}),
                       ("save_plot", {"report_id": "../../x"})):
        err, _ = run(call(transport, tool, args))
        assert err, (tool, args)
    assert not seen, [str(r.url) for r in seen]


def test_a_refused_plot_says_why():
    detail = "Only a report with every check can be kept. Ask for the report without naming checks."
    transport, _ = fake_api({("POST", "/api/v1/plots"): (409, {"detail": detail})})
    err, text = run(call(transport, "save_plot", {"report_id": "abc123"}))
    assert err and "every check" in text and "analyse this point" not in text, text


def test_registry_entry_matches_the_package():
    # server.json is what the official MCP Registry publishes, and a published
    # version can never be changed. Its version is the one the server reports, its
    # name is the one the README carries, and every package it names must exist
    # where it says, or the publish is refused (none is on PyPI today).
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "server.json")) as f:
        entry = json.load(f)
    with open(os.path.join(root, "README.md")) as f:
        readme = f.read()
    assert entry["version"] == server.__version__
    assert f"mcp-name: {entry['name']}" in readme
    assert len(entry["description"]) <= 100
    assert [r["url"] for r in entry["remotes"]] == ["https://ruralcheck.eu/mcp"]
    assert "packages" not in entry


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
