"""Analytical acceleration/cruise/braking profiles along a metric route."""

from __future__ import annotations

from bisect import bisect_right
import math

from shapely.geometry import LineString

from scenario_generator.io.import_validation import (
    MAX_WAYPOINTS_PER_ACTOR,
    finite_number,
)
from scenario_generator.scenario_elements.road_user.trajectory import Waypoint


# Remove redundant map vertices without visibly changing the routed path.
ROUTE_GEOMETRY_TOLERANCE_M = 0.01


def cumulative_lengths(points):
    lengths = [0.0]
    for start, end in zip(points, points[1:]):
        lengths.append(lengths[-1] + math.dist(start, end))
    return lengths


def point_at(points, lengths, distance):
    index = min(len(points) - 2, max(0, bisect_right(lengths, distance) - 1))
    span = lengths[index + 1] - lengths[index]
    fraction = min(1.0, max(0.0, (distance - lengths[index]) / span)) if span else 0
    return tuple(
        points[index][axis] + fraction * (points[index + 1][axis] - points[index][axis])
        for axis in (0, 1)
    )


def generate_motion(points, parameters):
    values = {
        key: finite_number(parameters.get(key, default), key)
        for key, default in {
            "start_time_s": 0,
            "start_speed_mps": 30 / 3.6,
            "end_speed_mps": 30 / 3.6,
            "reference_speed_mps": 30 / 3.6,
            "acceleration_mps2": 2,
            "deceleration_mps2": 2,
            "dt_s": 0.1,
        }.items()
    }
    t0, v0, ve, vr, a, b, dt = values.values()
    if min(t0, v0, ve) < 0 or min(vr, a, b, dt) <= 0:
        raise ValueError(
            "Times/speeds must be nonnegative; cruise speed, acceleration, braking and sampling interval must be positive."
        )
    if vr < max(v0, ve):
        raise ValueError("Reference speed must be at least the start and end speed.")
    if len(points) > 2:
        points = list(
            LineString(points).simplify(ROUTE_GEOMETRY_TOLERANCE_M).coords
        )
    lengths = cumulative_lengths(points)
    distance = lengths[-1]
    if distance <= 1e-6:
        raise ValueError("Start and target must define a nonzero route.")
    peak = min(vr, math.sqrt((2 * a * b * distance + b * v0**2 + a * ve**2) / (a + b)))
    if peak < max(v0, ve) - 1e-8:
        raise ValueError("The route is too short to reach the requested end speed.")
    da, db = max(0, (peak**2 - v0**2) / (2 * a)), max(0, (peak**2 - ve**2) / (2 * b))
    dc = max(0, distance - da - db)
    ta, tc, tb = (peak - v0) / a, dc / peak, (peak - ve) / b
    duration = ta + tc + tb
    if math.ceil(duration / dt) + 1 > MAX_WAYPOINTS_PER_ACTOR:
        raise ValueError(
            "Sampling would exceed the waypoint limit; increase the sampling interval."
        )

    def time_at(s):
        if s <= da and ta > 0:
            return (math.sqrt(v0**2 + 2 * a * s) - v0) / a
        if s <= da + dc:
            return ta + (s - da) / peak
        return ta + tc + (peak - math.sqrt(max(0, peak**2 - 2 * b * (s - da - dc)))) / b

    times = sorted(
        {
            0.0,
            duration,
            ta,
            ta + tc,
            *(index * dt for index in range(1, math.ceil(duration / dt))),
            *(time_at(s) for s in lengths[1:-1]),
        }
    )
    waypoints = []
    for t in times:
        if waypoints and t + t0 - waypoints[-1].time_s < 1e-8:
            continue
        if t <= ta:
            s, speed = v0 * t + a * t**2 / 2, v0 + a * t
        elif t <= ta + tc:
            s, speed = da + peak * (t - ta), peak
        else:
            u = t - ta - tc
            s, speed = da + dc + peak * u - b * u**2 / 2, peak - b * u
        x, y = point_at(points, lengths, min(distance, s))
        waypoints.append(Waypoint(t0 + t, x, y, max(0, speed)))
        if len(waypoints) > MAX_WAYPOINTS_PER_ACTOR:
            raise ValueError(
                "Sampling would exceed the waypoint limit; increase the sampling interval."
            )
    waypoints[-1] = Waypoint(t0 + duration, *points[-1], ve)
    return waypoints, {
        "length_m": distance,
        "duration_s": duration,
        "peak_speed_mps": peak,
        "parameters": values,
    }
