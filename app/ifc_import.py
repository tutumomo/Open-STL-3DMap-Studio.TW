"""Load a Revit/IFC export (or plain STL/OBJ/3MF) and place it on the map.

IfcOpenShell hands back triangulated world-coordinate geometry in metres, which
is exactly what the map pipeline works in, so placement is the same affine
transform the rest of the model gets.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

log = logging.getLogger(__name__)

MESH_SUFFIXES = {".stl", ".obj", ".3mf", ".ply", ".off", ".glb", ".gltf"}
IFC_SUFFIXES = {".ifc", ".ifczip"}

# Nothing here contributes printable volume; IfcSpace in particular is a solid
# blob filling every room and would swallow the whole building.
IFC_SKIP_TYPES = {"IfcSpace", "IfcOpeningElement", "IfcAnnotation", "IfcGrid", "IfcSite"}


@dataclass
class LoadedDesign:
    mesh: trimesh.Trimesh
    source_name: str
    size_m: tuple[float, float, float]
    triangle_count: int

    def info(self) -> dict:
        return {
            "name": self.source_name,
            "size_m": [round(v, 2) for v in self.size_m],
            "triangles": self.triangle_count,
        }


def load_design(path: Path, *, units: str = "auto") -> LoadedDesign:
    """Load any supported file into a mesh expressed in metres."""
    suffix = path.suffix.lower()

    if suffix in IFC_SUFFIXES:
        mesh = _load_ifc(path)
        scale = 1.0 if units in ("auto", "m") else _unit_scale(units)
    elif suffix in MESH_SUFFIXES:
        mesh = _load_mesh(path)
        # A mesh exported for printing is almost always in millimetres.
        scale = _unit_scale("mm" if units == "auto" else units)
    else:
        raise ValueError(
            f"Bestandstype '{suffix}' wordt niet ondersteund. "
            f"Gebruik IFC, STL, OBJ, 3MF, PLY of GLB."
        )

    if scale != 1.0:
        mesh.apply_scale(scale)

    extents = mesh.extents if mesh.extents is not None else (0.0, 0.0, 0.0)
    return LoadedDesign(
        mesh=mesh,
        source_name=path.name,
        size_m=(float(extents[0]), float(extents[1]), float(extents[2])),
        triangle_count=len(mesh.faces),
    )


def _unit_scale(units: str) -> float:
    return {"mm": 0.001, "cm": 0.01, "m": 1.0}.get(units, 1.0)


def _load_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="mesh")
    if isinstance(loaded, trimesh.Scene):
        loaded = trimesh.util.concatenate(list(loaded.dump()))
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ValueError(f"Geen bruikbare geometrie gevonden in {path.name}")
    return loaded


def _load_ifc(path: Path) -> trimesh.Trimesh:
    try:
        import ifcopenshell
        import ifcopenshell.geom
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise RuntimeError(
            "ifcopenshell is niet geinstalleerd. Exporteer je Revit-model als STL "
            "of installeer ifcopenshell (zie requirements.txt)."
        ) from exc

    ifc_file = ifcopenshell.open(str(path))
    settings = ifcopenshell.geom.settings()
    # The settings API changed between 0.7 and 0.8; support both.
    for setter in (
        lambda: settings.set("use-world-coords", True),
        lambda: settings.set(settings.USE_WORLD_COORDS, True),
    ):
        try:
            setter()
            break
        except Exception:  # noqa: BLE001 - probing which API this build has
            continue

    meshes: list[trimesh.Trimesh] = []
    iterator = ifcopenshell.geom.iterator(settings, ifc_file)
    if not iterator.initialize():
        raise ValueError(f"Geen geometrie gevonden in {path.name}")

    while True:
        shape = iterator.get()
        product = ifc_file.by_id(shape.id) if hasattr(shape, "id") else None
        skip = product is not None and any(product.is_a(t) for t in IFC_SKIP_TYPES)

        if not skip:
            geometry = shape.geometry
            verts = np.asarray(geometry.verts, dtype=np.float64).reshape(-1, 3)
            faces = np.asarray(geometry.faces, dtype=np.int64).reshape(-1, 3)
            if len(verts) and len(faces):
                meshes.append(trimesh.Trimesh(vertices=verts, faces=faces, process=False))

        if not iterator.next():
            break

    if not meshes:
        raise ValueError(f"Geen printbare geometrie in {path.name}")

    combined = trimesh.util.concatenate(meshes)
    # IFC is Z-up in world coords, same as the print bed. No axis swap needed.
    return combined


def place_design(
    design: LoadedDesign,
    *,
    scale_mm_per_m: float,
    anchor_mm: tuple[float, float],
    z_base_mm: float,
    rotation_deg: float = 0.0,
    relative_scale: float = 1.0,
    z_offset_mm: float = 0.0,
) -> trimesh.Trimesh:
    """Scale, rotate and drop the design onto the plate at `anchor_mm`."""
    mesh = design.mesh.copy()

    mesh.apply_scale(scale_mm_per_m * max(relative_scale, 1e-6))

    if rotation_deg:
        rot = trimesh.transformations.rotation_matrix(
            math.radians(rotation_deg), (0.0, 0.0, 1.0)
        )
        mesh.apply_transform(rot)

    bounds = mesh.bounds
    centre_x = (bounds[0][0] + bounds[1][0]) / 2.0
    centre_y = (bounds[0][1] + bounds[1][1]) / 2.0
    mesh.apply_translation((
        anchor_mm[0] - centre_x,
        anchor_mm[1] - centre_y,
        z_base_mm + z_offset_mm - bounds[0][2],
    ))
    return mesh
