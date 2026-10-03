"""Appending routes must retain the preceding motion and editable section boundaries."""

from copy import deepcopy
from io import BytesIO
import asyncio
import math

import pytest
from fastapi import HTTPException, UploadFile

from test_geodata import frame as frame, lanelet_file as lanelet_file
from scenario_generator.map.routing import LaneGraph
from scenario_generator.scenario_elements.road_user.trajectory import Waypoint
from scenario_generator.webapp import server


@pytest.fixture
def routed_scene(lanelet_file, frame):
    scene = server.WebScenario()
    scene.geo.frame = frame
    scene.geo.add_lanelet(lanelet_file, scene)
    scene.vehicles["vehicle_1"] = [Waypoint(0, 0, 0, 5), Waypoint(1, 5, 1, 5), Waypoint(2, 10, 0, 5)]
    scene.settings["time_step_s"] = 0.25
    scene.snapshot()
    token = server.active_scenario.set(scene)
    try:
        yield scene
    finally:
        server.active_scenario.reset(token)


def append(scene, x=70, y=0, **changes):
    payload = {"source": "lanelet2", "target": {"x_m": x, "y_m": y},
               "parameters": {"reference_speed_mps": 5, "end_speed_mps": 5},
               "map_revision": scene.geo.revision,
               "actor_revision": scene.geo.actor_revision(scene, "vehicle_1")}
    payload.update(changes)
    return server.append_actor_route("vehicle_1", payload)


def series(scene):
    return scene.actor_trajectory("vehicle_1", scene.vehicles["vehicle_1"]).as_series()


def assert_prefix(before, after):
    for key in ("time_s", "x_m", "y_m", "speed_mps", "yaw_rad"):
        assert after[key][:len(before[key])] == before[key]


def test_append_preserves_manual_curve_and_successive_sections(routed_scene):
    scene = routed_scene
    before = series(scene)
    points = deepcopy(scene.vehicles["vehicle_1"])
    result = append(scene)
    assert result["route"]["seeded"] is False
    assert scene.vehicles["vehicle_1"][:3] == points
    assert_prefix(before, series(scene))
    first = series(scene)
    append(scene, 90)
    after = series(scene)
    assert_prefix(first, after)
    assert len(scene.geo.actor_options["vehicle_1"]["generation"]["segments"]) == 2
    assert len(after["interval_modes"]) == len(after["time_s"]) - 1
    assert len(set(after["time_s"])) == len(after["time_s"])
    for i in range(len(before["time_s"]) - 1, len(after["time_s"]) - 1):
        dt = after["time_s"][i + 1] - after["time_s"][i]
        distance = math.hypot(after["x_m"][i + 1] - after["x_m"][i], after["y_m"][i + 1] - after["y_m"][i])
        assert dt <= 0.25 + 1e-8
        assert distance / dt == pytest.approx((after["speed_mps"][i] + after["speed_mps"][i + 1]) / 2)


def test_append_preserves_legacy_inferred_speeds(routed_scene):
    scene = routed_scene
    scene.vehicles["vehicle_1"][1].speed_mps = None
    scene.vehicles["vehicle_1"][2].speed_mps = None
    before = series(scene)
    append(scene, parameters={"reference_speed_mps": 30, "end_speed_mps": 5})
    assert scene.vehicles["vehicle_1"][2].speed_mps is None
    assert_prefix(before, series(scene))
    assert series(scene)["speed_mps"][len(before["speed_mps"]) - 1] == before["speed_mps"][-1]


def test_off_centre_start_is_preserved_and_transition_stays_in_lane(routed_scene):
    scene = routed_scene
    scene.vehicles["vehicle_1"] = [Waypoint(0, 5, 1, 5), Waypoint(1, 10, 1, 5)]
    before = series(scene)
    append(scene)
    assert_prefix(before, series(scene))
    assert scene.vehicles["vehicle_1"][1].y_m == 1
    generated = scene.vehicles["vehicle_1"][2:]
    assert all(-1e-6 <= p.y_m <= 1 + 1e-6 for p in generated)
    assert any(0 < p.y_m < 1 for p in generated)
    assert generated[-1].y_m == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("last_y,target,params", [(3, 70, {}), (1, 20, {}), (0, 70, {"reference_speed_mps": 1})])
def test_bad_route_does_not_mutate_actor(routed_scene, last_y, target, params):
    scene = routed_scene
    scene.vehicles["vehicle_1"][-1].y_m = last_y
    before = deepcopy(scene.vehicles["vehicle_1"])
    options = deepcopy(scene.geo.actor_options)
    with pytest.raises(HTTPException) as exc:
        append(scene, target, parameters={"reference_speed_mps": 5, "end_speed_mps": 5, **params})
    assert exc.value.status_code == 400
    assert scene.vehicles["vehicle_1"] == before
    assert scene.geo.actor_options == options


def test_stale_append_and_change_during_generation_are_rejected(routed_scene, monkeypatch):
    scene = routed_scene
    with pytest.raises(HTTPException) as exc:
        append(scene, actor_revision="stale")
    assert exc.value.status_code == 409
    original = scene.geo.append
    def changing(*args):
        result = original(*args)
        scene.vehicles["vehicle_1"][0].x_m += 1
        return result
    monkeypatch.setattr(scene.geo, "append", changing)
    with pytest.raises(HTTPException) as exc:
        append(scene)
    assert exc.value.status_code == 409
    assert len(scene.vehicles["vehicle_1"]) == 3
    assert scene.geo.actor_options == {}


def test_empty_actor_seed_and_profile_start(routed_scene):
    scene = routed_scene
    scene.vehicles["vehicle_1"] = []
    result = append(scene, 5, parameters={"start_time_s": 4, "start_speed_mps": 2})
    assert result["route"]["seeded"]
    assert scene.vehicles["vehicle_1"] == [Waypoint(4, pytest.approx(5), pytest.approx(0, abs=1e-6), 2)]
    append(scene, 50)
    assert scene.vehicles["vehicle_1"][0].time_s == 4
    assert scene.vehicles["vehicle_1"][0].speed_mps == 2
    assert series(scene)["speed_mps"][0] == 2


def test_sections_follow_insert_delete_and_manual_append(routed_scene):
    scene = routed_scene
    append(scene)
    scene.insert_waypoint("vehicle_1", 2)
    scene.actor_trajectory("vehicle_1", scene.vehicles["vehicle_1"]).validate_sections()
    scene.delete_waypoint("vehicle_1", 2)
    scene.delete_waypoint("vehicle_1", len(scene.vehicles["vehicle_1"]) - 1)
    scene.actor_trajectory("vehicle_1", scene.vehicles["vehicle_1"]).validate_sections()
    before = series(scene)
    end = scene.vehicles["vehicle_1"][-1]
    scene.add_waypoint("vehicle_1", end.x_m + 2, end.y_m)
    assert_prefix(before, series(scene))
    assert scene.geo.actor_options["vehicle_1"]["interpolation_sections"][-1]["mode"] == "smooth"
    append(scene, 90, parameters={"reference_speed_mps": 20, "end_speed_mps": scene.vehicles["vehicle_1"][-1].speed_mps})
    scene.actor_trajectory("vehicle_1", scene.vehicles["vehicle_1"]).validate_sections()


@pytest.mark.parametrize("kind", ["config", "json"])
def test_section_export_import_roundtrip(routed_scene, tmp_path, monkeypatch, kind):
    scene = routed_scene
    append(scene)
    before = series(scene)
    monkeypatch.setattr(server, "EXPORT_DIRECTORY", tmp_path)
    response = server.export_scenario(kind, {"base_name": "sections"})
    data = response.path.read_bytes()
    asyncio.run(server.import_config(UploadFile(filename=f"sections_{kind}.json", file=BytesIO(data))))
    assert_prefix(before, series(scene))
    assert len(series(scene)["time_s"]) == len(before["time_s"])


def test_two_way_lanes_require_an_explicit_direction():
    graph = LaneGraph()
    graph.add_lane("forward", [(0, 0), (100, 0)])
    graph.add_lane("backward", [(100, 0), (0, 0)])
    assert {v["lane_id"] for v in graph.candidates({"x_m": 10, "y_m": 0})} == {"forward", "backward"}
    assert abs(graph.candidates({"x_m": 10, "y_m": 0})[0]["heading_rad"] - graph.candidates({"x_m": 10, "y_m": 0})[1]["heading_rad"]) == pytest.approx(math.pi)


def test_legacy_origins_and_opacity_restore(routed_scene, tmp_path):
    scene = routed_scene
    workspace = scene.geo.persisted(scene)
    workspace["settings"].pop("show_xodr_origin")
    workspace["settings"].pop("show_lanelet_origin")
    workspace["settings"].update(show_origins=True, lanelet_opacity=0.4)
    scene.geo.restore(workspace, scene, tmp_path)
    assert scene.settings["show_xodr_origin"] and scene.settings["show_lanelet_origin"]
    assert scene.settings["lanelet_opacity"] == 0.4
    with pytest.raises(ValueError):
        scene.geo.restore({**workspace, "settings": {"lanelet_opacity": 2}}, scene, tmp_path)


def test_bidirectional_target_follows_the_selected_start_direction(routed_scene):
    scene = routed_scene
    graph = LaneGraph()
    graph.add_lane("forward", [(0, 0), (100, 0)])
    graph.add_lane("backward", [(100, 0), (0, 0)])
    scene.geo.graphs["lanelet2"] = graph
    result = append(scene)
    assert result["route"]["lane_ids"] == ["forward"]
    scene.vehicles["vehicle_1"] = [Waypoint(4, 10, 0, 5)]
    scene.geo.actor_options["vehicle_1"] = {}
    with pytest.raises(HTTPException, match="ambiguous"):
        append(scene)
    result = append(scene, lane_id="forward")
    assert result["route"]["lane_ids"] == ["forward"]


def test_xodr_append_preserves_existing_motion(routed_scene):
    from pathlib import Path
    from scenario_generator.io.scenario_files import load_xodr_reference_map
    scene = routed_scene
    path = Path(__file__).parent / "e2e/fixtures/reference.xodr"
    scene.map.load_view_only(load_xodr_reference_map(path), path)
    scene.snapshot()
    scene.vehicles["vehicle_1"] = [Waypoint(0, 0, -1.5, 5), Waypoint(1, 5, -1.5, 5), Waypoint(2, 10, -1.5, 5)]
    before = series(scene)
    append(scene, 70, -1.5, source="xodr")
    assert_prefix(before, series(scene))
    assert scene.vehicles["vehicle_1"][-1].x_m == pytest.approx(70)


def test_sampling_and_layer_preferences_roundtrip(routed_scene, tmp_path):
    scene = routed_scene
    scene.settings.update(show_map=False, show_lanelet_origin=True, lanelet_opacity=0.3)
    workspace = deepcopy(scene.geo.persisted(scene))
    scene.settings.update(time_step_s=1, show_map=True, show_lanelet_origin=False, lanelet_opacity=1)
    scene.geo.restore(workspace, scene, tmp_path)
    assert scene.settings["time_step_s"] == 0.25
    assert scene.settings["show_map"] is False
    assert scene.settings["show_lanelet_origin"] is True
    assert scene.settings["lanelet_opacity"] == 0.3


def test_invalid_sections_and_metadata_do_not_load(routed_scene, tmp_path):
    scene = routed_scene
    workspace = scene.geo.persisted(scene)
    workspace["actors"] = {"vehicle_1": {"interpolation_sections": [
        {"first_index": 0, "last_index": 1, "mode": "kinematic"}]}}
    with pytest.raises(ValueError, match="cover"):
        scene.geo.restore(workspace, scene, tmp_path)
    workspace["actors"] = {"vehicle_1": {"generation": {"segments": ["invalid"]}}}
    with pytest.raises(ValueError, match="segments"):
        scene.geo.restore(workspace, scene, tmp_path)
