"""End-to-end check without the browser: fetch a real area, build, export.

    .venv\\Scripts\\python.exe smoketest.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from app.config import Project
from app.pipeline import area_stats, build_model, export_model, load_area, preview_geojson

# Domplein, Utrecht - dense, has water (Oudegracht), roads and tall buildings.
BBOX = (5.1155, 52.0875, 5.1265, 52.0940)


def main() -> int:
    project = Project.from_dict({
        "id": "smoketest",
        "name": "Smoketest Utrecht",
        "bbox_wgs84": BBOX,
        "settings": {"max_size_mm": 180.0, "height_exaggeration": 2.0},
    })

    t0 = time.time()
    area = load_area(project)
    print(f"fetched in {time.time() - t0:.1f}s")
    stats = area_stats(area, project)
    for k, v in stats.items():
        print(f"  {k:18} {v}")

    if not area.buildings:
        print("FAIL: geen gebouwen opgehaald")
        return 1

    # Exercise the map preview too - this is the /api/area path, and a bad
    # geometry here (e.g. tree points) fails the whole area load in the UI.
    preview = preview_geojson(area, project)
    kinds = {}
    for f in preview["features"]:
        k = f["properties"].get("kind", "building")
        kinds[k] = kinds.get(k, 0) + 1
    print(f"preview features: {kinds}")
    if not preview["features"]:
        print("FAIL: lege preview")
        return 1

    # Hide a couple of buildings to exercise that path.
    project.hidden_building_ids = [b.id for b in area.buildings[:3]]
    project.settings.include_shrubs = True
    project.settings.include_trees = True

    t0 = time.time()
    model = build_model(area, project)
    print(f"built in {time.time() - t0:.1f}s")

    for band in model.bands:
        z1 = "top" if band["z1"] is None else f"{band['z1']:.2f}"
        print(f"  slot {band['slot']}  {band['key']:10} z {band['z0']:.2f}-{z1:>5}  "
              f"{band['triangles']:>7} tris")
    print("  stats:", model.stats)

    for body in model.bodies:
        b = body.mesh.bounds
        print(f"  {body.key:10} x {b[0][0]:7.2f}..{b[1][0]:7.2f}  "
              f"y {b[0][1]:7.2f}..{b[1][1]:7.2f}  z {b[0][2]:6.2f}..{b[1][2]:6.2f}")

    out = Path("output/smoketest")
    files = export_model(model, out, "smoketest")
    print("wrote:")
    for f in files:
        print(f"  {f['name']:34} {f['size_kb']:>9} kB")

    # Sanity: the plate must not exceed the requested maximum.
    w, h = model.stats["plate_mm"]
    if max(w, h) > project.settings.max_size_mm + 0.01:
        print(f"FAIL: plaat {w}x{h} groter dan max {project.settings.max_size_mm}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
