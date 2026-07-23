"""Orchestration: selected area -> stacked colour bands -> exported files."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.geometry.base import BaseGeometry

from .config import BANDS_BY_KEY, Project
from .geo import (
    Bbox,
    PlateTransform,
    bbox_wgs84_to_rd,
    geom_rd_to_wgs84,
    rd_to_wgs84,
    sanitize,
    union_all,
    wgs84_to_rd,
)
from .ifc_import import LoadedDesign, place_design
from .meshing import Body, extrude, extrude_varying
from .sources import (
    Building,
    OverpassUnavailable,
    SurfaceData,
    fetch_buildings,
    fetch_surfaces,
)

log = logging.getLogger(__name__)

# Beyond ~4x4 km an 8 m street scaled onto a 200 mm plate is 0.4 mm wide, which
# a 0.4 nozzle cannot resolve - so a bigger area buys detail you cannot print.
MAX_AREA_KM2 = 16.0
WARN_AREA_KM2 = 4.0


@dataclass
class AreaData:
    bbox_rd: Bbox
    buildings: list[Building]
    surfaces: SurfaceData
    # Set when Overpass was unreachable: buildings loaded, surfaces did not.
    surfaces_error: str | None = None

    @property
    def area_km2(self) -> float:
        minx, miny, maxx, maxy = self.bbox_rd
        return (maxx - minx) * (maxy - miny) / 1e6


_area_cache: dict[str, AreaData] = {}


def _area_key(bbox_rd: Bbox) -> str:
    return "|".join(f"{v:.1f}" for v in bbox_rd)


def load_area(project: Project, *, refresh: bool = False) -> AreaData:
    if not project.bbox_wgs84:
        raise ValueError("Nog geen gebied geselecteerd op de kaart.")

    bbox_rd = bbox_wgs84_to_rd(project.bbox_wgs84)
    minx, miny, maxx, maxy = bbox_rd
    area_km2 = (maxx - minx) * (maxy - miny) / 1e6
    if area_km2 > MAX_AREA_KM2:
        raise ValueError(
            f"Gebied is {area_km2:.1f} km2; het maximum is {MAX_AREA_KM2:.0f} km2. "
            f"Zoom verder in of selecteer een kleiner vlak."
        )

    key = _area_key(bbox_rd)
    if not refresh and key in _area_cache:
        return _area_cache[key]

    log.info("fetching area %s (%.2f km2)", key, area_km2)
    buildings = fetch_buildings(bbox_rd, refresh=refresh)

    # Buildings are the point of the map; water/roads/green are a bonus from the
    # flaky public Overpass mirrors. If they are all down, carry on with just
    # the buildings and let the UI show a retry-able warning.
    surfaces_error: str | None = None
    try:
        surfaces = fetch_surfaces(bbox_rd, refresh=refresh)
    except OverpassUnavailable as exc:
        log.warning("surfaces unavailable, continuing with buildings only: %s", exc)
        surfaces = SurfaceData()
        surfaces_error = str(exc)

    area = AreaData(bbox_rd=bbox_rd, buildings=buildings, surfaces=surfaces,
                    surfaces_error=surfaces_error)

    # Only cache a complete result, so "Kaartdata ophalen" retries the surfaces.
    if surfaces_error is None:
        _area_cache[key] = area
    return area


# --------------------------------------------------------------------------- #
# preview for the web UI
# --------------------------------------------------------------------------- #
def _feature(geom: BaseGeometry, props: dict) -> dict:
    return {"type": "Feature", "geometry": mapping(geom), "properties": props}


def preview_geojson(area: AreaData, project: Project) -> dict:
    """Buildings as clickable features, water/roads as a context overlay."""
    hidden = set(project.hidden_building_ids)
    features: list[dict] = []

    for b in area.buildings:
        geom = b.footprint_rd.simplify(0.4, preserve_topology=True)
        if geom.is_empty:
            continue
        features.append(_feature(geom_rd_to_wgs84(geom), {
            "kind": "building",
            "id": b.id,
            "height_m": round(b.height_m, 1),
            "year": b.year,
            "floors": b.floors,
            "hidden": b.id in hidden,
        }))

    for key, geom in (("water", area.surfaces.water),
                      ("shrubs", area.surfaces.shrubs),
                      ("woods", area.surfaces.woods),
                      ("roads", area.surfaces.roads_at())):
        simplified = geom.simplify(1.5, preserve_topology=True)
        if not simplified.is_empty:
            features.append(_feature(geom_rd_to_wgs84(simplified), {"kind": key}))

    # Individual trees as points, so the user sees where they will land. These
    # are already GeoJSON dicts, so they skip _feature()/mapping() (which expects
    # a shapely geometry, not a plain dict).
    for x_rd, y_rd in area.surfaces.tree_points:
        lon, lat = rd_to_wgs84(x_rd, y_rd)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"kind": "tree"},
        })

    return {"type": "FeatureCollection", "features": features}


def area_stats(area: AreaData, project: Project) -> dict:
    s = project.settings.snapped()
    plate = PlateTransform.fit(area.bbox_rd, s.max_size_mm)
    minx, miny, maxx, maxy = area.bbox_rd
    hidden = set(project.hidden_building_ids)

    return {
        "area_km2": round(area.area_km2, 3),
        "width_m": round(maxx - minx, 1),
        "height_m": round(maxy - miny, 1),
        "plate_mm": [round(plate.width_mm, 1), round(plate.height_mm, 1)],
        "scale_ratio": plate.ratio_label,
        "mm_per_m": round(plate.mm_per_m, 4),
        "buildings": len(area.buildings),
        "buildings_hidden": len([b for b in area.buildings if b.id in hidden]),
        "roads": area.surfaces.road_count,
        "trees": area.surfaces.tree_count,
        "has_water": not area.surfaces.water.is_empty,
        "has_shrubs": not area.surfaces.shrubs.is_empty,
        "has_woods": not area.surfaces.woods.is_empty,
        "warn_large": area.area_km2 > WARN_AREA_KM2,
        "surfaces_error": area.surfaces_error,
    }


# --------------------------------------------------------------------------- #
# model construction
# --------------------------------------------------------------------------- #
@dataclass
class BuiltModel:
    bodies: list[Body]
    plate: PlateTransform
    bands: list[dict]
    stats: dict = field(default_factory=dict)
    # The 2D millimetre footprints each body was extruded from. Kept so tests
    # can assert things like "every building sits on land" without re-deriving.
    layers: dict[str, BaseGeometry] = field(default_factory=dict)


def _body(key: str, mesh, settings, slot_override: int | None = None) -> Body | None:
    if mesh is None:
        return None
    spec = BANDS_BY_KEY[key]
    slot = slot_override if slot_override is not None else settings.slot_for(key)
    return Body(key=key, label=spec.label, color=spec.color, slot=slot, mesh=mesh)


def _detailed_buildings_mesh(detailed, hidden, plate, clip, s):
    """Scale each 3DBAG LoD2.2 solid onto the plate and merge into one body."""
    import trimesh

    from .meshing import mesh_from_arrays

    minx, miny, _, _ = plate.bbox_rd
    scale = plate.mm_per_m
    z_scale = scale * s.height_exaggeration
    meshes = []
    for b in detailed:
        if b.id in hidden:
            continue
        foot = plate.to_mm(b.footprint_rd)
        if foot.is_empty or not foot.intersects(clip):
            continue
        v = b.vertices.copy()
        v[:, 0] = (v[:, 0] - minx) * scale
        v[:, 1] = (v[:, 1] - miny) * scale
        v[:, 2] = v[:, 2] * z_scale + s.land_top
        # 3DBAG ground vertices scatter a little around the maaiveld; floor them
        # at land_top so the building sits flat on the land instead of poking
        # through into the water band below.
        v[:, 2] = v[:, 2].clip(min=s.land_top)
        m = mesh_from_arrays(v, b.faces)
        if m is not None:
            meshes.append(m)
    if not meshes:
        return None
    return trimesh.util.concatenate(meshes)


def build_model(
    area: AreaData,
    project: Project,
    design: LoadedDesign | None = None,
    detailed: "list | None" = None,
) -> BuiltModel:
    from .meshing import tree_cones

    s = project.settings.snapped()
    plate = PlateTransform.fit(area.bbox_rd, s.max_size_mm)
    clip = plate.plate_polygon()

    min_area = s.min_feature_area_mm2
    simp = s.simplify_mm

    def to_mm(geom: BaseGeometry) -> MultiPolygon:
        if geom is None or geom.is_empty:
            return MultiPolygon()
        return sanitize(plate.to_mm(geom).intersection(clip),
                        min_area=min_area, simplify=simp)

    water_mm = to_mm(area.surfaces.water) if s.include_water else MultiPolygon()
    shrubs_mm = to_mm(area.surfaces.shrubs) if s.include_shrubs else MultiPolygon()
    woods_mm = to_mm(area.surfaces.woods) if s.include_trees else MultiPolygon()

    roads_mm = MultiPolygon()
    if s.include_roads:
        # Buffer in millimetre space so the nozzle limit is applied per street:
        # a motorway keeps its true width, an alley gets widened to printable.
        parts = []
        for line, width_m in area.surfaces.road_lines:
            width_mm = max(width_m * plate.mm_per_m, s.min_road_width_mm)
            parts.append(plate.to_mm(line).buffer(width_mm / 2.0,
                                                 cap_style=2, join_style=1))
        roads_mm = sanitize(union_all(parts).intersection(clip),
                            min_area=min_area, simplify=simp)

    # Tree rows: buffer to a canopy width, then treat like woods.
    if s.include_trees and area.surfaces.tree_rows:
        row_w = max(4.0 * plate.mm_per_m, s.min_road_width_mm)
        rows = [plate.to_mm(line).buffer(row_w / 2.0, cap_style=1, join_style=1)
                for line in area.surfaces.tree_rows]
        woods_mm = sanitize(union_all([woods_mm, *rows]).intersection(clip),
                            min_area=min_area, simplify=simp)

    # Buildings, minus the ones the user hid to make room for their own design.
    # Water is cut out of the footprints: the base slab is one layer band lower
    # than the land, so a building left hanging over a canal would float.
    hidden = set(project.hidden_building_ids)
    building_items: list[tuple[Polygon, float]] = []
    if s.include_buildings and not detailed:
        for b in area.buildings:
            if b.id in hidden:
                continue
            geom = plate.to_mm(b.footprint_rd).intersection(clip)
            if not water_mm.is_empty:
                geom = geom.difference(water_mm)
            geom = sanitize(geom, min_area=min_area, simplify=simp)
            if geom.is_empty:
                continue
            h = b.height_m * plate.mm_per_m * s.height_exaggeration
            h = min(max(h, s.min_building_height_mm), s.max_building_height_mm)
            for part in geom.geoms:
                building_items.append((part, h))

    buildings_union = union_all([p for p, _ in building_items])
    recessed = s.road_style == "recessed"

    # A footprint union for the detailed path too, so vegetation and roads still
    # know where the buildings are even though those meshes come pre-built.
    if s.include_buildings and detailed:
        det_foot = [plate.to_mm(b.footprint_rd).intersection(clip)
                    for b in detailed if b.id not in hidden]
        buildings_union = sanitize(union_all([buildings_union, *det_foot]))

    # Roads never run over water, and never under a building - in recessed mode a
    # groove crossing a footprint would leave that part unsupported.
    for cutter in (water_mm, buildings_union):
        if cutter.is_empty or roads_mm.is_empty:
            continue
        roads_mm = sanitize(roads_mm.difference(cutter), min_area=min_area)

    # Vegetation sits ON the land, so keep the land solid underneath it. It must
    # not float over water, a recessed road groove, or a building though.
    veg_cut = [water_mm, buildings_union]
    if recessed:
        veg_cut.append(roads_mm)
    veg_exclude = union_all([g for g in veg_cut if not g.is_empty])
    if not veg_exclude.is_empty:
        woods_mm = sanitize(woods_mm.difference(veg_exclude), min_area=min_area)
        shrubs_mm = sanitize(shrubs_mm.difference(veg_exclude), min_area=min_area)
    # Where trees and shrubs overlap, the taller trees win.
    if not woods_mm.is_empty and not shrubs_mm.is_empty:
        shrubs_mm = sanitize(shrubs_mm.difference(woods_mm), min_area=min_area)

    # Tree points -> little cones, dropped only where they land on the plate.
    tree_pts_mm: list[tuple[float, float]] = []
    if s.include_trees:
        blocked = veg_exclude
        for x_rd, y_rd in area.surfaces.tree_points:
            pt = plate.point_to_mm(x_rd, y_rd)
            p = Point(pt)
            if not clip.contains(p):
                continue
            if not blocked.is_empty and blocked.contains(p):
                continue
            tree_pts_mm.append(pt)

    # --- work out the land layer -----------------------------------------
    land = clip
    if not water_mm.is_empty:
        land = land.difference(water_mm)
    if recessed and not roads_mm.is_empty:
        land = land.difference(roads_mm)
    land_mm = sanitize(land, min_area=min_area)

    # --- extrude ----------------------------------------------------------
    road_z0 = s.base_top if recessed else s.land_top
    tree_top = round(s.land_top + s.tree_height_mm, 4)
    shrub_top = round(s.land_top + s.shrub_height_mm, 4)

    tree_mesh = extrude(woods_mm, s.land_top, tree_top)
    cones = tree_cones(tree_pts_mm, s.land_top, s.tree_height_mm,
                       s.tree_radius_mm) if tree_pts_mm else None
    if tree_mesh is not None and cones is not None:
        import trimesh
        tree_mesh = trimesh.util.concatenate([tree_mesh, cones])
    elif cones is not None:
        tree_mesh = cones

    bodies: list[Body | None] = [
        _body("base", extrude(clip, 0.0, s.base_top), s),
        _body("roads", extrude(roads_mm, road_z0, s.road_top), s),
        _body("land", extrude(land_mm, s.base_top, s.land_top), s),
        _body("shrubs", extrude(shrubs_mm, s.land_top, shrub_top), s),
        _body("trees", tree_mesh, s),
    ]

    if s.include_buildings and detailed:
        bodies.append(_body("buildings",
                            _detailed_buildings_mesh(detailed, hidden, plate, clip, s), s))
    else:
        bodies.append(_body("buildings", extrude_varying(building_items, s.land_top), s))

    if design is not None and project.design.enabled:
        if project.design.anchor_lonlat:
            anchor_rd = wgs84_to_rd(*project.design.anchor_lonlat)
        else:
            anchor_rd = _default_anchor(area, hidden)
        anchor_mm = plate.point_to_mm(*anchor_rd)
        mesh = place_design(
            design,
            scale_mm_per_m=plate.mm_per_m,
            anchor_mm=anchor_mm,
            z_base_mm=s.land_top,
            rotation_deg=project.design.rotation_deg,
            relative_scale=project.design.relative_scale,
            z_offset_mm=project.design.z_offset_mm,
        )
        bodies.append(_body("design", mesh, s, slot_override=project.design.slot))

    live = [b for b in bodies if b is not None and not b.is_empty]

    bands = [{
        "key": b.key, "label": b.label, "color": b.color, "slot": b.slot,
        "z0": _band_z(b.key, s)[0], "z1": _band_z(b.key, s)[1],
        "triangles": b.triangles,
    } for b in live]

    return BuiltModel(
        bodies=live,
        plate=plate,
        bands=bands,
        layers={
            "water": water_mm, "shrubs": shrubs_mm, "woods": woods_mm,
            "roads": roads_mm, "land": land_mm,
            "buildings": buildings_union, "plate": clip,
        },
        stats={
            "plate_mm": [round(plate.width_mm, 1), round(plate.height_mm, 1)],
            "scale_ratio": plate.ratio_label,
            "total_height_mm": round(_total_height(live), 2),
            "triangles": sum(b.triangles for b in live),
            "filament_changes": estimate_changes(live, s),
        },
    )


def _band_z(key: str, s) -> tuple[float, float]:
    recessed = s.road_style == "recessed"
    return {
        "base": (0.0, s.base_top),
        "roads": (s.base_top if recessed else s.land_top, s.road_top),
        "land": (s.base_top, s.land_top),
        "shrubs": (s.land_top, round(s.land_top + s.shrub_height_mm, 4)),
        "trees": (s.land_top, round(s.land_top + s.tree_height_mm, 4)),
        "buildings": (s.land_top, None),
        "design": (s.land_top, None),
    }.get(key, (0.0, 0.0))


def _total_height(bodies: list[Body]) -> float:
    return max((float(b.mesh.bounds[1][2]) for b in bodies if b.mesh is not None), default=0.0)


def _default_anchor(area: AreaData, hidden: set[str]) -> tuple[float, float]:
    """Centre of the hidden buildings, so a replacement lands where the old one was."""
    picked = [b.footprint_rd for b in area.buildings if b.id in hidden]
    if picked:
        c = union_all(picked).centroid
        return (c.x, c.y)
    minx, miny, maxx, maxy = area.bbox_rd
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)


def estimate_changes(bodies: list[Body], s) -> int:
    """How many filament swaps the CFS will actually perform.

    This is the number that decides whether the print costs 40 g of purge or
    2 kg, so it is worth showing before anyone hits export.
    """
    lh = max(s.layer_height, 0.01)
    top = _total_height(bodies)
    if top <= 0:
        return 0

    spans = []
    for b in bodies:
        if b.mesh is None:
            continue
        z0 = float(b.mesh.bounds[0][2])
        z1 = float(b.mesh.bounds[1][2])
        spans.append((z0, z1, b.slot))

    changes = 0
    previous: list[int] = []
    n_layers = int(math.ceil(top / lh))
    for i in range(n_layers):
        z_mid = i * lh + lh / 2.0
        slots = sorted({slot for z0, z1, slot in spans if z0 - 1e-6 <= z_mid <= z1 + 1e-6})
        if not slots:
            continue
        if previous:
            # Swap in if this layer starts on a different slot than the last ended on.
            changes += 1 if slots[0] != previous[-1] else 0
        changes += max(0, len(slots) - 1)
        previous = slots
    return changes


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_model(model: BuiltModel, out_dir: Path, prefix: str,
                 bed_size_mm: float = 300.0) -> list[dict]:
    from .meshing import write_3mf, write_stls

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}_*"):
        old.unlink(missing_ok=True)

    files: list[Path] = write_stls(model.bodies, out_dir, prefix)
    threemf = write_3mf(model.bodies, out_dir / f"{prefix}.3mf", object_name=prefix,
                        bed_size_mm=bed_size_mm)
    if threemf:
        files.insert(0, threemf)

    files.append(_write_readme(model, out_dir, prefix))

    return [{
        "name": p.name,
        "size_kb": round(p.stat().st_size / 1024, 1),
    } for p in files]


def _write_readme(model: BuiltModel, out_dir: Path, prefix: str) -> Path:
    """Drop a note next to the export explaining how to load it.

    Loading the STLs as separate objects cannot work: a slicer drops every
    object onto the bed, which flattens the whole Z-stack. That trips everyone
    up once, so it is written down right where the files are.
    """
    rows = []
    for band in model.bands:
        z1 = "top" if band["z1"] is None else f"{band['z1']:.1f} mm"
        rows.append(f"  slot {band['slot']}  {band['label']:<24} "
                    f"{band['z0']:.1f} - {z1}")

    stl_names = [f"{prefix}_{b.slot}_{b.key}.stl" for b in model.bodies]
    first, *rest = stl_names
    rest_block = chr(10).join(f"       - {name}" for name in rest) or "       (geen)"

    from .appinfo import APP_NAME

    text = f"""{prefix} - {APP_NAME}

Plaat        : {model.stats['plate_mm'][0]} x {model.stats['plate_mm'][1]} mm
Totale hoogte: {model.stats['total_height_mm']} mm
Schaal       : {model.stats['scale_ratio']}
Filamentwissels: {model.stats['filament_changes']}

KLEURLAGEN (van onder naar boven)
{chr(10).join(rows)}

ZO LAAD JE HET IN CREALITY PRINT
--------------------------------
Gebruik {prefix}.3mf. Slepen en loslaten, klaar. Alle onderdelen zitten er
als losse parts in, op de juiste hoogte, met de filamentsloten al toegewezen.

DE STL'S (alleen als het 3MF niet werkt)
----------------------------------------
Laad ze NIET alle vier los in: de slicer legt elk object plat op het bed en
dan staat de hele stapeling door elkaar. Dat is precies wat je niet wilt.

Doe het zo:
  1. Laad alleen {first}
  2. Rechtermuisknop op dat object -> "Add part" / "Onderdeel toevoegen" -> "Load"
  3. Kies daar de andere onderdelen:
{rest_block}
  4. Wijs per part het filamentslot toe. Het cijfer in de bestandsnaam is
     het slotnummer.

Als part behouden de bestanden hun eigen hoogte; als los object niet.

TIP VOOR DE CFS
---------------
Zet "flush into infill" en "flush into object" aan. Dat scheelt het meeste
purgemateriaal bij het wisselen.
"""
    path = out_dir / f"{prefix}_LEESMIJ.txt"
    path.write_text(text, encoding="utf-8")
    return path
