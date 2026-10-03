"""Session-owned geodata and routing services used by the existing web server."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import math

from scenario_generator.map.georeference import MapFrame, read_xodr_frame
from scenario_generator.map.lanelet import LaneletLayer
from scenario_generator.map.motion import generate_motion
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
                    for point in options.get("generation", {}).get("points", []):
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
                    "show_lanelet",
                    "show_origins",
                    "show_orthophoto",
                    "orthophoto_opacity",
                    "map_opacity",
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

    def preview(self, scenario, name, payload):
        if scenario.dimensions[name].actor_type != "vehicle":
            raise ValueError("Automatic routing currently supports vehicles.")
        source = payload.get("source", "lanelet2" if self.lanelet else "xodr")
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
        route, details = self.graphs[source].route(normalized)
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
        for options in self.actor_options.values():
            if options.get("interpolation_mode", "smooth") not in {
                "smooth",
                "kinematic",
            }:
                raise ValueError("Unknown trajectory interpolation mode.")
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
                "show_lanelet",
                "show_origins",
                "show_orthophoto",
                "orthophoto_opacity",
                "map_opacity",
            }:
                if key.endswith("opacity"):
                    setting = float(setting)
                    if not math.isfinite(setting) or not 0 <= setting <= 1:
                        raise ValueError("Layer opacity must be between 0 and 1.")
                elif not isinstance(setting, bool):
                    raise ValueError("Layer visibility settings must be booleans.")
                scenario.settings[key] = setting
        self.invalidate()
