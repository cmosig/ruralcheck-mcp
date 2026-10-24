"""The MCP tools against a real RuralCheck API: is the contract still the one this
server was written for?

Skipped unless both are set:
    RURALCHECK_LIVE_URL   e.g. http://127.0.0.1:8046 or https://ruralcheck.eu
    RURALCHECK_API_KEY    a key on that instance
Run it after any change to /api/v1 on the RuralCheck side, against the preview
that carries the change. Runs one noise check and one whole report.

An account starts one new report a minute through the API (the owner's decision of
2026-10-04), so the tests space their reports a minute apart and take about three
minutes.

The plot round trip keeps one plot on the account and leaves it in the Trash,
where the daily purge removes it 30 days later.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from mcp.client import Client

LIVE = os.environ.get("RURALCHECK_LIVE_URL")
pytestmark = pytest.mark.skipif(not (LIVE and os.environ.get("RURALCHECK_API_KEY")),
                                reason="set RURALCHECK_LIVE_URL and RURALCHECK_API_KEY")

PLOT = {"lat": 48.015, "lon": 8.19}  # the sample report's plot, in the Black Forest

_last_start = [0.0]


def spaced():
    """Wait until a new report may start: one a minute per account, counted from
    when the last call that started one returned (started())."""
    import time
    gap = 62 - (time.monotonic() - _last_start[0])
    if _last_start[0] and gap > 0:
        time.sleep(gap)


def started():
    import time
    _last_start[0] = time.monotonic()


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
    spaced()
    (e1, checks), (e2, cov), (e4, place), (e5, sample), (e6, report), (e7, bad) = calls(
        ("list_checks", {}),
        ("check_coverage", PLOT),
        ("find_place", {"query": "Furtwangen"}),
        ("sample_report", {"checks": ["flood"]}),
        ("analyse_plot", {**PLOT, "checks": ["noise"]}),
        ("analyse_plot", {**PLOT, "checks": ["no_such_check"]}),
    )
    started()
    assert not any((e1, e2, e4, e5, e6)), (checks, cov, place, sample, report)

    names = {c["name"] for c in checks["checks"]}
    assert {"flood", "noise", "sun", "parcel"} <= names
    assert all({"name", "runs"} <= set(c) for c in checks["checks"])

    assert cov["served"] is True
    assert place["results"] and "lat" in place["results"][0]
    assert "flood" in sample["results"]

    assert report["status"] == "done", report
    assert "noise" in report["results"] and report["attribution"]
    assert "total_db" in report["results"]["noise"]
    # drawing=false: no map outlines or pictures reach the model.
    assert not any(k in json.dumps(report["results"]) for k in ('"outlines"', '"slope_image"'))

    assert e7 and "no_such_check" in bad


def test_find_place_finds_regions_and_postcodes():
    """A region and a postcode are areas: they carry their extent and say whether
    reports are made there (cmosig/ruralcheck, the search's areas)."""
    (e1, region), (e2, code) = calls(("find_place", {"query": "Styria"}),
                                     ("find_place", {"query": "33540"}))
    assert not (e1 or e2), (region, code)
    top = region["results"][0]
    assert top["kind"] == "region" and len(top["bbox"]) == 4 and top["served"] in ("yes", "part", "no")
    assert code["results"][0]["kind"] == "postcode"


def test_listings_go_in_and_come_back_with_their_reasons():
    """Needs a key on the Professional plan. Sends two listings that will not run,
    so the test saves no plot and runs no report: one without a position and one
    out at sea."""
    import time
    (e1, up), = calls(("upload_listings", {"name": "ruralcheck-mcp live test", "listings": [
        {"name": "No position", "reference": "LIVE-1"},
        {"name": "At sea", "lat": 45.0, "lon": -20.0, "reference": "LIVE-2"}]}))
    if e1 and "still running" in up:
        pytest.skip("this account has an upload running")
    assert not e1, up
    assert up["total"] == 2 and up["page"].endswith(f"/listings/{up['id']}")
    for _ in range(30):
        (e2, got), (e3, uploads) = calls(("get_listing_upload", {"upload_id": up["id"]}),
                                         ("list_listing_uploads", {}))
        assert not (e2 or e3), (got, uploads)
        if got["status"] == "done":
            break
        time.sleep(1)
    assert got["status"] == "done", got
    notes = {x["reference"]: (x["status"], x["note"]) for x in got["listings"]}
    assert notes["LIVE-1"] == ("not_run", "No position given.")
    assert notes["LIVE-2"][0] == "not_run" and notes["LIVE-2"][1]
    assert up["id"] in [u["id"] for u in uploads["uploads"]]


def test_a_plot_is_kept_filed_trashed_and_restored():
    # One report at a time per account, so the whole one finishes first, and one new
    # report a minute, so the second waits for that too.
    spaced()
    ((e1, whole),) = calls(("analyse_plot", PLOT))
    started()
    assert not e1, whole
    while whole["status"] == "running":
        ((e, whole),) = calls(("get_report", {"report_id": whole["id"]}))
        assert not e, whole
    assert whole["status"] == "done", whole
    # A second report within the minute is refused, and the sentence says why.
    ((e_soon, soon),) = calls(("analyse_plot", {**PLOT, "checks": ["noise"]}))
    # "Custom solution" since the plan was renamed (ruralcheck #776); the old
    # name is accepted until that is live.
    assert e_soon and ("Custom solution" in soon or "Business plan" in soon), soon
    spaced()
    ((e2, part),) = calls(("analyse_plot", {**PLOT, "checks": ["noise"]}))
    started()
    assert not e2 and part["status"] == "done", part

    folder_name = f"Live test {uuid.uuid4().hex[:6]}"
    (e3, refused), (e4, kept), (e5, folder) = calls(
        ("save_plot", {"report_id": part["id"]}),
        ("save_plot", {"report_id": whole["id"]}),
        ("create_folder", {"name": folder_name}),
    )
    assert e3 and "every check" in refused, refused
    assert not e4 and not e5, (kept, folder)
    pid, fid = kept["id"], folder["id"]

    steps = calls(
        ("file_plot", {"plot_id": pid, "folder_id": fid}),
        ("list_plots", {"folder_id": fid}),
        ("get_plot", {"plot_id": pid}),
        ("delete_plot", {"plot_id": pid}),
        ("list_plots", {"folder_id": fid}),
        ("list_plots", {"in_trash": True}),
        ("update_plot", {"plot_id": pid, "note": "x"}),
        ("restore_plot", {"plot_id": pid}),
        ("delete_folder", {"folder_id": fid}),
        ("get_plot", {"plot_id": pid}),
        ("delete_plot", {"plot_id": pid}),
    )
    (_, filed), (_, in_folder), (_, got), (_, binned), (_, folder_after), (_, trash), \
        (e_edit, edit), (_, back), (_, gone), (_, loose), (_, last) = steps
    assert not any(e for i, (e, _) in enumerate(steps) if i != 6), steps
    assert filed["folder"]["id"] == fid
    assert [p["id"] for p in in_folder["plots"]] == [pid]
    assert "noise" in got["results"] and got["report"]["every_check"]
    assert binned["in_trash"] and binned["deleted_after"]
    assert folder_after["plots"] == []
    assert pid in [p["id"] for p in trash["plots"]]
    assert e_edit and "Restore it first" in edit, edit
    assert back["in_trash"] is False and back["folder"]["id"] == fid
    assert gone["removed"] is True
    # The folder went, and the plot stayed, in no folder.
    assert loose["folder"] is None and not loose["in_trash"]
    assert last["in_trash"]


def test_the_site_says_what_this_server_says():
    """The instructions an assistant reads on connecting say what a report is made of
    in the site's own words. /llms.txt is drawn from ruralcheck/provenance.py there,
    so a change of wording on the site fails here until this server follows it."""
    import httpx
    from ruralcheck_mcp import server
    llms = httpx.get(f"{LIVE.rstrip('/')}/llms.txt", timeout=30).text
    assert server.CLAIM in llms, "the site no longer says " + server.CLAIM
