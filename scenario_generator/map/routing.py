"""Directed lane routing with shared geometry for XODR and Lanelet2 sources."""

from __future__ import annotations

import heapq
import math
from bisect import bisect_right
from pathlib import Path

from defusedxml.ElementTree import parse
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import substring
from shapely.strtree import STRtree

from scenario_generator.io.scenario_files import (
    add_lane_transition_samples,
    lane_cross_sections_for_road,
    lane_sections_for_road,
    road_plan_view_samples,
    parse_lane_link_spec,
)
from scenario_generator.map.map import ScenarioMap
from scenario_generator.io.import_validation import finite_number


class LaneGraph:
    def __init__(self):
        self.lanes = {}
        self.links = {}
        self.index = None
        self.corridors = {}
        self.change_intervals = {}

    def add_lane(self, key, points, corridor=None):
        clean = [
            point
            for i, point in enumerate(points)
            if i == 0 or math.dist(point, points[i - 1]) > 1e-7
        ]
        if len(clean) >= 2:
            self.lanes[key] = LineString(clean)
            self.links[key] = []
            self.corridors[key] = (
                corridor if corridor is not None else self.lanes[key].buffer(2)
            )

    def add_link(self, source, target, change=False, intervals=None):
        if source in self.lanes and target in self.lanes:
            self.links[source].append((target, change))
            if change:
                self.change_intervals[source, target] = intervals or [
                    (0, self.lanes[source].length)
                ]

    def match(self, value):
        p = Point(
            finite_number(value["x_m"], "Route X"),
            finite_number(value["y_m"], "Route Y"),
        )
        key = value.get("lane_id")
        if key is None:
            if self.index is None:
                self.keys = list(self.lanes)
                if not self.keys:
                    raise ValueError("This map contains no routable vehicle lanes.")
                self.index = STRtree(list(self.lanes.values()))
            key = self.keys[int(self.index.nearest(p))]
        key = str(key)
        if key not in self.lanes:
            raise ValueError(f"Unknown or inaccessible lane: {key}")
        line = self.lanes[key]
        if p.distance(line) > 20:
            raise ValueError("Route points must be within 20 metres of a driving lane.")
        return key, line.project(p)

    def candidates(self, value, inside=True):
        """Return directed vehicle lanes containing a start point, with their heading."""
        p = Point(finite_number(value["x_m"], "Route X"), finite_number(value["y_m"], "Route Y"))
        self.match(value)  # Warm the same spatial index used for routing.
        candidates = []
        for index in self.index.query(p.buffer(20)) if self.index is not None else range(len(self.lanes)):
            key = self.keys[int(index)] if self.index is not None else list(self.lanes)[index]
            line = self.lanes[key]
            if line.distance(p) > 20 or (inside and not self.corridors[key].buffer(1e-6).covers(p)):
                continue
            s = line.project(p)
            before, after = line.interpolate(max(0, s - 0.2)), line.interpolate(min(line.length, s + 0.2))
            candidates.append({"lane_id": key, "heading_rad": math.atan2(after.y - before.y, after.x - before.x),
                               "distance_m": line.distance(p), "x_m": line.interpolate(s).x, "y_m": line.interpolate(s).y})
        return sorted(candidates, key=lambda item: (item["distance_m"], item["lane_id"]))

    def align_start(self, points, start, lane_id, start_heading=None):
        """Join an unchanged in-lane endpoint to the centreline over fifteen metres."""
        position = Point(start["x_m"], start["y_m"])
        corridor = self.corridors[lane_id]
        if not corridor.buffer(1e-6).covers(position):
            raise ValueError("The last waypoint is outside the selected driving lane. Move it into the lane before routing.")
        offset = (position.x - points[0][0], position.y - points[0][1])
        if math.hypot(*offset) < 1e-6:
            return [(position.x, position.y)] + points[1:]
        line = LineString(points)
        source_line = self.lanes[lane_id]
        source_s = source_line.project(position)
        if line.length < 15 or source_line.length - source_s < 15:
            raise ValueError("A 15 metre in-lane transition is needed from the last waypoint to the centreline.")
        ahead = line.interpolate(0.1)
        tangent = math.atan2(ahead.y - points[0][1], ahead.x - points[0][0])
        derivative = (0, 0) if start_heading is None else (
            math.cos(start_heading) - math.cos(tangent), math.sin(start_heading) - math.sin(tangent))
        transition = []
        for step in range(31):
            u = step / 30
            blend = u**3 * (10 + u * (-15 + 6 * u))
            # Quintic Hermite basis: retain initial lateral position and tangent;
            # position, tangent and curvature vanish at the centreline join.
            tangent_blend = u - 6 * u**3 + 8 * u**4 - 3 * u**5
            p = line.interpolate(u * 15)
            transition.append((p.x + (1 - blend) * offset[0] + 15 * tangent_blend * derivative[0],
                               p.y + (1 - blend) * offset[1] + 15 * tangent_blend * derivative[1]))
        if not corridor.buffer(1e-6).covers(LineString(transition)):
            raise ValueError("The start transition leaves the driving lane; adjust the last waypoint or choose another direction.")
        transition[0] = (position.x, position.y)
        remainder = substring(line, 15, line.length)
        return transition + (list(remainder.coords)[1:] if remainder.geom_type == "LineString" else [])

    def transition(self, source, target, progress, target_limit):
        line, other = self.lanes[source], self.lanes[target]
        for lo, hi in self.change_intervals[source, target]:
            begin = max(progress, lo)
            if hi - begin < 15:
                continue
            next_begin = other.project(line.interpolate(begin))
            if target_limit - next_begin < 15:
                continue
            points = []
            for step in range(31):
                u = step / 30
                blend = u**3 * (10 + u * (-15 + 6 * u))
                a, b = (
                    line.interpolate(begin + u * 15),
                    other.interpolate(next_begin + u * 15),
                )
                points.append((a.x + blend * (b.x - a.x), a.y + blend * (b.y - a.y)))
            if (
                self.corridors[source]
                .union(self.corridors[target])
                .buffer(0.05)
                .covers(LineString(points))
            ):
                return begin, next_begin + 15, points
        raise ValueError(
            "Insufficient parallel lane length or driving corridor for a 15 metre lane-change transition."
        )

    def shortest(self, source, target, start_s=0, end_s=None):
        end_s = self.lanes[target].length if end_s is None else end_s
        queue = [(0, source, start_s, False, [])]
        visited, rejected = set(), None
        while queue:
            cost, current, progress, complete, path = heapq.heappop(queue)
            if complete:
                return path + [(current, False)]
            state = (current, round(progress, 4))
            if state in visited:
                continue
            visited.add(state)
            if current == target and end_s >= progress:
                heapq.heappush(
                    queue, (cost + end_s - progress, current, progress, True, path)
                )
            for successor, change in self.links[current]:
                line = self.lanes[current]
                if change:
                    try:
                        begin, following_progress, transition = self.transition(
                            current,
                            successor,
                            progress,
                            end_s
                            if successor == target
                            else self.lanes[successor].length,
                        )
                    except ValueError as exc:
                        rejected = str(exc)
                        continue
                    edge_cost = begin - progress + LineString(transition).length + 20
                else:
                    gap = math.dist(line.coords[-1], self.lanes[successor].coords[0])
                    if gap > 2:
                        continue
                    edge_cost, following_progress = line.length - progress + gap, 0
                heapq.heappush(
                    queue,
                    (
                        cost + edge_cost,
                        successor,
                        following_progress,
                        False,
                        path + [(current, change)],
                    ),
                )
        raise ValueError(
            rejected
            or "No directed route connects these lanes; choose another lane or intermediate point."
        )

    def route(self, points):
        matches = [self.match(point) for point in points]
        output, lane_ids, changes = [], [], 0
        for (source, start_s), (target, end_s) in zip(matches, matches[1:]):
            path = self.shortest(source, target, start_s, end_s)
            current_start = start_s
            for index, (key, change) in enumerate(path):
                line = self.lanes[key]
                stop = end_s if index == len(path) - 1 else line.length
                lane_ids.append(key)
                if change:
                    next_key = path[index + 1][0]
                    other_stop = (
                        end_s if next_key == target else self.lanes[next_key].length
                    )
                    begin, following_progress, transition = self.transition(
                        key, next_key, current_start, other_stop
                    )
                    if begin > current_start:
                        output.extend(substring(line, current_start, begin).coords)
                    output.extend(transition)
                    current_start = following_progress
                    changes += 1
                    continue
                segment = substring(line, current_start, stop)
                if segment.geom_type == "LineString":
                    coordinates = list(segment.coords)
                    if output and math.dist(output[-1], coordinates[0]) > 2:
                        raise ValueError(
                            "Map lane connection has a geometry gap exceeding 2 metres."
                        )
                    output.extend(coordinates)
                current_start = 0
        clean = [
            p
            for i, p in enumerate(output)
            if i == 0 or math.dist(p, output[i - 1]) > 1e-7
        ]
        if len(clean) < 2:
            raise ValueError("Route has no usable length.")
        return clean, {
            "lane_ids": lane_ids,
            "lane_changes": changes,
            "matched_points": [
                {
                    "lane_id": key,
                    "x_m": self.lanes[key].interpolate(s).x,
                    "y_m": self.lanes[key].interpolate(s).y,
                }
                for key, s in matches
            ],
        }


def xodr_graph(path: Path, effective_roads=None):
    root = parse(path).getroot()
    graph = LaneGraph()
    roads = {road.get("id"): road for road in root.findall("road")}
    sections_by_road = {}
    directions = {}
    effective = (
        {road.name: road for road in effective_roads}
        if effective_roads is not None
        else None
    )
    for road_id, road in roads.items():
        edited = (
            effective.get(road.get("name") or road_id)
            if effective is not None
            else None
        )
        if effective is not None and edited is None:
            continue
        sections = lane_sections_for_road(road)
        sections_by_road[road_id] = sections
        samples = add_lane_transition_samples(road, road_plan_view_samples(road))
        for section_index, section in enumerate(sections):
            lo = float(section.get("s", 0))
            hi = (
                float(sections[section_index + 1].get("s"))
                if section_index + 1 < len(sections)
                else float(road.get("length"))
            )
            active = lane_cross_sections_for_road(
                road,
                [section],
                [sample for sample in samples if lo - 1e-8 <= sample[0] <= hi + 1e-8],
            )
            if edited and edited.lane_cross_sections:
                edited_samples = edited.lane_cross_sections
                positions = [p.s_m for p in edited_samples]
                for profile in active:
                    index = min(
                        len(positions) - 2,
                        max(0, bisect_right(positions, profile.s_m) - 1),
                    )
                    a, b = edited_samples[index : index + 2]
                    fraction = (
                        (profile.s_m - a.s_m) / (b.s_m - a.s_m) if b.s_m > a.s_m else 0
                    )
                    profile.x_m = a.x_m + fraction * (b.x_m - a.x_m)
                    profile.y_m = a.y_m + fraction * (b.y_m - a.y_m)
                    profile.heading_rad = a.heading_rad + fraction * (
                        (b.heading_rad - a.heading_rad + math.pi) % (2 * math.pi)
                        - math.pi
                    )
                    profile.lane_offset_m = a.lane_offset_m + fraction * (
                        b.lane_offset_m - a.lane_offset_m
                    )
                    for lane_id in profile.lane_widths_m:
                        if (
                            lane_id in edited.lane_widths_m
                            and lane_id in a.lane_widths_m
                        ):
                            profile.lane_widths_m[lane_id] = a.lane_widths_m[lane_id]
            for lane in section.findall("./left/lane") + section.findall(
                "./right/lane"
            ):
                lane_id = int(lane.get("id"))
                lane_type = (
                    edited.lane_types.get(lane_id, lane.get("type"))
                    if edited
                    else lane.get("type")
                )
                if (
                    str(lane_type).lower()
                    not in ScenarioMap.COMPATIBLE_LANE_TYPES["vehicle"]
                ):
                    continue
                forward = (lane_id < 0) != (road.get("rule", "RHT") == "LHT")
                if lane.get("direction") == "reversed":
                    forward = not forward
                key = f"{road_id}:{section_index}:{lane_id}"
                points, left, right = [], [], []
                for p in active:
                    width = p.lane_widths_m.get(lane_id, 0)
                    if width <= 1e-4:
                        continue
                    offset = ScenarioMap.lane_center_offset(
                        p.lane_widths_m, p.lane_offset_m, lane_id
                    )
                    points.append(
                        (
                            p.x_m - math.sin(p.heading_rad) * offset,
                            p.y_m + math.cos(p.heading_rad) * offset,
                        )
                    )
                    for bound, edge in (
                        (left, offset + width / 2),
                        (right, offset - width / 2),
                    ):
                        bound.append(
                            (
                                p.x_m - math.sin(p.heading_rad) * edge,
                                p.y_m + math.cos(p.heading_rad) * edge,
                            )
                        )
                corridor = (
                    Polygon(left + right[::-1]).buffer(0) if len(left) >= 2 else None
                )
                graph.add_lane(key, points if forward else points[::-1], corridor)
                directions[key] = forward
    for road_id, sections in sections_by_road.items():
        road = roads[road_id]
        for section_index, section in enumerate(sections):
            for lane in section.findall("./left/lane") + section.findall(
                "./right/lane"
            ):
                lane_id = int(lane.get("id"))
                key = f"{road_id}:{section_index}:{lane_id}"
                if key not in graph.lanes:
                    continue
                forward = directions[key]
                tag = "successor" if forward else "predecessor"
                lane_link = lane.find(f"link/{tag}")
                next_index = section_index + (1 if forward else -1)
                if 0 <= next_index < len(sections):
                    if lane_link is not None:
                        graph.add_link(
                            key, f"{road_id}:{next_index}:{lane_link.get('id')}"
                        )
                else:
                    link = road.find(f"link/{tag}")
                    if (
                        link is not None
                        and link.get("elementType") == "road"
                        and lane_link is not None
                    ):
                        target_id = link.get("elementId")
                        target_sections = sections_by_road.get(target_id, [])
                        target_index = (
                            0
                            if link.get("contactPoint") == "start"
                            else len(target_sections) - 1
                        )
                        target_key = f"{target_id}:{target_index}:{lane_link.get('id')}"
                        if directions.get(target_key) == (
                            link.get("contactPoint") == "start"
                        ):
                            graph.add_link(key, target_key)
                    elif link is not None and link.get("elementType") == "junction":
                        junction = root.find(f"junction[@id='{link.get('elementId')}']")
                        for connection in (
                            junction.findall("connection")
                            if junction is not None
                            else []
                        ):
                            if connection.get("incomingRoad") != road_id:
                                continue
                            target_id = connection.get("connectingRoad")
                            target_index = (
                                0
                                if connection.get("contactPoint") == "start"
                                else len(sections_by_road.get(target_id, [])) - 1
                            )
                            for pair in connection.findall("laneLink"):
                                if int(pair.get("from")) == lane_id:
                                    target_key = (
                                        f"{target_id}:{target_index}:{pair.get('to')}"
                                    )
                                    if directions.get(target_key) == (
                                        connection.get("contactPoint") == "start"
                                    ):
                                        graph.add_link(key, target_key)
                for neighbor_id in (lane_id - 1, lane_id + 1):
                    if lane_id * neighbor_id <= 0:
                        continue
                    neighbor = f"{road_id}:{section_index}:{neighbor_id}"
                    # The shared marking belongs to the lane nearer the reference line.
                    inner = min((lane_id, neighbor_id), key=abs)
                    boundary = section.find(
                        f"./{'left' if inner > 0 else 'right'}/lane[@id='{inner}']"
                    )
                    marks = boundary.findall("roadMark") if boundary is not None else []
                    marks.sort(key=lambda mark: float(mark.get("sOffset", 0)))
                    section_length = (
                        float(sections[section_index + 1].get("s"))
                        if section_index + 1 < len(sections)
                        else float(road.get("length"))
                    ) - float(section.get("s", 0))
                    intervals = []
                    for index, mark in enumerate(marks):
                        allowed = mark.get("laneChange", "both") in {
                            "both",
                            "increase" if neighbor_id > lane_id else "decrease",
                        }
                        if (
                            not allowed
                            or "solid" in mark.get("type", "")
                            or mark.get("type") not in {"broken", "none"}
                        ):
                            continue
                        begin = (
                            float(mark.get("sOffset", 0))
                            / section_length
                            * graph.lanes[key].length
                        )
                        end = (
                            (
                                float(marks[index + 1].get("sOffset", 0))
                                if index + 1 < len(marks)
                                else section_length
                            )
                            / section_length
                            * graph.lanes[key].length
                        )
                        intervals.append(
                            (begin, end)
                            if forward
                            else (
                                graph.lanes[key].length - end,
                                graph.lanes[key].length - begin,
                            )
                        )
                    if intervals and directions.get(neighbor) == forward:
                        graph.add_link(key, neighbor, True, sorted(intervals))
    if effective is not None:
        ids_by_name = {
            road.get("name") or road_id: road_id for road_id, road in roads.items()
        }
        added = [
            road
            for name, road in effective.items()
            if name not in ids_by_name and road.kind == "reference"
        ]
        created = editable_graph(added)
        for key, line in created.lanes.items():
            graph.add_lane(key, list(line.coords), created.corridors[key])

        def target_lanes(relation, tag):
            target_id = relation if relation in roads else ids_by_name.get(relation)
            if target_id is None:
                keys = [key for key in created.lanes if key.startswith(f"{relation}:")]
                return {
                    int(key.split(":")[-1]): key
                    for key in keys
                    if (int(key.split(":")[-1]) < 0) == (tag == "successor")
                }
            target_index = (
                0
                if tag == "successor"
                else len(sections_by_road.get(target_id, [])) - 1
            )
            return {
                int(key.split(":")[-1]): key
                for key in graph.lanes
                if key.startswith(f"{target_id}:{target_index}:")
                and directions.get(key) == (tag == "successor")
            }

        for edited in added:
            for tag in ("predecessor", "successor"):
                targets = target_lanes(getattr(edited, f"{tag}_road"), tag)
                sources = {
                    lane_id
                    for lane_id in edited.opendrive_lane_ids()
                    if (lane_id < 0) == (tag == "successor")
                }
                spec = getattr(edited, f"{tag}_lane_links")
                mapping = (
                    parse_lane_link_spec(spec, sources, set(targets))
                    if spec and targets
                    else {}
                )
                for lane_id in sources:
                    graph.add_link(
                        f"{edited.name}:{lane_id}",
                        targets.get(mapping.get(lane_id, lane_id)),
                    )
        for road_id, sections in sections_by_road.items():
            edited = effective[roads[road_id].get("name") or road_id]
            for tag in ("predecessor", "successor"):
                relation = getattr(edited, f"{tag}_road")
                spec = getattr(edited, f"{tag}_lane_links")
                original = roads[road_id].find(f"link/{tag}")
                original_relation = (
                    original.get("elementId", "")
                    if original is not None and original.get("elementType") == "road"
                    else ""
                )
                if relation == original_relation and not spec:
                    continue
                section_index = 0 if tag == "predecessor" else len(sections) - 1
                source_keys = [
                    key
                    for key in graph.lanes
                    if key.startswith(f"{road_id}:{section_index}:")
                    and directions[key] == (tag == "successor")
                ]
                targets = target_lanes(relation, tag)
                source_ids = {int(key.split(":")[-1]) for key in source_keys}
                mapping = (
                    parse_lane_link_spec(spec, source_ids, set(targets))
                    if spec and targets
                    else {}
                )
                for source in source_keys:
                    graph.links[source] = [
                        (target, change)
                        for target, change in graph.links[source]
                        if change
                    ]
                    lane_id = int(source.split(":")[-1])
                    graph.add_link(
                        source,
                        targets.get(mapping.get(lane_id, lane_id)),
                    )
    return graph


def editable_graph(roads):
    """Use the existing synthetic road/lane links after map-editor changes."""
    graph = LaneGraph()
    for road in roads:
        for lane_id in road.opendrive_lane_ids():
            if (
                road.lane_type_for(lane_id)
                not in ScenarioMap.COMPATIBLE_LANE_TYPES["vehicle"]
            ):
                continue
            points = []
            for index, point in enumerate(road.points):
                before, after = (
                    road.points[max(0, index - 1)],
                    road.points[min(index + 1, len(road.points) - 1)],
                )
                heading = math.atan2(after[1] - before[1], after[0] - before[0])
                offset = ScenarioMap.lane_center_offset(
                    road.lane_widths_by_id(), 0.0, lane_id
                )
                points.append(
                    (
                        point[0] - math.sin(heading) * offset,
                        point[1] + math.cos(heading) * offset,
                    )
                )
            graph.add_lane(
                f"{road.name}:{lane_id}", points if lane_id < 0 else points[::-1]
            )
    lookup = {road.name: road for road in roads}
    for road in roads:
        for lane_id in road.opendrive_lane_ids():
            relation = road.successor_road if lane_id < 0 else road.predecessor_road
            target = lookup.get(relation)
            if target:
                spec = (
                    road.successor_lane_links
                    if lane_id < 0
                    else road.predecessor_lane_links
                )
                links = parse_lane_link_spec(
                    spec,
                    set(road.opendrive_lane_ids()),
                    set(target.opendrive_lane_ids()),
                )
                graph.add_link(
                    f"{road.name}:{lane_id}",
                    f"{target.name}:{links.get(lane_id, lane_id)}",
                )
    return graph
