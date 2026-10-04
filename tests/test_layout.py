"""Layout tracking: every run of a test measures each element it acts on and compares it with the
last run of the same test that measured the same step - in plan mode and in agent mode."""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.execution.layout import compare
from conftest import Api
from test_agent import ScriptedAgent, agent_test, run_once
from test_execution import LOGIN, run_one

BOX = {"x": 100, "y": 200, "w": 80, "h": 30, "vw": 1280, "vh": 800, "fixed": False, "region": "main / form f",
       "ox": 10, "oy": 50}


def test_compare_tells_moves_shifts_and_resizes_apart():
    ref = {"box": BOX}
    assert compare(BOX, None, 3) is None
    assert compare({**BOX, "x": 102}, ref, 3)["kinds"] == []  # within tolerance
    moved = compare({**BOX, "x": 140, "ox": 50}, ref, 3)
    assert moved["kinds"] == ["moved"] and moved["summary"] == 'moved +40px, +0px inside "main / form f"'
    shifted = compare({**BOX, "y": 260}, ref, 3)  # its region moved down; its place inside it did not
    assert shifted["kinds"] == ["shifted"] and "together with" in shifted["summary"]
    region = compare({**BOX, "region": "header", "y": 10}, ref, 3)
    assert region["kinds"] == ["moved_region"] and 'from "main / form f" to "header"' in region["summary"]
    assert compare({**BOX, "w": 120}, ref, 3)["kinds"] == ["resized"]
    assert "viewport size differs" in compare({**BOX, "vw": 800}, ref, 3)["summary"]


def layout_of(report: dict, action_prefix: str) -> dict:
    return next(s["layout"] for s in report["steps"] if s["description"].startswith(action_prefix) and s.get("layout"))


def test_every_rerun_measures_and_compares_the_acted_on_elements(api, site):
    p = api.project("Demo", site.base_url)
    tid = api.add_tests(p["id"], LOGIN)[0]["id"]

    first = run_one(api, p["id"], tid)
    assert first["status"] == "passed"
    assert first["layout"]["measured"] == 3 and first["layout"]["baseline"] == 3  # 2 fields + button
    click = layout_of(first, "Click")
    assert click["reference"] is None and click["change"]["summary"].startswith("baseline")
    assert click["box"]["region"] == "main / form login-form" and click["box"]["w"] > 0

    same = run_one(api, p["id"], tid)
    assert same["layout"] == {"measured": 3, "compared": 3, "baseline": 0, "changed": []}
    assert layout_of(same, "Click")["reference"]["testRunId"] == first["testRunId"]
    assert "3 element(s) compared with the last run, none moved" in same["summary"]

    site.variant = "moved"  # 40px further right inside the form
    moved = run_one(api, p["id"], tid)
    assert moved["status"] == "passed"  # a layout change is reported, not failed
    [change] = moved["layout"]["changed"]
    assert change["kinds"] == ["moved"] and change["summary"] == 'moved +40px, +0px inside "main / form login-form"'
    assert "1 of 3 compared element(s) moved or resized" in moved["summary"]
    events = api.run_detail(p["id"], moved["runId"])["events"]
    assert any("Layout change at" in e["message"] and e["level"] == "warning" for e in events)

    site.variant = "relocated"  # the same button, now in the page header
    relocated = run_one(api, p["id"], tid)
    changes = {c["step"].split(" ")[0]: c for c in relocated["layout"]["changed"]}
    assert changes["Click"]["kinds"][0] == "moved_region"
    assert 'from "main / form login-form" to "header"' in changes["Click"]["summary"]
    # the taller header pushed the form down: its fields shifted with it (a true, separate change)
    assert {c["kinds"][0] for s, c in changes.items() if s != "Click"} == {"shifted"}


def test_agent_reruns_compare_the_replayed_elements(settings, site):
    agent = ScriptedAgent()
    with TestClient(create_app(replace(settings, action_timeout_ms=1500), llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid, tid = agent_test(api, site)
        first, _ = run_once(api, pid, tid)
        assert first["layout"]["measured"] >= 4 and first["layout"]["compared"] == 0
        second, _ = run_once(api, pid, tid)  # replayed from memory: the same elements, measured again
        assert second["verdictSource"] == "replay"
        assert second["layout"]["compared"] == first["layout"]["measured"] and second["layout"]["changed"] == []
        remembered = api.call("GET", f"/projects/{pid}/tests/{tid}/memory")["steps"]
        assert any(s.get("box") for s in remembered)  # the memory keeps each step's element box
