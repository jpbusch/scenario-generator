"""Geographic authoring regressions with small deterministic maps."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import math
import threading
import xml.etree.ElementTree as ET
import zipfile

from fastapi import HTTPException, UploadFile
import numpy as np
import pytest
from rasterio.io import MemoryFile
from shapely.geometry import LineString

from scenario_generator.io.scenario_files import write_xodr_map, load_xodr_reference_map
from scenario_generator.map.georeference import MapFrame, read_xodr_frame
from scenario_generator.map.lanelet import LaneletLayer
from scenario_generator.map.map import MapPolyline
from scenario_generator.map.motion import ROUTE_GEOMETRY_TOLERANCE_M, generate_motion
from scenario_generator.map.orthophoto import OrthophotoCache
from scenario_generator.map.routing import LaneGraph, xodr_graph
from scenario_generator.scenario_elements.road_user.trajectory import (
    Trajectory,
    Waypoint,
)
from scenario_generator.webapp import server


@pytest.fixture
def frame():
    return MapFrame.local_utm(6.04648796549892, 50.785562924295)


@pytest.fixture
def lanelet_file(tmp_path, frame):
    root = ET.Element("osm", version="0.6")
    # Two consecutive eastbound lanelets, and a parallel lane permitting lane changes.
    lines = [
        [(0, 2), (50, 2), (100, 2)],
        [(0, -2), (50, -2), (100, -2)],
        [(0, -6), (50, -6), (100, -6)],
    ]
    for line_index, points in enumerate(lines):
        for point_index, (x, y) in enumerate(points):
            lon, lat = frame.to_wgs84(x, y)
            ET.SubElement(
                root,
                "node",
                id=str(1 + line_index * 3 + point_index),
                lat=str(lat),
                lon=str(lon),
                version="1",
            )
        for segment in range(2):
            way_id = 100 + line_index * 2 + segment
            way = ET.SubElement(root, "way", id=str(way_id), version="1")
            for point_index in (segment, segment + 1):
                ET.SubElement(way, "nd", ref=str(1 + line_index * 3 + point_index))
            ET.SubElement(way, "tag", k="type", v="line_thin")
            ET.SubElement(
                way, "tag", k="subtype", v="dashed" if line_index == 1 else "solid"
            )
    for lane_id, left, right in [
        (1000, 100, 102),
        (1001, 101, 103),
        (1002, 102, 104),
        (1003, 103, 105),
    ]:
        relation = ET.SubElement(root, "relation", id=str(lane_id), version="1")
        ET.SubElement(relation, "member", type="way", ref=str(left), role="left")
        ET.SubElement(relation, "member", type="way", ref=str(right), role="right")
        for key, value in {
            "type": "lanelet",
            "subtype": "road",
            "location": "urban",
            "one_way": "yes",
        }.items():
            ET.SubElement(relation, "tag", k=key, v=value)
    path = tmp_path / "lanes.osm"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_projection_offset_rotation_roundtrip(tmp_path):
    frame = MapFrame("EPSG:25832", 300000, 5620000, 0.37, z=7.5)
    for point in [(0, 0), (12, -45), (-500, 800)]:
        assert frame.from_wgs84(*frame.to_wgs84(*point)) == pytest.approx(
            point, abs=1e-6
        )
    path = tmp_path / "map.xodr"
    write_xodr_map(path, [MapPolyline("road", [(0, 0), (20, 0)])], frame=frame)
    assert read_xodr_frame(path) == frame


def test_frame_change_preserves_actor_world_position(frame):
    scenario = server.WebScenario()
    scenario.geo.frame = frame
    point = scenario.vehicles["vehicle_1"][1]
    world = frame.to_wgs84(point.x_m, point.y_m)
    replacement = MapFrame(frame.crs, frame.x + 100, frame.y + 20, 0.2)
    scenario.geo.set_frame(replacement, scenario)
    assert replacement.to_wgs84(point.x_m, point.y_m) == pytest.approx(world)


def test_frame_change_preserves_layers_and_parked_heading(
    tmp_path, frame, lanelet_file
):
    scenario = server.WebScenario()
    path = tmp_path / "map.xodr"
    write_xodr_map(path, [MapPolyline("road", [(0, 0), (100, 0)])], frame=frame)
    scenario.map.load_view_only(load_xodr_reference_map(path), path)
    scenario.snapshot()
    scenario.geo.add_lanelet(lanelet_file, scenario)
    road = scenario.map.display_roads[0]
    world = frame.to_wgs84(*road.points[0])
    actor_point = scenario.vehicles["vehicle_1"][0]
    yaw = scenario.dimensions["vehicle_1"].parked_yaw_rad
    direction = frame.to_wgs84(
        actor_point.x_m + math.cos(yaw), actor_point.y_m + math.sin(yaw)
    )
    replacement = MapFrame(frame.crs, frame.x + 100, frame.y + 20, 0.2)
    scenario.geo.set_frame(replacement, scenario, transform_map=True)
    assert replacement.to_wgs84(*road.points[0]) == pytest.approx(world)
    assert scenario.map.modified
    assert scenario.geo.lanelet.geometry[0]["centerline"]
    new_yaw = scenario.dimensions["vehicle_1"].parked_yaw_rad
    assert replacement.to_wgs84(
        actor_point.x_m + math.cos(new_yaw), actor_point.y_m + math.sin(new_yaw)
    ) == pytest.approx(direction)
    scenario.map.enable_editing()
    assert scenario.map.modified
    restored = server.WebScenario()
    restored.map.load_view_only(load_xodr_reference_map(path), path)
    # Standalone config can still reference the original map's previous frame.
    restored.geo.restore(scenario.geo.persisted(scenario), restored, tmp_path)
    assert restored.map.display_roads[0].points[0] == pytest.approx(road.points[0])


def test_supplied_georeference_export_retains_source_topology(tmp_path, frame):
    scenario = server.WebScenario()
    path = tmp_path / "local.xodr"
    write_xodr_map(path, [MapPolyline("road", [(0, 0), (100, 0)])])
    root = ET.parse(path).getroot()
    ET.SubElement(root, "junction", id="5", name="retained")
    ET.ElementTree(root).write(path)
    scenario.map.load_view_only(load_xodr_reference_map(path), path)
    scenario.snapshot()
    token = server.active_scenario.set(scenario)
    try:
        server.set_georeference(frame.payload())
        output = tmp_path / "export.xodr"
        server.write_current_map_export(output)
        assert read_xodr_frame(output) == frame
        assert ET.parse(output).getroot().find("junction").get("id") == "5"
    finally:
        server.active_scenario.reset(token)


@pytest.mark.parametrize(
    "start,end,cruise,length",
    [(0, 0, 10, 100), (5, 8, 12, 200), (10, 10, 10, 100), (0, 0, 20, 10)],
)
def test_motion_speed_distance_consistency(start, end, cruise, length):
    points, summary = generate_motion(
        [(0, 0), (length, 0)],
        {
            "start_speed_mps": start,
            "end_speed_mps": end,
            "reference_speed_mps": cruise,
            "start_time_s": 3,
        },
    )
    assert points[0].time_s == 3
    assert points[-1].time_s == pytest.approx(3 + summary["duration_s"])
    assert points[0].speed_mps == start and points[-1].speed_mps == end
    assert points[-1].x_m == length
    for a, b in zip(points, points[1:]):
        assert b.time_s > a.time_s
        assert b.time_s - a.time_s <= 0.100001
        assert b.x_m - a.x_m == pytest.approx(
            (a.speed_mps + b.speed_mps) * (b.time_s - a.time_s) / 2, abs=1e-7
        )
    assert Trajectory(points, interpolation_mode="kinematic").as_series()["x_m"] == [
        p.x_m for p in points
    ]


def test_motion_includes_geometry_and_profile_events():
    points, _ = generate_motion(
        [(0, 0), (13, 0), (13, 47)], {"start_speed_mps": 0, "end_speed_mps": 0}
    )
    assert any(p.x_m == pytest.approx(13) and p.y_m == pytest.approx(0) for p in points)
    for a, b in zip(points, points[1:]):
        assert math.dist((a.x_m, a.y_m), (b.x_m, b.y_m)) == pytest.approx(
            (a.speed_mps + b.speed_mps) * (b.time_s - a.time_s) / 2, abs=1e-7
        )


def test_motion_drops_redundant_straight_vertices_without_changing_motion():
    parameters = {"start_time_s": 3, "start_speed_mps": 10,
                  "end_speed_mps": 10, "reference_speed_mps": 10, "dt_s": 1}
    dense, dense_summary = generate_motion([(i / 10, 0) for i in range(1026)], parameters)
    sparse, sparse_summary = generate_motion([(0, 0), (102.5, 0)], parameters)
    assert dense == sparse
    assert dense_summary == sparse_summary
    assert [p.time_s for p in dense] == [*range(3, 14), 13.25]


def test_motion_reduces_dense_curves_with_bounded_geometry_error():
    route = [(50 * math.cos(i * math.pi / 2000),
              50 * math.sin(i * math.pi / 2000)) for i in range(1001)]
    points, summary = generate_motion(route, {"start_speed_mps": 10,
        "end_speed_mps": 10, "reference_speed_mps": 10, "dt_s": 1})
    assert len(points) < len(route) / 10
    assert (points[0].x_m, points[0].y_m) == route[0]
    assert (points[-1].x_m, points[-1].y_m) == route[-1]
    sampled = LineString([(p.x_m, p.y_m) for p in points])
    original = LineString(route)
    assert original.hausdorff_distance(sampled) <= ROUTE_GEOMETRY_TOLERANCE_M
    assert summary["length_m"] == pytest.approx(sampled.length)
    assert points[-1].time_s == pytest.approx(sampled.length / 10)
    for a, b in zip(points, points[1:]):
        assert 0 < b.time_s - a.time_s <= 1 + 1e-8
        assert math.dist((a.x_m, a.y_m), (b.x_m, b.y_m)) == pytest.approx(
            (a.speed_mps + b.speed_mps) * (b.time_s - a.time_s) / 2, abs=1e-7)


def test_motion_keeps_exact_acceleration_braking_events_after_reduction():
    parameters = {"start_time_s": 3, "start_speed_mps": 0, "end_speed_mps": 0,
                  "reference_speed_mps": 7, "acceleration_mps2": 2,
                  "deceleration_mps2": 2, "dt_s": 1}
    dense, summary = generate_motion([(i / 10, 0) for i in range(601)], parameters)
    sparse, _ = generate_motion([(0, 0), (60, 0)], parameters)
    assert dense == sparse
    assert any(p.time_s == pytest.approx(6.5) and p.speed_mps == 7 for p in dense)
    assert any(p.time_s == pytest.approx(3 + summary["duration_s"] - 3.5)
               and p.speed_mps == 7 for p in dense)
    assert dense[-1] == Waypoint(3 + summary["duration_s"], 60, 0, 0)


def test_motion_reduction_preserves_turns_and_backtracking():
    route = [(0, 0), (5, 0), (10, 0), (10, 5), (10, 10), (10, 5), (10, 0), (0, 0)]
    points, summary = generate_motion(route, {"start_speed_mps": 5,
        "end_speed_mps": 5, "reference_speed_mps": 5, "dt_s": 1})
    assert summary["length_m"] == 40
    assert points[-1].time_s == 8
    assert [(p.x_m, p.y_m) for p in points] == route[:-1] + [(5, 0), (0, 0)]


def test_motion_limit_counts_retained_samples(monkeypatch):
    monkeypatch.setattr("scenario_generator.map.motion.MAX_WAYPOINTS_PER_ACTOR", 11)
    parameters = {"start_speed_mps": 10, "end_speed_mps": 10,
                  "reference_speed_mps": 10, "dt_s": 1}
    points, _ = generate_motion([(i / 10, 0) for i in range(1001)], parameters)
    assert len(points) == 11
    with pytest.raises(ValueError, match="waypoint limit"):
        generate_motion([(0, 0), (0, 5), (95, 5)], parameters)


def test_kinematic_interpolation_between_samples_and_mutable_legacy_points():
    motion = Trajectory(
        [Waypoint(0, 0, 0, 0), Waypoint(1, 1, 0, 2)], interpolation_mode="kinematic"
    )
    assert motion.interpolate(0.5)[0] == pytest.approx(0.25)
    legacy = Trajectory([Waypoint(0, 0, 0, 2), Waypoint(1, 2, 0, 2)])
    legacy.as_series()
    legacy.waypoints.append(Waypoint(2, 4, 0, 2))
    assert legacy.as_series()["time_s"][-1] == 2
    legacy.waypoints = [Waypoint(0, 10, 0, 2), Waypoint(1, 12, 0, 2)]
    assert legacy.as_series()["x_m"][0] == 10


@pytest.mark.parametrize(
    "parameters",
    [
        {"dt_s": 0},
        {"start_speed_mps": -1},
        {"end_speed_mps": float("nan")},
        {"start_speed_mps": 15, "end_speed_mps": 0, "reference_speed_mps": 20},
    ],
)
def test_motion_rejects_invalid_or_unreachable_profile(parameters):
    with pytest.raises(ValueError):
        generate_motion([(0, 0), (2, 0)], parameters)


def test_directed_route_via_and_lane_change():
    graph = LaneGraph()
    graph.add_lane("a", [(0, 0), (50, 0)])
    graph.add_lane("b", [(0, -4), (50, -4)])
    graph.add_lane("c", [(50, -4), (100, -4)])
    graph.add_link("a", "b", True)
    graph.add_link("b", "c")
    points, summary = graph.route(
        [{"x_m": 2, "y_m": 0, "lane_id": "a"}, {"x_m": 90, "y_m": -4, "lane_id": "c"}]
    )
    assert points[0] == (2, 0) and points[-1] == (90, -4)
    assert summary["lane_changes"] == 1
    assert all(-4 <= y <= 0 for _, y in points)
    via, via_summary = graph.route(
        [
            {"x_m": 2, "y_m": 0, "lane_id": "a"},
            {"x_m": 40, "y_m": -4, "lane_id": "b"},
            {"x_m": 90, "y_m": -4, "lane_id": "c"},
        ]
    )
    assert (40, -4) in via and via_summary["lane_changes"] == 1
    with pytest.raises(ValueError, match="No directed route"):
        graph.route([{"x_m": 90, "y_m": -4}, {"x_m": 2, "y_m": 0}])
    with pytest.raises(ValueError, match="Insufficient"):
        graph.route(
            [
                {"x_m": 45, "y_m": 0, "lane_id": "a"},
                {"x_m": 49, "y_m": -4, "lane_id": "b"},
            ]
        )


def test_xodr_road_links_and_direction(tmp_path):
    path = tmp_path / "connected.xodr"
    write_xodr_map(
        path,
        [
            MapPolyline("one", [(0, 0), (40, 0)], successor_road="two"),
            MapPolyline("two", [(40, 0), (80, 0)], predecessor_road="one"),
        ],
    )
    graph = xodr_graph(path)
    points, summary = graph.route([{"x_m": 2, "y_m": -1.5}, {"x_m": 70, "y_m": -1.5}])
    assert summary["lane_ids"] == ["1:0:-1", "2:0:-1"]
    assert points[0] == pytest.approx((2, -1.5))
    assert points[-1] == pytest.approx((70, -1.5))
    root = ET.parse(path).getroot()
    for lane in root.findall("road/lanes/laneSection/right/lane"):
        lane.set("type", "onRamp")
    ET.ElementTree(root).write(path)
    assert "1:0:-1" in xodr_graph(path).lanes


def test_imported_routing_includes_added_editor_roads(tmp_path):
    path = tmp_path / "original.xodr"
    write_xodr_map(path, [MapPolyline("original", [(0, 0), (40, 0)])])
    roads = load_xodr_reference_map(path)
    original = next(road for road in roads if road.kind == "reference")
    original.successor_road = "extension"
    roads.append(
        MapPolyline("extension", [(40, 0), (90, 0)], predecessor_road="original")
    )
    graph = xodr_graph(path, roads)
    _, details = graph.route([{"x_m": 5, "y_m": -1.5}, {"x_m": 85, "y_m": -1.5}])
    assert details["lane_ids"] == ["1:0:-1", "extension:-1"]
    _, reverse = graph.route([{"x_m": 85, "y_m": 1.5}, {"x_m": 5, "y_m": 1.5}])
    assert reverse["lane_ids"] == ["extension:1", "1:0:1"]


def test_xodr_junction_and_changed_lane_ids(tmp_path):
    path = tmp_path / "junction.xodr"
    write_xodr_map(
        path,
        [
            MapPolyline("incoming", [(0, 0), (40, 0)]),
            MapPolyline("connecting", [(40, 0), (80, 0)]),
        ],
    )
    root = ET.parse(path).getroot()
    incoming, connecting = root.findall("road")
    link = ET.SubElement(incoming, "link")
    ET.SubElement(link, "successor", elementType="junction", elementId="99")
    junction = ET.SubElement(root, "junction", id="99", name="test")
    connection = ET.SubElement(
        junction,
        "connection",
        id="0",
        incomingRoad="1",
        connectingRoad="2",
        contactPoint="start",
    )
    ET.SubElement(connection, "laneLink", **{"from": "-1", "to": "-2"})
    connecting.find("lanes/laneSection/right/lane").set("id", "-2")
    # A zero-width inner lane places the renamed lane at the same centreline.
    inner = ET.SubElement(
        connecting.find("lanes/laneSection/right"), "lane", id="-1", type="none"
    )
    ET.SubElement(inner, "width", sOffset="0", a="0", b="0", c="0", d="0")
    ET.ElementTree(root).write(path)
    graph = xodr_graph(path)
    _, details = graph.route([{"x_m": 5, "y_m": -1.5}, {"x_m": 75, "y_m": -1.5}])
    assert details["lane_ids"] == ["1:0:-1", "2:0:-2"]


def test_xodr_contact_point_end_and_lane_section_transition(tmp_path):
    path = tmp_path / "contact.xodr"
    write_xodr_map(
        path,
        [
            MapPolyline("first", [(0, 0), (40, 0)], successor_road="second"),
            MapPolyline("second", [(80, 0), (40, 0)]),
        ],
    )
    root = ET.parse(path).getroot()
    root.find("road/link/successor").set("contactPoint", "end")
    root.find("road/lanes/laneSection/right/lane/link/successor").set("id", "1")
    ET.ElementTree(root).write(path)
    graph = xodr_graph(path)
    _, details = graph.route(
        [
            {"x_m": 5, "y_m": -1.5, "lane_id": "1:0:-1"},
            {"x_m": 75, "y_m": -1.5, "lane_id": "2:0:1"},
        ]
    )
    assert details["lane_ids"] == ["1:0:-1", "2:0:1"]

    section_path = tmp_path / "sections.xodr"
    write_xodr_map(section_path, [MapPolyline("road", [(0, 0), (80, 0)])])
    root = ET.parse(section_path).getroot()
    lanes = root.find("road/lanes")
    first = lanes.find("laneSection/right/lane")
    ET.SubElement(ET.SubElement(first, "link"), "successor", id="-2")
    section = ET.SubElement(lanes, "laneSection", s="40")
    right = ET.SubElement(section, "right")
    lane = ET.SubElement(right, "lane", id="-2", type="driving")
    ET.SubElement(lane, "width", sOffset="0", a="3", b="0", c="0", d="0")
    ET.SubElement(ET.SubElement(lane, "link"), "predecessor", id="-1")
    ET.ElementTree(root).write(section_path)
    graph = xodr_graph(section_path)
    _, details = graph.route([{"x_m": 5, "y_m": -1.5}, {"x_m": 75, "y_m": -1.5}])
    assert details["lane_ids"] == ["1:0:-1", "1:1:-2"]


@pytest.mark.parametrize("marking,allowed", [("broken", True), ("solid", False)])
def test_xodr_lane_change_permissions(tmp_path, marking, allowed):
    path = tmp_path / "parallel.xodr"
    road = MapPolyline(
        "parallel",
        [(0, 0), (100, 0)],
        lane_count=4,
        lane_widths_m={-1: 3, -2: 3},
        lane_types={-1: "driving", -2: "driving"},
    )
    write_xodr_map(path, [road])
    root = ET.parse(path).getroot()
    lane = root.find("road/lanes/laneSection/right/lane[@id='-1']")
    ET.SubElement(lane, "roadMark", sOffset="0", type=marking, laneChange="both")
    ET.ElementTree(root).write(path)
    graph = xodr_graph(path)
    points = [
        {"x_m": 5, "y_m": -1.5, "lane_id": "1:0:-1"},
        {"x_m": 90, "y_m": -4.5, "lane_id": "1:0:-2"},
    ]
    if allowed:
        _, details = graph.route(points)
        assert details["lane_changes"] == 1
    else:
        with pytest.raises(ValueError, match="No directed route"):
            graph.route(points)


def test_map_changes_invalidate_geometry_and_routing(tmp_path):
    path = tmp_path / "map.xodr"
    write_xodr_map(path, [MapPolyline("road", [(0, 0), (40, 0)])])
    scenario = server.WebScenario()
    scenario.map.load_view_only(load_xodr_reference_map(path), path)
    before = scenario.snapshot()["geodata"]["revision"]
    scenario.geo.graphs["xodr"] = object()
    scenario.map.enable_editing()
    scenario.map.roads[0].points[1] = (50, 0)
    scenario.map.invalidate_elevation_index()
    snapshot = scenario.snapshot()
    assert snapshot["geodata"]["revision"] > before
    assert not scenario.geo.graphs
    assert snapshot["map"]["roads"][0]["points"][-1] == (50, 0)


def test_lanelet_native_routing_and_alignment(lanelet_file, frame):
    layer = LaneletLayer(lanelet_file, frame)
    assert len(layer.geometry) == 4
    assert next(lane for lane in layer.geometry if lane["id"] == "1000")["centerline"][
        0
    ][0] == pytest.approx(0, abs=0.01)
    graph = layer.route_graph()
    points, summary = graph.route(
        [
            {"x_m": 5, "y_m": 0, "lane_id": "1000"},
            {"x_m": 90, "y_m": 0, "lane_id": "1001"},
        ]
    )
    assert summary["lane_ids"] == ["1000", "1001"]
    assert points[-1] == pytest.approx((90, 0), abs=0.01)


def test_standalone_project_reuses_uploaded_lanelet_filename(
    tmp_path, lanelet_file, frame
):
    scenario = server.WebScenario()
    stored = tmp_path / "first-session_lanes.osm"
    stored.write_bytes(lanelet_file.read_bytes())
    scenario.geo.add_lanelet(stored, scenario)
    scenario.geo.lanelet_sources["original.osm"] = stored
    workspace = scenario.geo.persisted(scenario)
    assert workspace["lanelet_path"] == "original.osm"
    restored = server.WebScenario()
    uploaded_again = tmp_path / "second-session_lanes.osm"
    uploaded_again.write_bytes(lanelet_file.read_bytes())
    restored.geo.lanelet_sources["original.osm"] = uploaded_again
    restored.geo.restore(workspace, restored, tmp_path)
    assert restored.geo.lanelet.path == uploaded_again
    assert restored.geo.payload()["lanelet_path"] == "original.osm"


def test_actor_cache_and_compact_snapshot(tmp_path, monkeypatch):
    scenario = server.WebScenario()
    scenario.snapshot()
    previous = scenario.actor_snapshot("vehicle_2", scenario.vehicles["vehicle_2"])
    token = server.compact_snapshot.set(True)
    try:
        monkeypatch.setattr(
            scenario,
            "map_snapshot",
            lambda: pytest.fail("Compact state must not build map geometry"),
        )
        scenario.set_waypoint_geometry("vehicle_1", 1, 15, 1)
        snapshot = scenario.snapshot()
        assert "roads" not in snapshot["map"]
        assert (
            scenario.actor_snapshot("vehicle_2", scenario.vehicles["vehicle_2"])
            is previous
        )
    finally:
        server.compact_snapshot.reset(token)


def test_route_revision_checks_and_bundle_roundtrip(
    tmp_path, monkeypatch, lanelet_file, frame
):
    scenario = server.WebScenario()
    scenario.geo.add_lanelet(lanelet_file, scenario)
    monkeypatch.setattr(server, "EXPORT_DIRECTORY", tmp_path)
    token = server.active_scenario.set(scenario)
    try:
        payload = {
            "source": "lanelet2",
            "points": [
                {"x_m": 5, "y_m": 0, "lane_id": "1000"},
                {"x_m": 90, "y_m": 0, "lane_id": "1001"},
            ],
            "parameters": {"start_speed_mps": 0, "end_speed_mps": 0},
        }
        preview = server.preview_actor_route("vehicle_1", payload)
        scenario.vehicles["vehicle_1"][0].x_m += 1
        with pytest.raises(HTTPException) as exc:
            server.apply_actor_route("vehicle_1", preview)
        assert exc.value.status_code == 409
        preview = server.preview_actor_route("vehicle_1", payload)
        scenario.geo.actor_options["vehicle_1"] = {"interpolation_mode": "kinematic"}
        with pytest.raises(HTTPException) as changed_mode:
            server.apply_actor_route("vehicle_1", preview)
        assert changed_mode.value.status_code == 409
        preview = server.preview_actor_route("vehicle_1", payload)
        old_preview = preview
        preview = server.preview_actor_route("vehicle_1", payload)
        with pytest.raises(HTTPException) as replaced:
            server.apply_actor_route("vehicle_1", old_preview)
        assert replaced.value.status_code == 409
        server.apply_actor_route("vehicle_1", preview)
        before = scenario.trajectory_payload()["vehicle_1"]
        response = server.export_scenario_bundle(
            {"formats": ["json", "xosc"], "base_name": "geo"}
        )
        with zipfile.ZipFile(response.path) as bundle:
            assert "geo.osm" in bundle.namelist()
            bundle.extractall(tmp_path / "roundtrip")
        config = json.loads((tmp_path / "roundtrip/geo_config.json").read_text())
        assert config["version"] == 5
        assert config["workspace"]["lanelet_path"] == "geo.osm"
        # Upload dependencies into the same isolated import directory before the config.
        import_directory = tmp_path / "imports"
        import_directory.mkdir(exist_ok=True)
        (import_directory / "geo.osm").write_bytes(lanelet_file.read_bytes())
        upload = UploadFile(
            filename="geo_config.json", file=BytesIO(json.dumps(config).encode())
        )
        asyncio.run(server.import_config(upload))
        after = scenario.trajectory_payload()["vehicle_1"]
        assert after["time_s"] == before["time_s"]
        assert after["speed_mps"] == before["speed_mps"]
        assert after["x_m"] == before["x_m"]
    finally:
        server.active_scenario.reset(token)


def test_orthophoto_cache_coalesces_and_survives_restart(tmp_path, frame, monkeypatch):
    cache = OrthophotoCache(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return b"test-image"

    monkeypatch.setattr(cache, "fetch", fetch)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.tile, frame, 0, 0, 0)
        assert entered.wait(5)
        second = pool.submit(cache.tile, frame, 0, 0, 0)
        release.set()
        assert first.result() == second.result() == b"test-image"
    assert len(calls) == 1
    restarted = OrthophotoCache(tmp_path)
    monkeypatch.setattr(
        restarted, "fetch", lambda *args: pytest.fail("Persistent cache miss")
    )
    assert restarted.tile(frame, 0, 0, 0) == b"test-image"


def test_orthophoto_reprojects_mock_wms(frame, monkeypatch):
    with MemoryFile() as source:
        with source.open(
            driver="PNG", width=512, height=512, count=3, dtype="uint8"
        ) as dataset:
            dataset.write(np.full((3, 512, 512), 120, dtype=np.uint8))
        png = source.read()

    class Response(BytesIO):
        pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(png))
    data = OrthophotoCache.fetch(frame, 0, 0, 0)
    with MemoryFile(data) as result, result.open() as image:
        assert image.count == 4 and image.width == 512
        assert image.read(1)[256, 256] == 120
        assert image.read(4)[256, 256] == 255


def test_orthophoto_failures_do_not_poison_cache(tmp_path, frame, monkeypatch):
    cache = OrthophotoCache(tmp_path)
    monkeypatch.setattr(
        cache, "fetch", lambda *args: (_ for _ in ()).throw(TimeoutError("WMS timeout"))
    )
    with pytest.raises(TimeoutError):
        cache.tile(frame, 0, 0, 0)
    assert not cache.pending and not list(tmp_path.glob("*.png"))
    monkeypatch.setattr(cache, "fetch", lambda *args: b"recovered")
    assert cache.tile(frame, 0, 0, 0) == b"recovered"


def test_orthophoto_cache_is_bounded(tmp_path, frame, monkeypatch):
    cache = OrthophotoCache(tmp_path, max_bytes=15)
    monkeypatch.setattr(cache, "fetch", lambda *args: b"0123456789")
    cache.tile(frame, 0, 0, 0)
    cache.tile(frame, 0, 1, 0)
    assert sum(path.stat().st_size for path in tmp_path.glob("*.png")) <= 15


def test_invalid_workspace_import_preserves_current_project(tmp_path, monkeypatch):
    scenario = server.WebScenario()
    monkeypatch.setattr(server, "EXPORT_DIRECTORY", tmp_path)
    token = server.active_scenario.set(scenario)
    try:
        config = {
            "vehicles": [
                {
                    "name": "replacement",
                    "waypoints": [
                        {"time_s": 0, "x_m": 0, "y_m": 0},
                        {"time_s": 1, "x_m": 1, "y_m": 0},
                    ],
                }
            ],
            "workspace": {"frame": {"crs": "invalid projection"}},
        }
        with pytest.raises(HTTPException):
            asyncio.run(
                server.import_config(
                    UploadFile(
                        filename="invalid.json",
                        file=BytesIO(json.dumps(config).encode()),
                    )
                )
            )
        assert set(scenario.vehicles) == {"vehicle_1", "vehicle_2"}
    finally:
        server.active_scenario.reset(token)
