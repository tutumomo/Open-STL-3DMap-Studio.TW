"""Data sources: 3DBAG (buildings), OpenStreetMap (surfaces), PDOK (geocoding).

Responses are cached on disk keyed by request, because Overpass rate-limits hard
and you will re-fetch the same area many times while tweaking the design.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

from .config import CRS_RD, CRS_WGS84
from .geo import Bbox, bbox_rd_to_wgs84, sanitize, union_all
from .paths import data_dir

log = logging.getLogger(__name__)

CACHE_DIR = data_dir() / "cache"
USER_AGENT = "open-stl-3dmap-studio/1.1 (OpenAEC Foundation)"

BAG3D_ITEMS = "https://api.3dbag.nl/collections/pand/items"
BAG3D_WFS = "https://data.3dbag.nl/api/BAG3D/wfs"
PDOK_GEOCODE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
# Overpass is a set of free public mirrors, each of which is regularly
# overloaded. We try them in turn with a short per-request timeout so a slow one
# is abandoned quickly rather than hanging the whole fetch.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]
OVERPASS_TIMEOUT = 60  # seconds per endpoint; a healthy mirror answers in a few

_to_rd = Transformer.from_crs(CRS_WGS84, CRS_RD, always_xy=True)


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def _cache_path(kind: str, key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    d = CACHE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def _cached_json(kind: str, key: str, fetch, *, refresh: bool = False) -> Any:
    path = _cache_path(kind, key)
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("corrupt cache entry %s, refetching", path)
    data = fetch()
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        log.warning("could not write cache entry %s", path)
    return data


# --------------------------------------------------------------------------- #
# PDOK Locatieserver - address / place search
# --------------------------------------------------------------------------- #
_POINT_RE = re.compile(r"POINT\(([-\d.]+)\s+([-\d.]+)\)")


def geocode(query: str, rows: int = 8) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "rows": rows,
        "fl": "id,weergavenaam,type,centroide_ll,centroide_rd",
        "fq": "type:(woonplaats OR gemeente OR weg OR adres OR postcode OR buurt OR wijk)",
    }
    r = requests.get(PDOK_GEOCODE, params=params, timeout=20,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    docs = r.json().get("response", {}).get("docs", [])

    out = []
    for d in docs:
        m = _POINT_RE.search(d.get("centroide_ll", "") or "")
        if not m:
            continue
        out.append({
            "label": d.get("weergavenaam", ""),
            "type": d.get("type", ""),
            "lon": float(m.group(1)),
            "lat": float(m.group(2)),
        })
    return out


# --------------------------------------------------------------------------- #
# 3DBAG - building footprints with real measured heights
# --------------------------------------------------------------------------- #
@dataclass
class Building:
    id: str
    footprint_rd: Polygon | MultiPolygon
    height_m: float
    ground_m: float
    year: int | None = None
    floors: int | None = None
    status: str = ""


def _cityjson_polygon(boundaries: list, verts: list[list[float]]) -> BaseGeometry:
    """Turn a CityJSON MultiSurface boundary array into a shapely polygon.

    LoD 0 of a 3DBAG Building is the footprint: a MultiSurface whose surfaces are
    [exterior_ring, *interior_rings] of vertex indices.
    """
    parts: list[Polygon] = []
    for surface in boundaries:
        if not surface:
            continue
        rings: list[list[tuple[float, float]]] = []
        for ring in surface:
            pts = [(verts[i][0], verts[i][1]) for i in ring if 0 <= i < len(verts)]
            if len(pts) >= 3:
                rings.append(pts)
        if not rings:
            continue
        try:
            poly = Polygon(rings[0], rings[1:])
        except (ValueError, TypeError):
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty and poly.geom_type in ("Polygon", "MultiPolygon"):
            parts.append(poly)

    if not parts:
        return Polygon()
    return union_all(parts)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_bag3d_page(payload: dict) -> list[Building]:
    transform = (payload.get("metadata") or {}).get("transform") or {}
    scale = transform.get("scale", [0.001, 0.001, 0.001])
    translate = transform.get("translate", [0.0, 0.0, 0.0])

    buildings: list[Building] = []
    for feature in payload.get("features", []):
        objects = feature.get("CityObjects") or {}
        raw_verts = feature.get("vertices") or []
        verts = [
            [v[0] * scale[0] + translate[0],
             v[1] * scale[1] + translate[1],
             v[2] * scale[2] + translate[2]]
            for v in raw_verts
        ]

        for obj_id, obj in objects.items():
            if obj.get("type") != "Building":
                continue
            attrs = obj.get("attributes") or {}

            footprint = Polygon()
            for geom in obj.get("geometry") or []:
                if str(geom.get("lod")) == "0":
                    footprint = _cityjson_polygon(geom.get("boundaries") or [], verts)
                    break
            if footprint.is_empty:
                continue

            roof = _as_float(attrs.get("b3_h_dak_50p"))
            ground = _as_float(attrs.get("b3_h_maaiveld"))
            if roof is None:
                continue
            if ground is None:
                ground = 0.0

            buildings.append(Building(
                id=str(attrs.get("identificatie") or obj_id),
                footprint_rd=footprint,
                height_m=max(roof - ground, 0.0),
                ground_m=ground,
                year=int(attrs["oorspronkelijkbouwjaar"])
                if str(attrs.get("oorspronkelijkbouwjaar", "")).isdigit() else None,
                floors=int(attrs["b3_bouwlagen"])
                if str(attrs.get("b3_bouwlagen", "")).isdigit() else None,
                status=str(attrs.get("status") or ""),
            ))
    return buildings


# The API caps a page at 100 features regardless of what you ask for, and
# returns HTTP 500 when offset is explicitly 0 - so the first page omits it.
BAG3D_PAGE_SIZE = 100
BAG3D_WORKERS = 6


def _fetch_bag3d_page(bbox: str, offset: int) -> dict:
    params: dict[str, Any] = {"bbox": bbox, "limit": BAG3D_PAGE_SIZE}
    if offset:
        params["offset"] = offset

    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(BAG3D_ITEMS, params=params, timeout=180,
                             headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"3DBAG gaf een fout bij offset {offset}: {last}")


# --- primary route: the 2D WFS ------------------------------------------- #
# Same data as the API but a plain GeoJSON dump: ~10.000 panden in one 7 second
# request, versus 50 panden per 26 second call on the OGC API. Note the shorter
# attribute names here: b3_h_50p, not b3_h_dak_50p.
WFS_PAGE = 10000


def _fetch_wfs_page(bbox_rd: Bbox, start_index: int) -> dict:
    minx, miny, maxx, maxy = bbox_rd
    params: dict[str, Any] = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "BAG3D:lod12",
        "bbox": f"{minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f},urn:ogc:def:crs:EPSG::28992",
        "outputFormat": "application/json",
        "count": WFS_PAGE,
    }
    if start_index:
        params["startIndex"] = start_index

    r = requests.get(BAG3D_WFS, params=params, timeout=300,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()


def _parse_wfs_page(payload: dict) -> list[Building]:
    from shapely.geometry import shape

    buildings: list[Building] = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        geom_json = feature.get("geometry")
        if not geom_json:
            continue

        roof = _as_float(props.get("b3_h_50p"))
        if roof is None:
            continue
        ground = _as_float(props.get("b3_h_maaiveld")) or 0.0

        try:
            footprint = shape(geom_json)
        except (ValueError, TypeError, AttributeError):
            continue
        if footprint.is_empty:
            continue
        if not footprint.is_valid:
            footprint = footprint.buffer(0)
        if footprint.is_empty:
            continue

        buildings.append(Building(
            id=str(props.get("identificatie") or props.get("fid") or ""),
            footprint_rd=footprint,
            height_m=max(roof - ground, 0.0),
            ground_m=ground,
            year=int(props["oorspronkelijkbouwjaar"])
            if str(props.get("oorspronkelijkbouwjaar", "")).isdigit() else None,
            floors=int(props["b3_bouwlagen"])
            if str(props.get("b3_bouwlagen", "")).isdigit() else None,
            status=str(props.get("status") or ""),
        ))
    return buildings


def _fetch_buildings_wfs(bbox_rd: Bbox, max_features: int, refresh: bool) -> list[Building]:
    minx, miny, maxx, maxy = bbox_rd
    key = f"bag3dwfs|{minx:.1f},{miny:.1f},{maxx:.1f},{maxy:.1f}"

    def fetch() -> list[dict]:
        pages = [_fetch_wfs_page(bbox_rd, 0)]
        matched = int(pages[0].get("numberMatched") or 0)
        total = min(matched, max_features)
        start = len(pages[0].get("features") or [])
        while start < total:
            pages.append(_fetch_wfs_page(bbox_rd, start))
            returned = len(pages[-1].get("features") or [])
            if not returned:
                break
            start += returned
        log.info("3dbag wfs: %d panden in %d pagina's", matched, len(pages))
        return pages

    pages = _cached_json("bag3d-wfs", key, fetch, refresh=refresh)
    buildings: list[Building] = []
    for page in pages:
        buildings.extend(_parse_wfs_page(page))
    return buildings


def fetch_buildings(bbox_rd: Bbox, *, max_features: int = 60000,
                    refresh: bool = False) -> list[Building]:
    """All BAG buildings intersecting the RD bbox, with AHN-derived heights."""
    try:
        buildings = _fetch_buildings_wfs(bbox_rd, max_features, refresh)
        if buildings:
            return buildings
        log.warning("3DBAG WFS gaf niets terug, val terug op de API")
    except Exception as exc:  # noqa: BLE001 - any failure should fall back
        log.warning("3DBAG WFS mislukt (%s), val terug op de tragere API", exc)
    return _fetch_buildings_api(bbox_rd, max_features, refresh)


def _fetch_buildings_api(bbox_rd: Bbox, max_features: int,
                         refresh: bool) -> list[Building]:
    """Fallback: the OGC API Features endpoint. Correct, but slow."""
    minx, miny, maxx, maxy = bbox_rd
    bbox = f"{minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}"
    key = f"bag3d|{minx:.1f},{miny:.1f},{maxx:.1f},{maxy:.1f}"

    def fetch() -> list[dict]:
        first = _fetch_bag3d_page(bbox, 0)
        matched = int(first.get("numberMatched") or 0)
        total = min(matched, max_features)
        offsets = list(range(BAG3D_PAGE_SIZE, total, BAG3D_PAGE_SIZE))
        if not offsets:
            return [first]

        log.info("3dbag: %d panden in %d pagina's", matched, len(offsets) + 1)
        pages = [first]
        with ThreadPoolExecutor(max_workers=BAG3D_WORKERS) as pool:
            for page in pool.map(lambda o: _fetch_bag3d_page(bbox, o), offsets):
                pages.append(page)
        return pages

    pages = _cached_json("bag3d", key, fetch, refresh=refresh)
    buildings: list[Building] = []
    for page in pages:
        buildings.extend(_parse_bag3d_page(page))
    return buildings


# --------------------------------------------------------------------------- #
# OpenStreetMap via Overpass - water, roads, green
# --------------------------------------------------------------------------- #
# Metres, used when the way carries no explicit width tag.
ROAD_WIDTH_M = {
    "motorway": 22.0, "motorway_link": 10.0,
    "trunk": 16.0, "trunk_link": 9.0,
    "primary": 14.0, "primary_link": 8.0,
    "secondary": 12.0, "secondary_link": 7.0,
    "tertiary": 10.0, "tertiary_link": 6.0,
    "unclassified": 8.0, "residential": 8.0, "living_street": 7.0,
    "pedestrian": 7.0, "service": 4.5, "track": 3.0,
    "footway": 2.0, "cycleway": 2.5, "path": 1.8, "steps": 2.0,
}
MINOR_ROADS = {"footway", "cycleway", "path", "steps", "track", "service"}

# Low vegetation: grass, scrub, parks - printed as a low green band.
SHRUB_LANDUSE = {"grass", "meadow", "recreation_ground", "village_green",
                 "allotments", "cemetery", "farmland", "orchard", "vineyard"}
SHRUB_NATURAL = {"scrub", "grassland", "heath"}
SHRUB_LEISURE = {"park", "garden", "pitch", "golf_course", "playground"}
# Tree-covered areas: printed taller than the shrubs.
WOOD_LANDUSE = {"forest"}
WOOD_NATURAL = {"wood"}

OVERPASS_QUERY = """
[out:json][timeout:{timeout}];
(
  way["natural"="water"]({bbox});
  relation["natural"="water"]({bbox});
  way["landuse"~"^(basin|reservoir)$"]({bbox});
  relation["landuse"~"^(basin|reservoir)$"]({bbox});
  way["waterway"="riverbank"]({bbox});
  way["waterway"~"^(river|canal|stream|ditch|drain)$"]({bbox});
  way["highway"]({bbox});
  way["leisure"~"^(park|garden|pitch|golf_course|playground)$"]({bbox});
  relation["leisure"~"^(park|garden|golf_course)$"]({bbox});
  way["landuse"~"^(grass|forest|meadow|recreation_ground|village_green|allotments|cemetery|farmland|orchard|vineyard)$"]({bbox});
  relation["landuse"~"^(forest|recreation_ground|cemetery|farmland)$"]({bbox});
  way["natural"~"^(wood|scrub|grassland|heath)$"]({bbox});
  relation["natural"~"^(wood|scrub)$"]({bbox});
  node["natural"="tree"]({bbox});
  way["natural"="tree_row"]({bbox});
);
out geom;
"""


@dataclass
class SurfaceData:
    water: MultiPolygon = field(default_factory=MultiPolygon)
    # Low vegetation (grass, scrub, parks).
    shrubs: MultiPolygon = field(default_factory=MultiPolygon)
    # Tree-covered areas (forest, wood).
    woods: MultiPolygon = field(default_factory=MultiPolygon)
    # Individual mapped trees as RD points, and tree rows as RD lines.
    tree_points: list[tuple[float, float]] = field(default_factory=list)
    tree_rows: list[LineString] = field(default_factory=list)
    # Centrelines with their real-world width in metres. Kept as lines so the
    # pipeline can widen only the streets that fall below the nozzle limit once
    # it knows the map scale.
    road_lines: list[tuple[LineString, float]] = field(default_factory=list)

    @property
    def road_count(self) -> int:
        return len(self.road_lines)

    @property
    def tree_count(self) -> int:
        return len(self.tree_points) + len(self.tree_rows)

    @property
    def green(self) -> MultiPolygon:
        """Shrubs and woods together, for the map preview."""
        return sanitize(union_all([self.shrubs, self.woods]))

    def roads_at(self, min_width_m: float = 0.0) -> MultiPolygon:
        """Road surface in RD metres, for previews."""
        parts = [line.buffer(max(width, min_width_m) / 2.0, cap_style=2, join_style=1)
                 for line, width in self.road_lines]
        return sanitize(union_all(parts))


class OverpassUnavailable(RuntimeError):
    """Every Overpass mirror failed. Buildings are unaffected, so callers can
    still build a map without water/roads/green rather than aborting."""


def _overpass(query: str, *, refresh: bool = False) -> dict:
    def fetch() -> dict:
        last_error: Exception | None = None
        for url in OVERPASS_ENDPOINTS:
            try:
                r = requests.post(url, data={"data": query},
                                  timeout=OVERPASS_TIMEOUT,
                                  headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, json.JSONDecodeError) as exc:
                log.warning("overpass endpoint %s failed: %s", url, exc)
                last_error = exc
        raise OverpassUnavailable(
            "De publieke OpenStreetMap-servers (Overpass) reageren nu niet. "
            "Water, wegen en groen konden niet worden opgehaald."
        ) from last_error

    return _cached_json("overpass", query, fetch, refresh=refresh)


def _coords_to_rd(coords: Iterable[dict]) -> list[tuple[float, float]]:
    lons, lats = [], []
    for c in coords:
        lons.append(c["lon"])
        lats.append(c["lat"])
    if not lons:
        return []
    xs, ys = _to_rd.transform(lons, lats)
    return list(zip(xs, ys))


def _stitch_rings(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join OSM relation member ways into closed rings.

    Members arrive in arbitrary order and direction, so walk the ends until each
    ring closes. Fragments that never close are dropped rather than guessed at.
    """
    rings: list[list[tuple[float, float]]] = []
    pending = [list(w) for w in ways if len(w) >= 2]

    while pending:
        cur = pending.pop(0)
        progressed = True
        while cur[0] != cur[-1] and progressed and pending:
            progressed = False
            for i, w in enumerate(pending):
                if w[0] == cur[-1]:
                    cur += w[1:]
                elif w[-1] == cur[-1]:
                    cur += w[-2::-1]
                elif w[-1] == cur[0]:
                    cur = w[:-1] + cur
                elif w[0] == cur[0]:
                    cur = w[:0:-1] + cur
                else:
                    continue
                pending.pop(i)
                progressed = True
                break
        if len(cur) >= 4 and cur[0] == cur[-1]:
            rings.append(cur)
    return rings


def _polygon_from_element(el: dict) -> BaseGeometry | None:
    """Build a polygon from an Overpass way or multipolygon relation."""
    if el.get("type") == "way":
        pts = _coords_to_rd(el.get("geometry") or [])
        if len(pts) < 4 or pts[0] != pts[-1]:
            return None
        try:
            poly = Polygon(pts)
        except ValueError:
            return None
        return poly.buffer(0) if not poly.is_valid else poly

    if el.get("type") == "relation":
        outer_ways, inner_ways = [], []
        for member in el.get("members") or []:
            pts = _coords_to_rd(member.get("geometry") or [])
            if len(pts) < 2:
                continue
            (inner_ways if member.get("role") == "inner" else outer_ways).append(pts)

        outers = [Polygon(r) for r in _stitch_rings(outer_ways) if len(r) >= 4]
        inners = [Polygon(r) for r in _stitch_rings(inner_ways) if len(r) >= 4]
        if not outers:
            return None
        shell = union_all([p.buffer(0) for p in outers])
        if inners:
            holes = union_all([p.buffer(0) for p in inners])
            shell = shell.difference(holes)
        return shell

    return None


def _road_width(tags: dict[str, str]) -> float:
    explicit = _as_float(tags.get("width"))
    if explicit and 0.5 < explicit < 100:
        return explicit
    lanes = _as_float(tags.get("lanes"))
    base = ROAD_WIDTH_M.get(tags.get("highway", ""), 6.0)
    if lanes and lanes >= 1:
        return max(base, lanes * 3.2)
    return base


def fetch_surfaces(bbox_rd: Bbox, *, include_minor_roads: bool = True,
                   refresh: bool = False) -> SurfaceData:
    """Water bodies, green areas and road surfaces inside the RD bbox."""
    w, s, e, n = bbox_rd_to_wgs84(bbox_rd)
    # Overpass bbox order is south,west,north,east.
    query = OVERPASS_QUERY.format(bbox=f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}",
                                  timeout=OVERPASS_TIMEOUT)
    data = _overpass(query, refresh=refresh)

    water_parts: list[BaseGeometry] = []
    shrub_parts: list[BaseGeometry] = []
    wood_parts: list[BaseGeometry] = []
    tree_points: list[tuple[float, float]] = []
    tree_rows: list[LineString] = []
    road_lines: list[tuple[LineString, float]] = []

    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        natural = tags.get("natural")
        landuse = tags.get("landuse")
        leisure = tags.get("leisure")

        # Individual trees are nodes; tree rows are ways.
        if natural == "tree":
            lon, lat = el.get("lon"), el.get("lat")
            if lon is not None and lat is not None:
                tree_points.append(tuple(_to_rd.transform(lon, lat)))
            continue
        if natural == "tree_row":
            pts = _coords_to_rd(el.get("geometry") or [])
            if len(pts) >= 2:
                tree_rows.append(LineString(pts))
            continue

        highway = tags.get("highway")
        if highway:
            if highway in ("proposed", "construction", "raceway"):
                continue
            if not include_minor_roads and highway in MINOR_ROADS:
                continue
            pts = _coords_to_rd(el.get("geometry") or [])
            if len(pts) < 2:
                continue
            try:
                road_lines.append((LineString(pts), _road_width(tags)))
            except ValueError:
                pass
            continue

        waterway = tags.get("waterway")
        if waterway in ("river", "canal", "stream", "ditch", "drain"):
            pts = _coords_to_rd(el.get("geometry") or [])
            if len(pts) >= 2:
                default = {"river": 30.0, "canal": 12.0, "stream": 3.0,
                           "ditch": 2.0, "drain": 2.0}[waterway]
                width = _as_float(tags.get("width")) or default
                water_parts.append(LineString(pts).buffer(width / 2.0, cap_style=2))
            continue

        poly = _polygon_from_element(el)
        if poly is None or poly.is_empty:
            continue

        if natural == "water" or waterway == "riverbank" \
                or landuse in ("basin", "reservoir"):
            water_parts.append(poly)
        elif landuse in WOOD_LANDUSE or natural in WOOD_NATURAL:
            wood_parts.append(poly)
        elif (landuse in SHRUB_LANDUSE or natural in SHRUB_NATURAL
              or leisure in SHRUB_LEISURE):
            shrub_parts.append(poly)
        else:
            # Anything else green-ish we still treat as low vegetation.
            shrub_parts.append(poly)

    return SurfaceData(
        water=sanitize(union_all(water_parts)),
        shrubs=sanitize(union_all(shrub_parts)),
        woods=sanitize(union_all(wood_parts)),
        tree_points=tree_points,
        tree_rows=tree_rows,
        road_lines=road_lines,
    )
