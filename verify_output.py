"""Check the exported 3MF is well formed and that design placement works.

    .venv\\Scripts\\python.exe verify_output.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import trimesh

from app.config import Project
from app.ifc_import import load_design
from app.pipeline import build_model, load_area

THREEMF = Path("output/smoketest/smoketest.3mf")
BBOX = (5.1155, 52.0875, 5.1265, 52.0940)
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


CORE_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
           "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"}
PATH_ATTR = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"


def verify_3mf() -> None:
    """Check the archive matches the layout Bambu Studio / Orca / Creality read.

    The PrusaSlicer layout (one merged mesh plus triangle ranges in
    Slic3r_PE_model.config) loads in Creality Print as a single object with no
    filament assignment, which is exactly the bug this replaces.
    """
    print("3MF-structuur (Orca/Bambu-indeling):")
    check(THREEMF.exists(), f"{THREEMF} bestaat")
    if not THREEMF.exists():
        return

    with zipfile.ZipFile(THREEMF) as z:
        names = set(z.namelist())
        for required in ("[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model",
                         "3D/_rels/3dmodel.model.rels", "3D/Objects/object-1.model",
                         "Metadata/model_settings.config"):
            check(required in names, f"bevat {required}")
        check("Metadata/Slic3r_PE_model.config" not in names,
              "geen PrusaSlicer-config meer (die zorgde voor 1 object)")

        root = ET.fromstring(z.read("3D/3dmodel.model"))
        objects_model = ET.fromstring(z.read("3D/Objects/object-1.model"))
        config = ET.fromstring(z.read("Metadata/model_settings.config"))

    components = root.findall(".//m:components/m:component", CORE_NS)
    check(len(components) >= 2, f"{len(components)} componenten in het hoofdobject")
    check(all(c.get(PATH_ATTR) == "/3D/Objects/object-1.model" for c in components),
          "elke component verwijst naar /3D/Objects/object-1.model")

    item = root.find(".//m:build/m:item", CORE_NS)
    check(item is not None and item.get("transform") is not None,
          f"build-item plaatst het model op het bed ({item.get('transform') if item is not None else '?'})")

    # Each component must resolve to a real object carrying its own mesh.
    objects = {o.get("id"): o for o in objects_model.findall(".//m:object", CORE_NS)}
    component_ids = [c.get("objectid") for c in components]
    check(all(cid in objects for cid in component_ids),
          "elke component wijst naar een bestaand object")

    total_triangles = 0
    for cid in component_ids:
        obj = objects[cid]
        tris = obj.findall(".//m:triangles/m:triangle", CORE_NS)
        total_triangles += len(tris)
        check(len(tris) > 0 and obj.get("pindex") is not None,
              f"object {cid} '{obj.get('name')}' heeft {len(tris)} driehoeken, pindex={obj.get('pindex')}")

    bases = objects_model.findall(".//m:basematerials/m:base", CORE_NS)
    check(len(bases) >= len(set(o.get("pindex") for o in objects.values())),
          f"{len(bases)} filamentkleuren gedefinieerd")

    # The part list is what actually assigns the filament slots.
    parts = config.findall(".//part")
    check(len(parts) == len(components),
          f"{len(parts)} parts in model_settings.config, evenveel als componenten")
    check([p.get("id") for p in parts] == component_ids,
          "part-ids komen overeen met de component-ids")

    for part in parts:
        extruder = part.find("./metadata[@key='extruder']")
        name = part.find("./metadata[@key='name']")
        ok = (extruder is not None and extruder.get("value").isdigit()
              and part.get("subtype") == "normal_part")
        check(ok, f"part {part.get('id')} '{name.get('value') if name is not None else '?'}'"
                  f" -> extruder {extruder.get('value') if extruder is not None else '?'}")

    # pindex is zero-based, extruder is one-based; a mismatch prints the wrong colour.
    aligned = all(
        objects[p.get("id")].get("pindex") ==
        str(int(p.find("./metadata[@key='extruder']").get("value")) - 1)
        for p in parts
    )
    check(aligned, "pindex komt overeen met extruder - 1")

    # trimesh cannot read this layout - its loader only ever opens
    # 3D/3dmodel.model and never follows p:path - so verify the geometry
    # straight out of the objects file instead.
    print("  -- geometrie in het bestand:")
    zs: dict[str, tuple[float, float]] = {}
    all_x: list[float] = []
    all_y: list[float] = []
    for cid in component_ids:
        obj = objects[cid]
        verts = obj.findall(".//m:vertices/m:vertex", CORE_NS)
        z_values = [float(v.get("z")) for v in verts]
        all_x += [float(v.get("x")) for v in verts]
        all_y += [float(v.get("y")) for v in verts]
        zs[obj.get("name")] = (min(z_values), max(z_values))
        print(f"       {obj.get('name'):22} z {min(z_values):5.2f} - {max(z_values):5.2f} mm")

    check(total_triangles > 1000, f"{total_triangles} driehoeken in totaal")

    base = zs.get("Water / onderplaat")
    land = zs.get("Land / maaiveld")
    buildings = zs.get("Gebouwen")
    if base and land and buildings:
        check(abs(base[0]) < 1e-6, "onderplaat begint op z=0")
        check(abs(land[0] - base[1]) < 1e-6,
              f"maaiveld sluit aan op de onderplaat ({land[0]:.2f} = {base[1]:.2f})")
        check(abs(buildings[0] - land[1]) < 1e-6,
              f"gebouwen staan op het maaiveld ({buildings[0]:.2f} = {land[1]:.2f})")

    # Bambu-style files centre the geometry and place it with the build item.
    check(abs((min(all_x) + max(all_x)) / 2) < 0.01 and abs((min(all_y) + max(all_y)) / 2) < 0.01,
          "geometrie is gecentreerd op de oorsprong")


def verify_design_placement() -> None:
    print("\nPlaatsing eigen ontwerp:")
    stl = Path("output/smoketest/smoketest_4_buildings.stl")
    if not stl.exists():
        check(False, "geen test-STL beschikbaar; draai eerst smoketest.py")
        return

    design = load_design(stl, units="mm")
    check(design.triangle_count > 0, f"ontwerp geladen ({design.triangle_count} driehoeken)")

    project = Project.from_dict({
        "id": "verify", "name": "verify", "bbox_wgs84": BBOX,
        "settings": {"max_size_mm": 180.0},
        # Anchor deliberately off-centre to prove the placement actually moves it.
        "design": {"filename": stl.name, "anchor_lonlat": [5.1200, 52.0900],
                   "rotation_deg": 30.0, "relative_scale": 1.0, "slot": 4},
    })

    area = load_area(project)
    model = build_model(area, project, design)

    body = next((b for b in model.bodies if b.key == "design"), None)
    check(body is not None, "design-body zit in het model")
    if body is None:
        return

    bounds = body.mesh.bounds
    s = project.settings.snapped()
    check(abs(bounds[0][2] - s.land_top) < 1e-6,
          f"ontwerp staat op maaiveld ({bounds[0][2]:.2f} mm, verwacht {s.land_top:.2f})")

    plate_w, plate_h = model.stats["plate_mm"]
    centre = ((bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2)
    check(0 <= centre[0] <= plate_w and 0 <= centre[1] <= plate_h,
          f"ontwerp ligt binnen de plaat (midden op {centre[0]:.1f}, {centre[1]:.1f} mm)")
    check(body.slot == 4, f"ontwerp gebruikt CFS-slot {body.slot}")


def verify_support() -> None:
    """Nothing may float: every raised body needs solid material underneath."""
    print("\nOndersteuning (niets mag zweven):")
    project = Project.from_dict({
        "id": "verify-support", "name": "verify", "bbox_wgs84": BBOX,
        "settings": {"max_size_mm": 180.0, "road_style": "recessed"},
    })
    model = build_model(load_area(project), project)
    L = model.layers

    # Buildings start at land_top, so land must exist under every footprint.
    floating = L["buildings"].difference(L["land"].buffer(0.01))
    check(floating.area < L["buildings"].area * 0.001,
          f"gebouwen staan op land (zwevend: {floating.area:.2f} van "
          f"{L['buildings'].area:.0f} mm2)")

    # Roads sit on the base slab, which spans the whole plate.
    outside = L["roads"].difference(L["plate"].buffer(0.01))
    check(outside.area < 0.01, f"wegen vallen binnen de plaat ({outside.area:.3f} mm2 buiten)")

    # Land and water must not both claim the same spot.
    overlap = L["land"].intersection(L["water"])
    check(overlap.area < 0.01, f"land en water overlappen niet ({overlap.area:.3f} mm2)")

    # In recessed mode land and roads are cut out of each other.
    road_overlap = L["land"].intersection(L["roads"])
    check(road_overlap.area < 0.01,
          f"land en wegen overlappen niet ({road_overlap.area:.3f} mm2)")


if __name__ == "__main__":
    verify_3mf()
    verify_design_placement()
    verify_support()
    print("\n" + ("ALLES OK" if not failures else f"{len(failures)} PROBLEMEN"))
    sys.exit(1 if failures else 0)
