"""Keep interpolation boundaries and generation ranges aligned with waypoint edits."""

from copy import deepcopy

from scenario_generator.scenario_elements.road_user.trajectory import Trajectory


def sections_for(options, count):
    if count < 2:
        return []
    return deepcopy(options.get("interpolation_sections") or [{
        "first_index": 0, "last_index": count - 1,
        "mode": options.get("interpolation_mode", "smooth"),
    }])


def append_section(options, old_count, new_count, mode, start_speed=None):
    sections = sections_for(options, old_count)
    if new_count > max(old_count, 1):
        if mode == "smooth" and sections and sections[-1]["mode"] == "smooth":
            sections[-1]["last_index"] = new_count - 1
            options["interpolation_sections"] = sections
            return
        section = {"first_index": max(old_count - 1, 0), "last_index": new_count - 1, "mode": mode}
        if start_speed is not None:
            section["start_speed_mps"] = start_speed
        sections.append(section)
    options["interpolation_sections"] = sections


def remap_sections(options, count, index, deleting=False):
    if not options.get("interpolation_sections") and options.get("interpolation_mode", "smooth") == "smooth":
        return
    sections = sections_for(options, count)
    if not deleting and index == count:
        append_section(options, count, count + 1, "smooth")
    else:
        for section in sections:
            for key in ("first_index", "last_index"):
                value = section[key]
                section[key] = max(0, value - 1) if deleting and value >= index else (
                    value + 1 if not deleting and value >= index else value)
        sections = [section for section in sections if section["last_index"] > section["first_index"]]
        if sections:
            sections[0]["first_index"] = 0
        options["interpolation_sections"] = sections
    for generation in options.get("generation", {}).get("segments", []):
        for key in ("first_index", "last_index"):
            value = generation[key]
            generation[key] = max(0, value - 1) if deleting and value >= index else (
                value + 1 if not deleting and value >= index else value)


def validate_options(options, waypoints):
    if not isinstance(options, dict):
        raise ValueError("Actor trajectory options must be objects.")
    generation = options.get("generation", {})
    if not isinstance(generation, dict):
        raise ValueError("Route generation metadata must be an object.")
    segments = generation.get("segments", [])
    if not isinstance(segments, list) or not all(isinstance(segment, dict) for segment in segments):
        raise ValueError("Route generation segments must be objects.")
    for segment in segments:
        if (type(segment.get("first_index")) is not int or type(segment.get("last_index")) is not int
                or not isinstance(segment.get("points"), list) or not isinstance(segment.get("parameters"), dict)):
            raise ValueError("Invalid route generation segment.")
    for entry in [generation, *segments]:
        if not isinstance(entry.get("points", []), list) or not all(isinstance(point, dict) for point in entry.get("points", [])):
            raise ValueError("Generation route points must be objects.")
    mode = options.get("interpolation_mode", "smooth")
    if mode not in {"smooth", "kinematic"}:
        raise ValueError("Unknown trajectory interpolation mode.")
    sections = options.get("interpolation_sections", [])
    if not isinstance(sections, list) or not all(isinstance(section, dict) for section in sections):
        raise ValueError("Trajectory sections must be objects.")
    Trajectory(waypoints, interpolation_mode=mode, interpolation_sections=sections).validate_sections()
