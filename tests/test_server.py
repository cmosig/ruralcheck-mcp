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


def test_tools_are_listed_with_honest_hints():
    async def go():
        async with Client(server.mcp) as client:
            return {t.name: t for t in (await client.list_tools()).tools}
    tools = run(go())
    assert set(tools) == {"find_place", "check_coverage", "list_checks",
                          "analyse_plot", "get_report", "sample_report"}
    # analyse_plot spends credits, so it must not claim to be read-only.
    assert tools["analyse_plot"].annotations.read_only_hint is False
    assert all(tools[n].annotations.read_only_hint for n in tools if n != "analyse_plot")


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
        ("GET", "/api/v1/reports/abc123"): lambda r: (200, next(polls)),
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
            "sample_report": {}}
    for tool, a in args.items():
        transport, seen = fake_api({})
        err, text = run(call(transport, tool, a, key=None))
        assert err and "paid plan" in text, (tool, text)
        assert not seen, f"{tool} reached the API without a key"


def test_key_is_sent():
    transport, seen = fake_api({("GET", "/api/v1/checks"): (200, {"checks": []})})
    run(call(transport, "list_checks", {}))
    assert seen[0].headers["Authorization"] == "Bearer rc_test"


def test_http_callers_key_wins_over_the_environment():
    ctx = SimpleNamespace(headers={"authorization": "Bearer rc_caller"})
    api = server._client(ctx)
    assert api._http.headers["Authorization"] == "Bearer rc_caller"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
