"""Coordinate handling and polygon hygiene.

Server-side everything lives in EPSG:28992 (Rijksdriehoek) metres until the very
last step, where a single affine transform takes it to millimetres on the plate.
Doing the scaling early means every threshold in PrintSettings is expressed in
real printed millimetres, which is what you actually care about.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pyproj import Transformer
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

from .config import CRS_RD, CRS_WGS84

Bbox = tuple[float, float, float, float]


@lru_cache(maxsize=4)
def _tf(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def wgs84_to_rd(lon: float, lat: float) -> tuple[float, float]:
    return _tf(CRS_WGS84, CRS_RD).transform(lon, lat)


def rd_to_wgs84(x: float, y: float) -> tuple[float, float]:
    return _tf(CRS_RD, CRS_WGS84).transform(x, y)


def bbox_wgs84_to_rd(bbox: Bbox) -> Bbox:
    """w,s,e,n in degrees -> minx,miny,maxx,maxy in RD metres.

    RD is rotated relative to lon/lat, so a lon/lat rectangle is not a rectangle
    in RD. Transforming all four corners and taking the envelope guarantees the
    RD box covers everything the user selected.
    """
    w, s, e, n = bbox
    corners = [(w, s), (e, s), (e, n), (w, n)]
    pts = [wgs84_to_rd(lon, lat) for lon, lat in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_rd_to_wgs84(bbox: Bbox) -> Bbox:
    minx, miny, maxx, maxy = bbox
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
    pts = [rd_to_wgs84(x, y) for x, y in corners]
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons), min(lats), max(lons), max(lats))


def geom_rd_to_wgs84(geom: BaseGeometry) -> BaseGeometry:
    return shapely_transform(lambda x, y, z=None: _tf(CRS_RD, CRS_WGS84).transform(x, y), geom)


@dataclass(frozen=True)
class PlateTransform:
    """Maps RD metres onto the print plate in millimetres."""

    bbox_rd: Bbox
    scale: float  # mm per metre
    width_mm: float
    height_mm: float

    @staticmethod
    def fit(bbox_rd: Bbox, max_size_mm: float) -> "PlateTransform":
        minx, miny, maxx, maxy = bbox_rd
        w_m = max(maxx - minx, 1e-6)
        h_m = max(maxy - miny, 1e-6)
        scale = max_size_mm / max(w_m, h_m)
        return PlateTransform(bbox_rd, scale, w_m * scale, h_m * scale)

    @property
    def mm_per_m(self) -> float:
        return self.scale

    @property
    def ratio_label(self) -> str:
        """Human readable map scale, e.g. '1 : 10.000'."""
        denom = 1000.0 / self.scale if self.scale else 0.0
        return f"1 : {denom:,.0f}".replace(",", ".")

    def to_mm(self, geom: BaseGeometry) -> BaseGeometry:
        minx, miny, _, _ = self.bbox_rd
        s = self.scale
        return affinity.affine_transform(geom, [s, 0.0, 0.0, s, -minx * s, -miny * s])

    def point_to_mm(self, x_rd: float, y_rd: float) -> tuple[float, float]:
        minx, miny, _, _ = self.bbox_rd
        return ((x_rd - minx) * self.scale, (y_rd - miny) * self.scale)

    def metres_to_mm(self, metres: float) -> float:
        return metres * self.scale

    def plate_polygon(self) -> Polygon:
        return box(0.0, 0.0, self.width_mm, self.height_mm)


def sanitize(
    geom: BaseGeometry | None,
    *,
    min_area: float = 0.0,
    simplify: float = 0.0,
) -> MultiPolygon:
    """Force any geometry into a clean, valid MultiPolygon.

    OSM and BAG data both contain self-intersecting rings; extruding one of those
    produces a mesh the slicer will silently mangle. buffer(0) is the cheapest
    reliable repair.
    """
    if geom is None or geom.is_empty:
        return MultiPolygon()

    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty:
        return MultiPolygon()

    if simplify > 0:
        geom = geom.simplify(simplify, preserve_topology=True)
        if not geom.is_valid:
            geom = geom.buffer(0)

    polys: list[Polygon] = []
    for part in getattr(geom, "geoms", [geom]):
        if part.is_empty or part.geom_type != "Polygon":
            continue
        if min_area > 0 and part.area < min_area:
            continue
        # Drop pinhole interior rings that would print as unresolvable slivers.
        if min_area > 0 and part.interiors:
            keep = [r for r in part.interiors if Polygon(r).area >= min_area]
            part = Polygon(part.exterior, keep)
        polys.append(part)

    return MultiPolygon(polys) if polys else MultiPolygon()


def union_all(geoms: list[BaseGeometry]) -> BaseGeometry:
    live = [g for g in geoms if g is not None and not g.is_empty]
    if not live:
        return MultiPolygon()
    return unary_union(live)
