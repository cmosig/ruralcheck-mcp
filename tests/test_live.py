"""The MCP tools against a real RuralCheck API: is the contract still the one this
server was written for?

Skipped unless both are set:
    RURALCHECK_LIVE_URL   e.g. http://127.0.0.1:8046 or https://ruralcheck.eu
    RURALCHECK_API_KEY    a key on that instance
Run it after any change to /api/v1 on the RuralCheck side, against the preview
that carries the change. Spends 0.15 of a credit (one noise check).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from mcp.client import Client

LIVE = os.environ.get("RURALCHECK_LIVE_URL")
pytestmark = pytest.mark.skipif(not (LIVE and os.environ.get("RURALCHECK_API_KEY")),
                                reason="set RURALCHECK_LIVE_URL and RURALCHECK_API_KEY")

PLOT = {"lat": 48.015, "lon": 8.19}  # the sample report's plot, in the Black Forest


def calls(*steps):
    os.environ["RURALCHECK_BASE_URL"] = LIVE
    from ruralcheck_mcp import server

    async def go():
        out = []
        async with Client(server.mcp) as client:
            for tool, args in steps:
                r = await client.call_tool(tool, args)
                text = r.content[0].text
                out.append((r.is_error, text if r.is_error else json.loads(text)))
        return out
    return asyncio.run(go())


def test_the_contract_holds():
    (e1, checks), (e2, cov), (e3, quote), (e4, place), (e5, sample), (e6, report), (e7, bad) = calls(
        ("list_checks", {}),
        ("check_coverage", PLOT),
        ("quote_cost", {"checks": ["noise"]}),
        ("find_place", {"query": "Furtwangen"}),
        ("sample_report", {"checks": ["flood"]}),
        ("analyse_plot", {**PLOT, "checks": ["noise"]}),
        ("analyse_plot", {**PLOT, "checks": ["no_such_check"]}),
    )
    assert not any((e1, e2, e3, e4, e5, e6)), (checks, cov, quote, place, sample, report)

    names = {c["name"] for c in checks["checks"]}
    assert {"flood", "noise", "sun", "parcel"} <= names
    assert all({"name", "cost", "free", "runs"} <= set(c) for c in checks["checks"])

    assert cov["served"] is True
    assert quote["cost"] > 0 and "noise" in quote["runs"]
    assert place["results"] and "lat" in place["results"][0]
    assert "flood" in sample["results"]

    assert report["status"] == "done", report
    assert "noise" in report["results"] and report["attribution"]
    assert "total_db" in report["results"]["noise"]
    # drawing=false: no map outlines or pictures reach the model.
    assert not any(k in json.dumps(report["results"]) for k in ('"outlines"', '"slope_image"'))

    assert e7 and "no_such_check" in bad
