"""Session-owned geodata and routing services used by the existing web server."""

from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import math

from scenario_generator.map.georeference import MapFrame, read_xodr_frame
from scenario_generator.map.lanelet import LaneletLayer
from scenario_generator.map.motion import generate_motion
from scenario_generator.io.import_validation import finite_number, validate_imported_scenario
from scenario_generator.scenario_elements.road_user.trajectory import Waypoint
from scenario_generator.webapp.trajectory_sections import append_section, validate_options
from scenario_generator.map.routing import editable_graph, xodr_graph


class GeoWorkspace:
    def __init__(self):
        self.frame = None
        self.lanelet = None
        self.map_path = None
        self.map_revision = -1
        self.revision = 0
        self.graphs = {}
        self.previews = {}
        self.actor_options = {}
        self.warning = None
        self.missing_lanelet = None
        self.lanelet_sources = {}

    def invalidate(self):
        self.revision += 1
        self.graphs.clear()
        self.previews.clear()

    def set_frame(self, frame, scenario, transform_actors=True, transform_map=False):
        previous = self.frame
        layer = LaneletLayer(self.lanelet.path, frame) if self.lanelet else None
        if previous and frame and previous != frame:

            def project(x, y):
                return frame.from_wgs84(*previous.to_wgs84(x, y))

            def heading(x, y, angle):
                start = project(x, y)
                end = project(x + math.cos(angle), y + math.sin(angle))
                return math.atan2(end[1] - start[1], end[0] - start[0])

            if transform_actors:
                for name, waypoints in scenario.vehicles.items():
                    dimensions = scenario.dimensions[name]
                    if waypoints:
                        dimensions.parked_yaw_rad = heading(
                            waypoints[0].x_m,
                            waypoints[0].y_m,
                            dimensions.parked_yaw_rad,
                        )
                    for point in waypoints:
                        point.x_m, point.y_m = project(point.x_m, point.y_m)
                for options in self.actor_options.values():
                    generation = options.get("generation", {})
                    points = list(generation.get("points", []))
                    for segment in generation.get("segments", []):
                        points.extend(segment.get("points", []))
                    for point in points:
                        if "x_m" in point:
                            point["x_m"], point["y_m"] = project(
                                point["x_m"], point["y_m"]
                            )
            if transform_map:
                roads = {
                    id(road): road
                    for road in scenario.map.roads + scenario.map.view_roads
                }
                for road in roads.values():
                    road.points = [project(*point) for point in road.points]
                    for profile in road.lane_cross_sections:
                        profile.heading_rad = heading(
                            profile.x_m, profile.y_m, profile.heading_rad
                        )
                        profile.x_m, profile.y_m = project(profile.x_m, profile.y_m)
                scenario.map.modified = bool(roads)
                scenario.map.invalidate_elevation_index()
        self.frame, self.lanelet = frame, layer
        self.invalidate()

    def sync_map(self, scenario):
        path = scenario.map.path
        if path != self.map_path:
            frame = read_xodr_frame(path) if path and path.is_file() else None
            if path is None and self.lanelet:
                frame = self.frame
            self.set_frame(frame, scenario)
            self.map_path = path
        if scenario.map.revision != self.map_revision:
            self.map_revision = scenario.map.revision
            self.invalidate()
        self.warning = self.missing_lanelet or (
            "The XODR has no georeference. Set a projected CRS to align geographic layers."
            if scenario.map.has_any_roads() and self.frame is None
            else None
        )

    def add_lanelet(self, path, scenario):
        if scenario.map.has_any_roads() and self.frame is None:
            raise ValueError("Set a georeference for the XODR before loading Lanelet2.")
        layer = LaneletLayer(path, self.frame)
        self.lanelet = layer
        self.missing_lanelet = None
        self.frame = layer.frame
        self.invalidate()

    def lanelet_reference(self):
        if self.lanelet is None:
            return None
        return next(
            (
                name
                for name, path in self.lanelet_sources.items()
                if path == self.lanelet.path
            ),
            self.lanelet.path.name,
        )

    def payload(self):
        return {
            "revision": self.revision,
            "frame": self.frame.payload() if self.frame else None,
            "lanelet_path": self.lanelet_reference(),
            "lanelet_count": len(self.lanelet.geometry) if self.lanelet else 0,
            "lanelet_origin": {
                "lon": self.lanelet.origin_lon,
                "lat": self.lanelet.origin_lat,
                "point": self.frame.from_wgs84(
                    self.lanelet.origin_lon, self.lanelet.origin_lat
                ),
            }
            if self.lanelet and self.frame
            else None,
            "warning": self.warning,
        }

    def persisted(self, scenario, lanelet_path=None):
        return {
            "frame": asdict(self.frame) if self.frame else None,
            "lanelet_path": str(lanelet_path or self.lanelet_reference())
            if self.lanelet
            else None,
            "settings": {
                key: scenario.settings[key]
                for key in (
                    "time_step_s",
                    "show_map",
                    "show_road_connections",
                    "show_road_points",
                    "show_lane_numbers",
                    "show_road_centerlines",
                    "show_map_helpers",
                    "show_lanelet",
                    "show_origins",
                    "show_xodr_origin",
                    "show_lanelet_origin",
                    "show_orthophoto",
                    "orthophoto_opacity",
                    "map_opacity",
                    "lanelet_opacity",
                )
            },
            "actors": {
                name: options
                for name, options in self.actor_options.items()
                if name in scenario.vehicles
            },
        }

    def actor_revision(self, scenario, name):
        state = [[p.time_s, p.x_m, p.y_m, p.speed_mps] for p in scenario.vehicles[name]]
        return hashlib.sha256(
            json.dumps(
                [
                    state,
                    scenario.dimensions[name].as_dict(),
                    scenario.dimensions[name].parked_yaw_rad,
                    self.actor_options.get(name, {}),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def graph(self, scenario, source):
        if source not in self.graphs:
            if source == "lanelet2" and self.lanelet:
                self.graphs[source] = self.lanelet.route_graph()
            elif source == "xodr" and scenario.map.has_any_roads():
                self.graphs[source] = (
                    xodr_graph(
                        scenario.map.path,
                        scenario.map.display_roads if scenario.map.modified else None,
                    )
                    if scenario.map.path
                    else editable_graph(scenario.map.display_roads)
                )
            else:
                raise ValueError("Load the selected routing map first.")
        return self.graphs[source]

    @staticmethod
    def routing_settings(values):
        if not isinstance(values, dict):
            raise ValueError("Routing settings must be an object.")
        source = values.get("source", "xodr")
        if source not in {"xodr", "lanelet2"}:
            raise ValueError("Choose OpenDRIVE or Lanelet2 for routing.")
        output = {"source": source, "lane_id": str(values.get("lane_id") or "")}
        defaults = {"start_time_s": 0, "start_speed_mps": 30 / 3.6,
                    "end_speed_mps": 30 / 3.6, "reference_speed_mps": 30 / 3.6,
                    "acceleration_mps2": 2, "deceleration_mps2": 2}
        for key, default in defaults.items():
            output[key] = finite_number(values.get(key, default), key)
            if output[key] < 0 or (key in {"acceleration_mps2", "deceleration_mps2", "reference_speed_mps"} and output[key] == 0):
                raise ValueError(f"Invalid routing setting: {key}.")
        return output

    def append(self, scenario, name, payload):
        """Build and validate everything before committing a new trajectory section."""
        if scenario.dimensions[name].actor_type != "vehicle":
            raise ValueError("Automatic routing currently supports vehicles.")
        source = payload.get("source", "lanelet2" if self.lanelet else "xodr")
        graph = self.graph(scenario, source)
        settings = self.routing_settings({**payload.get("parameters", {}), "source": source,
                                          "lane_id": payload.get("lane_id", "")})
        existing = scenario.vehicles[name]
        options = deepcopy(self.actor_options.get(name, {}))
        target = dict(payload["target"])
        if not existing:
            key, s = graph.match({**target, **({"lane_id": settings["lane_id"]} if settings["lane_id"] else {})})
            point = graph.lanes[key].interpolate(s)
            points = [Waypoint(settings["start_time_s"], point.x, point.y, settings["start_speed_mps"])]
            options.pop("interpolation_sections", None)
            options["routing_settings"] = settings
            validate_imported_scenario({**scenario.vehicles, name: points}, scenario.dimensions)
            return points, options, {"seeded": True, "lane_ids": [key]}
        last = existing[-1]
        trajectory = scenario.actor_trajectory(name, existing)
        speed = last.speed_mps if last.speed_mps is not None else trajectory.resolved_waypoint_speeds()[0][-1]
        start = {"x_m": last.x_m, "y_m": last.y_m}
        if settings["lane_id"]:
            start["lane_id"] = settings["lane_id"]
        else:
            candidates = graph.candidates(start)
            if not candidates:
                raise ValueError("The last waypoint is outside a driving lane. Move it into a lane before routing.")
            previous = options.get("generation", {}).get("segments", [])
            preferred = previous[-1].get("lane_ids", [])[-1:] if previous and previous[-1].get("source") == source else []
            heading = None
            if len(existing) > 1:
                curve = trajectory.as_series()
                heading = curve["yaw_rad"][-1]
            compatible = [candidate for candidate in candidates if candidate["lane_id"] in preferred]
            if not compatible and heading is not None:
                compatible = [candidate for candidate in candidates if math.cos(candidate["heading_rad"] - heading) > 0]
            matches = compatible or candidates
            if len(existing) == 1 and len(matches) > 1 and abs(matches[0]["distance_m"] - matches[1]["distance_m"]) < 0.01:
                raise ValueError("The start direction is ambiguous. Select a start lane / direction in Routing settings.")
            start["lane_id"] = matches[0]["lane_id"]
        if target.get("lane_id"):
            route, details = graph.route([start, target])
        else:
            candidates = graph.candidates(target, inside=False)
            if not candidates:
                raise ValueError("Route targets must be within 20 metres of a driving lane.")
            choices, failure = [], None
            for candidate in candidates:
                if candidate["distance_m"] > candidates[0]["distance_m"] + 1e-4:
                    break
                try:
                    path, info = graph.route([start, {**target, "lane_id": candidate["lane_id"]}])
                    cost = sum(math.dist(a, b) for a, b in zip(path, path[1:])) + 20 * info["lane_changes"]
                    choices.append((cost, path, info))
                except ValueError as exc:
                    failure = exc
            if not choices:
                raise failure or ValueError("No directed route connects the start and target.")
            _cost, route, details = min(choices, key=lambda choice: choice[0])
        heading = trajectory.as_series()["yaw_rad"][-1] if len(existing) > 1 else None
        route = graph.align_start(route, start, start["lane_id"], heading)
        parameters = {**settings, "start_time_s": last.time_s, "start_speed_mps": speed,
                      "dt_s": payload.get("parameters", {}).get("dt_s", scenario.settings["time_step_s"])}
        generated, motion = generate_motion(route, parameters)
        points = existing + generated[1:]
        append_section(options, len(existing), len(points), "kinematic", speed)
        options["routing_settings"] = {**settings, "lane_id": ""}
        generation = options.setdefault("generation", {})
        generation.setdefault("segments", []).append({"source": source, "points": [start, target],
            "parameters": motion["parameters"], "lane_ids": details["lane_ids"],
            "first_index": len(existing) - 1, "last_index": len(points) - 1})
        validate_options(options, points)
        validate_imported_scenario({**scenario.vehicles, name: points}, scenario.dimensions)
        return points, options, {**details, **motion, "seeded": False}

    def preview(self, scenario, name, payload):
        if scenario.dimensions[name].actor_type != "vehicle":
            raise ValueError("Automatic routing currently supports vehicles.")
        source = payload.get("source", "lanelet2" if self.lanelet else "xodr")
        graph = self.graph(scenario, source)
        points = payload["points"]
        if not isinstance(points, list) or not 2 <= len(points) <= 64:
            raise ValueError("Choose start, target and at most 62 intermediate points.")
        normalized = []
        for point in points:
            value = dict(point)
            if "lat" in value:
                if self.frame is None:
                    raise ValueError("Geographic route points require a map reference.")
                value["x_m"], value["y_m"] = self.frame.from_wgs84(
                    float(value["lon"]), float(value["lat"])
                )
            normalized.append(value)
        route, details = graph.route(normalized)
        waypoints, motion = generate_motion(route, payload.get("parameters", {}))
        response = {
            **details,
            **motion,
            "points": route,
            "source": source,
            "preview_id": uuid4().hex,
            "map_revision": self.revision,
            "actor_revision": self.actor_revision(scenario, name),
        }
        self.previews[name] = (
            response,
            waypoints,
            {
                "source": source,
                "points": normalized,
                "parameters": motion["parameters"],
            },
        )
        return response

    def restore(self, value, scenario, directory):
        self.actor_options = value.get("actors", {})
        if not isinstance(self.actor_options, dict) or not all(
            isinstance(option, dict) for option in self.actor_options.values()
        ):
            raise ValueError("Actor trajectory options must be objects.")
        for name, options in self.actor_options.items():
            validate_options(options, scenario.vehicles.get(name, []))
            if "routing_settings" in options:
                options["routing_settings"] = self.routing_settings(options["routing_settings"])
        frame = value.get("frame")
        restored_frame = MapFrame.from_payload(frame) if frame else None
        self.frame = read_xodr_frame(scenario.map.path) if scenario.map.path else None
        self.set_frame(
            restored_frame, scenario, transform_actors=False, transform_map=True
        )
        self.map_path = scenario.map.path
        self.map_revision = scenario.map.revision
        self.lanelet = None
        path = value.get("lanelet_path")
        if path:
            candidate = (directory / path).resolve()
            if (
                not candidate.is_relative_to(directory.resolve())
                or candidate.suffix.lower() != ".osm"
            ):
                raise ValueError(
                    "Lanelet2 references must remain inside the import directory."
                )
            if not candidate.is_file():
                candidate = self.lanelet_sources.get(Path(path).name, candidate)
            if candidate.is_file():
                self.add_lanelet(candidate, scenario)
            else:
                self.missing_lanelet = f"Referenced Lanelet2 map is missing: {Path(path).name}. Upload it separately."
        for key, setting in value.get("settings", {}).items():
            if key in {
                "time_step_s",
                "show_map",
                "show_road_connections",
                "show_road_points",
                "show_lane_numbers",
                "show_road_centerlines",
                "show_map_helpers",
                "show_lanelet",
                "show_origins",
                "show_xodr_origin",
                "show_lanelet_origin",
                "show_orthophoto",
                "orthophoto_opacity",
                "map_opacity",
                "lanelet_opacity",
            }:
                if key == "time_step_s":
                    setting = finite_number(setting, "Sampling interval")
                    if setting <= 0:
                        raise ValueError("Sampling interval must be positive.")
                elif key.endswith("opacity"):
                    setting = float(setting)
                    if not math.isfinite(setting) or not 0 <= setting <= 1:
                        raise ValueError("Layer opacity must be between 0 and 1.")
                elif not isinstance(setting, bool):
                    raise ValueError("Layer visibility settings must be booleans.")
                scenario.settings[key] = setting
        settings = value.get("settings", {})
        if "show_origins" in settings:
            for key in ("show_xodr_origin", "show_lanelet_origin"):
                if key not in settings:
                    scenario.settings[key] = settings["show_origins"]
        self.invalidate()
