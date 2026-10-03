"""Bounded, coalesced NRW WMS tile cache with exact local-frame reprojection."""

from __future__ import annotations

from concurrent.futures import Future
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import urllib.parse
import urllib.request

import numpy as np
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.transform import Affine, from_bounds
from rasterio.warp import reproject, Resampling


class OrthophotoCache:
    def __init__(self, directory=None, max_bytes=512 * 1024 * 1024):
        self.directory = Path(
            directory
            or os.getenv(
                "SCENARIO_GENERATOR_ORTHOPHOTO_CACHE",
                "/tmp/scenario-generator-orthophotos",
            )
        )
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.pending = {}
        self.slots = threading.Semaphore(4)

    def tile(self, frame, level, column, row):
        if not -3 <= level <= 9 or max(abs(column), abs(row)) > 100000:
            raise ValueError("Invalid orthophoto tile coordinates.")
        frame_key = {key: value for key, value in frame.payload().items() if key != "z"}
        key = hashlib.sha256(
            json.dumps([frame_key, level, column, row], sort_keys=True).encode()
        ).hexdigest()
        destination = self.directory / f"{key}.png"
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if destination.exists():
                destination.touch()
                return destination.read_bytes()
            if key in self.pending:
                future, owner = self.pending[key], False
            else:
                future, owner = Future(), True
                self.pending[key] = future
        if not owner:
            return future.result(timeout=30)
        try:
            with self.slots:
                image = self.fetch(frame, level, column, row)
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(image)
            temporary.replace(destination)
            with self.lock:
                files = sorted(
                    self.directory.glob("*.png"), key=lambda p: p.stat().st_mtime
                )
                total = sum(p.stat().st_size for p in files)
                for path in files:
                    if total <= self.max_bytes:
                        break
                    total -= path.stat().st_size
                    path.unlink(missing_ok=True)
            future.set_result(image)
            return image
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)

    @staticmethod
    def fetch(frame, level, column, row):
        size = 256 * 2.0**level
        xmin, ymin = column * size, row * size
        xmax, ymax = xmin + size, ymin + size
        transformer = Transformer.from_crs(frame.crs, "EPSG:25832", always_xy=True)
        edge_points = [(xmin + size * i / 8, y) for i in range(9) for y in (ymin, ymax)]
        edge_points += [
            (x, ymin + size * i / 8) for i in range(9) for x in (xmin, xmax)
        ]
        projected = [
            transformer.transform(*frame.to_projected(x, y), errcheck=True)
            for x, y in edge_points
        ]
        bounds = (
            min(p[0] for p in projected),
            min(p[1] for p in projected),
            max(p[0] for p in projected),
            max(p[1] for p in projected),
        )
        parameters = {
            "SERVICE": "WMS",
            "REQUEST": "GetMap",
            "VERSION": "1.3.0",
            "LAYERS": "nw_dop_rgb",
            "STYLES": "",
            "CRS": "EPSG:25832",
            "BBOX": ",".join(str(v) for v in bounds),
            "WIDTH": 512,
            "HEIGHT": 512,
            "FORMAT": "image/png",
        }
        url = "https://www.wms.nrw.de/geobasis/wms_nw_dop?" + urllib.parse.urlencode(
            parameters
        )
        with urllib.request.urlopen(url, timeout=20) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024 or not data.startswith(b"\x89PNG"):
            raise ValueError("NRW WMS did not return a valid bounded PNG image.")
        c, s = math.cos(frame.heading), math.sin(frame.heading)
        resolution = size / 512
        ox, oy = frame.to_projected(xmin, ymax)
        destination_transform = Affine(
            c * resolution, s * resolution, ox, s * resolution, -c * resolution, oy
        )
        with MemoryFile(data) as source_file, source_file.open() as source:
            output = np.zeros((4, 512, 512), dtype=np.uint8)
            for band in range(min(source.count, 3)):
                reproject(
                    source.read(band + 1),
                    output[band],
                    src_transform=from_bounds(*bounds, source.width, source.height),
                    src_crs="EPSG:25832",
                    dst_transform=destination_transform,
                    dst_crs=frame.crs,
                    resampling=Resampling.bilinear,
                )
            alpha = (
                source.read(4)
                if source.count >= 4
                else np.full((source.height, source.width), 255, dtype=np.uint8)
            )
            reproject(
                alpha,
                output[3],
                src_transform=from_bounds(*bounds, source.width, source.height),
                src_crs="EPSG:25832",
                dst_transform=destination_transform,
                dst_crs=frame.crs,
                resampling=Resampling.nearest,
            )
        with MemoryFile() as result:
            with result.open(
                driver="PNG", width=512, height=512, count=4, dtype="uint8"
            ) as dataset:
                dataset.write(output)
            return result.read()
