"""Detailed LoD2.2 buildings from 3DBAG.

The fast WFS route only carries 2D footprints. Real roof shapes live in the
CityJSON solids of the slower OGC API, so this module is only used when the user
turns on detail mode. Each building becomes a triangulated mesh expressed in RD
metres with z measured above its own ground level, ready for the pipeline to
scale onto the plate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from mapbox_earcut import triangulate_float32
from shapely.geometry import MultiPolygon, Polygon

from .geo import Bbox
from .sources import (
    BAG3D_PAGE_SIZE,
    BAG3D_WORKERS,
    _as_float,
    _cached_json,
    _cityjson_polygon,
    _fetch_bag3d_page,
)

log = logging.getLogger(__name__)


@dataclass
class DetailedBuilding:
    id: str
    vertices: np.ndarray  # (N, 3): x_rd, y_rd, z above own ground (metres)
    faces: np.ndarray     # (M, 3) triangle indices
    footprint_rd: Polygon | MultiPolygon
    ground_m: float
    height_m: float


def _triangulate_face(points: np.ndarray) -> list[tuple[int, int, int]]:
    """Fan/earcut triangulate one planar 3D polygon ring.

    3DBAG roof faces are planar but arbitrarily oriented, so we project onto the
    plane's two dominant axes before ear-cutting and keep the original indices.
    """
    n = len(points)
    if n < 3:
        return []
    if n == 3:
        return [(0, 1, 2)]

    # Newell's method gives a stable normal even for slightly non-planar rings.
    normal = np.zeros(3)
    for i in range(n):
        cur, nxt = points[i], points[(i + 1) % n]
        normal[0] += (cur[1] - nxt[1]) * (cur[2] + nxt[2])
        normal[1] += (cur[2] - nxt[2]) * (cur[0] + nxt[0])
        normal[2] += (cur[0] - nxt[0]) * (cur[1] + nxt[1])
    drop = int(np.argmax(np.abs(normal)))
    axes = [a for a in range(3) if a != drop]
    flat = np.ascontiguousarray(points[:, axes], dtype=np.float32)

    try:
        idx = triangulate_float32(flat, np.array([n], dtype=np.uint32))
    except Exception as exc:  # noqa: BLE001 - degenerate ring
        log.debug("face triangulation failed: %s", exc)
        return []
    return [tuple(int(i) for i in idx[k:k + 3]) for k in range(0, len(idx), 3)]


def _solid_to_mesh(boundaries: list, verts: list[list[float]],
                   ground_m: float) -> tuple[np.ndarray, np.ndarray] | None:
    """Turn a CityJSON Solid boundary array into (vertices, faces).

    Solid = [outer_shell, *inner_shells]; a shell is a list of faces; a face is
    [exterior_ring, *holes]; a ring is a list of vertex indices. We keep only
    exterior rings - the 3DBAG solids have no real interior rings in practice -
    and z is stored above ground so the pipeline can drop it onto the maaiveld.
    """
    out_verts: list[list[float]] = []
    out_faces: list[tuple[int, int, int]] = []
    remap: dict[int, int] = {}

    def local(global_idx: int) -> int:
        if global_idx not in remap:
            v = verts[global_idx]
            remap[global_idx] = len(out_verts)
            out_verts.append([v[0], v[1], v[2] - ground_m])
        return remap[global_idx]

    for shell in boundaries:
        for face in shell:
            if not face:
                continue
            ring = face[0]  # exterior
            if len(ring) < 3:
                continue
            local_ids = [local(i) for i in ring if 0 <= i < len(verts)]
            if len(local_ids) < 3:
                continue
            pts = np.array([out_verts[i] for i in local_ids], dtype=np.float64)
            for a, b, c in _triangulate_face(pts):
                out_faces.append((local_ids[a], local_ids[b], local_ids[c]))

    if not out_faces:
        return None
    return np.array(out_verts, dtype=np.float64), np.array(out_faces, dtype=np.int64)


def _parse_detailed_page(payload: dict) -> list[DetailedBuilding]:
    transform = (payload.get("metadata") or {}).get("transform") or {}
    scale = transform.get("scale", [0.001, 0.001, 0.001])
    translate = transform.get("translate", [0.0, 0.0, 0.0])

    out: list[DetailedBuilding] = []
    for feature in payload.get("features", []):
        objects = feature.get("CityObjects") or {}
        raw = feature.get("vertices") or []
        verts = [[v[0] * scale[0] + translate[0],
                  v[1] * scale[1] + translate[1],
                  v[2] * scale[2] + translate[2]] for v in raw]

        # A Building holds attributes and a footprint; its geometry sits on the
        # child BuildingPart, so gather both.
        for obj_id, obj in objects.items():
            if obj.get("type") != "Building":
                continue
            attrs = obj.get("attributes") or {}
            ground = _as_float(attrs.get("b3_h_maaiveld")) or 0.0
            roof = _as_float(attrs.get("b3_h_dak_50p"))

            footprint = Polygon()
            for g in obj.get("geometry") or []:
                if str(g.get("lod")) == "0":
                    footprint = _cityjson_polygon(g.get("boundaries") or [], verts)
                    break

            solid = None
            for child_id in obj.get("children") or []:
                child = objects.get(child_id) or {}
                for g in child.get("geometry") or []:
                    if str(g.get("lod")) == "2.2" and g.get("type") == "Solid":
                        solid = g.get("boundaries")
                        break
                if solid:
                    break

            if not solid:
                continue
            mesh = _solid_to_mesh(solid, verts, ground)
            if mesh is None:
                continue

            out.append(DetailedBuilding(
                id=str(attrs.get("identificatie") or obj_id),
                vertices=mesh[0],
                faces=mesh[1],
                footprint_rd=footprint,
                ground_m=ground,
                height_m=max((roof or 0.0) - ground, 0.0),
            ))
    return out


def fetch_buildings_detailed(bbox_rd: Bbox, *, max_features: int = 20000,
                             refresh: bool = False) -> list[DetailedBuilding]:
    """LoD2.2 solids for the bbox. Slow (public API): minutes for a city."""
    from concurrent.futures import ThreadPoolExecutor

    minx, miny, maxx, maxy = bbox_rd
    bbox = f"{minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}"
    key = f"bag3d-lod22|{minx:.1f},{miny:.1f},{maxx:.1f},{maxy:.1f}"

    def fetch() -> list[dict]:
        first = _fetch_bag3d_page(bbox, 0)
        matched = int(first.get("numberMatched") or 0)
        total = min(matched, max_features)
        offsets = list(range(BAG3D_PAGE_SIZE, total, BAG3D_PAGE_SIZE))
        log.info("3dbag LoD2.2: %d panden in %d pagina's (dit duurt even)",
                 matched, len(offsets) + 1)
        pages = [first]
        if offsets:
            with ThreadPoolExecutor(max_workers=BAG3D_WORKERS) as pool:
                pages.extend(pool.map(lambda o: _fetch_bag3d_page(bbox, o), offsets))
        return pages

    pages = _cached_json("bag3d-lod22", key, fetch, refresh=refresh)
    buildings: list[DetailedBuilding] = []
    for page in pages:
        buildings.extend(_parse_detailed_page(page))
    return buildings
