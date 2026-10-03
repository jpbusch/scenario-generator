"""Transform map coordinates without changing the editor's local metric frame."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cached_property
import math
from pathlib import Path

from defusedxml.ElementTree import parse
from pyproj import CRS, Transformer


@dataclass(frozen=True)
class MapFrame:
    crs: str
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0
    z: float = 0.0

    def __post_init__(self):
        projection = CRS.from_user_input(self.crs)
        if not projection.is_projected or not all(
            math.isfinite(v) for v in (self.x, self.y, self.heading, self.z)
        ):
            raise ValueError(
                "The map frame needs a projected metric CRS and finite offsets."
            )
        if any(
            abs(axis.unit_conversion_factor - 1) > 1e-9
            for axis in projection.axis_info[:2]
        ):
            raise ValueError("Map frame coordinates must use metres.")

    @cached_property
    def forward(self):
        return Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)

    @cached_property
    def inverse(self):
        return Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)

    def to_projected(self, x, y):
        c, s = math.cos(self.heading), math.sin(self.heading)
        return self.x + c * x - s * y, self.y + s * x + c * y

    def from_projected(self, x, y):
        c, s = math.cos(self.heading), math.sin(self.heading)
        dx, dy = x - self.x, y - self.y
        return c * dx + s * dy, -s * dx + c * dy

    def from_wgs84(self, lon, lat):
        return self.from_projected(*self.forward.transform(lon, lat, errcheck=True))

    def to_wgs84(self, x, y):
        return self.inverse.transform(*self.to_projected(x, y), errcheck=True)

    def payload(self):
        lon, lat = self.to_wgs84(0, 0)
        return {**asdict(self), "origin_lon": lon, "origin_lat": lat}

    @classmethod
    def from_payload(cls, value):
        return cls(
            str(value["crs"]),
            float(value.get("x", 0)),
            float(value.get("y", 0)),
            float(value.get("heading", 0)),
            float(value.get("z", 0)),
        )

    @classmethod
    def local_utm(cls, lon, lat):
        zone = max(1, min(60, int((lon + 180) / 6) + 1))
        crs = f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(
            lon, lat
        )
        return cls(crs, x, y)


def read_xodr_frame(path: Path) -> MapFrame | None:
    header = parse(path).getroot().find("header")
    if header is None:
        return None
    reference = header.findtext("geoReference", "").strip()
    if not reference:
        return None
    offset = header.find("offset")
    attributes = offset.attrib if offset is not None else {}
    return MapFrame(
        reference,
        float(attributes.get("x", 0)),
        float(attributes.get("y", 0)),
        float(attributes.get("hdg", 0)),
        float(attributes.get("z", 0)),
    )


def append_frame(header, frame):
    """Write the projection and local-frame offset into an exported XODR header."""
    import xml.etree.ElementTree as ET

    ET.SubElement(header, "geoReference").text = frame.crs
    ET.SubElement(
        header,
        "offset",
        x=str(frame.x),
        y=str(frame.y),
        z=str(frame.z),
        hdg=str(frame.heading),
    )


def write_referenced_xodr(source: Path, destination: Path, frame: MapFrame):
    """Retain source roads and junctions when only its georeference was supplied."""
    import xml.etree.ElementTree as ET

    root = parse(source).getroot()
    header = root.find("header")
    if header is None:
        header = ET.Element("header")
        root.insert(0, header)
    for child in list(header):
        if child.tag in {"geoReference", "offset"}:
            header.remove(child)
    append_frame(header, frame)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
