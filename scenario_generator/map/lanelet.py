"""Read-only Lanelet2 geometry and native German vehicle routing."""

from __future__ import annotations

from pathlib import Path

from defusedxml.ElementTree import parse

from scenario_generator.map.georeference import MapFrame
from scenario_generator.io.import_validation import (
    MAX_TOTAL_MAP_POINTS,
    MAX_IMPORTED_ROADS,
)


class LaneletLayer:
    def __init__(self, path: Path, frame: MapFrame | None):
        import lanelet2

        root = parse(path).getroot()
        nodes = root.findall("node")
        if not nodes or len(nodes) > MAX_TOTAL_MAP_POINTS:
            raise ValueError(
                "Lanelet2 map contains no nodes or exceeds the node limit."
            )
        self.origin_lon = float(nodes[0].get("lon"))
        self.origin_lat = float(nodes[0].get("lat"))
        self.frame = frame or MapFrame.local_utm(self.origin_lon, self.origin_lat)
        self.path = path
        origin = lanelet2.io.Origin(self.origin_lat, self.origin_lon)
        self.projector = lanelet2.projection.UtmProjector(origin)
        self.native = lanelet2.io.load(str(path), self.projector)
        if len(self.native.laneletLayer) > MAX_IMPORTED_ROADS:
            raise ValueError("Lanelet2 map exceeds the lanelet limit.")
        rules = lanelet2.traffic_rules.create(
            lanelet2.traffic_rules.Locations.Germany,
            lanelet2.traffic_rules.Participants.Vehicle,
        )
        self.rules = rules
        self.graph = lanelet2.routing.RoutingGraph(
            self.native, rules, [lanelet2.routing.RoutingCostDistance(20)]
        )
        self.geometry = []
        self.centerlines = {}
        for lane in self.native.laneletLayer:
            left, right, center = [
                self.points(line)
                for line in (lane.leftBound, lane.rightBound, lane.centerline)
            ]
            self.geometry.append(
                {
                    "id": str(lane.id),
                    "left": left,
                    "right": right,
                    "points": left + right[::-1],
                    "centerline": center,
                    "subtype": dict(lane.attributes).get("subtype", "road"),
                }
            )
            self.centerlines[str(lane.id)] = center

    def points(self, line):
        points = []
        for point in line:
            gps = self.projector.reverse(point.basicPoint())
            points.append(self.frame.from_wgs84(gps.lon, gps.lat))
        return points

    def route_graph(self):
        from scenario_generator.map.routing import LaneGraph
        from shapely.geometry import Polygon

        graph = LaneGraph()
        native_lanes = {}
        corridors = {
            lane["id"]: Polygon(lane["points"]).buffer(0) for lane in self.geometry
        }
        for original in self.native.laneletLayer:
            for lane in (original, original.invert()):
                if self.rules.canPass(lane):
                    key = str(-lane.id if lane.inverted() else lane.id)
                    points = self.centerlines[str(lane.id)]
                    graph.add_lane(
                        key,
                        points[::-1] if lane.inverted() else points,
                        corridors[str(lane.id)],
                    )
                    native_lanes[key] = lane
        for key, lane in native_lanes.items():
            for target in self.graph.following(lane):
                graph.add_link(key, str(-target.id if target.inverted() else target.id))
            for target in (self.graph.left(lane), self.graph.right(lane)):
                if target is not None:
                    graph.add_link(
                        key, str(-target.id if target.inverted() else target.id), True
                    )
        return graph
