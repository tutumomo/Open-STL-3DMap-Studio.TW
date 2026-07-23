"""Turn 2D millimetre polygons into printable solids and write STL / 3MF.

Every body is a set of flat-topped prisms. Because the shapely geometry is
unioned before it gets here, prisms inside one body never overlap, which keeps
the result sliceable even though the body as a whole is not a single manifold.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import trimesh
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)


@dataclass
class Body:
    """One printable object = one filament slot."""

    key: str
    label: str
    color: str
    slot: int
    mesh: trimesh.Trimesh | None = None

    @property
    def triangles(self) -> int:
        return 0 if self.mesh is None else len(self.mesh.faces)

    @property
    def is_empty(self) -> bool:
        return self.mesh is None or len(self.mesh.faces) == 0


def _polygons(geom: BaseGeometry | None) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]


def extrude(geom: BaseGeometry | None, z_bottom: float, z_top: float) -> trimesh.Trimesh | None:
    """Extrude every polygon in `geom` from z_bottom to z_top."""
    height = z_top - z_bottom
    if height <= 0:
        return None

    meshes: list[trimesh.Trimesh] = []
    for poly in _polygons(geom):
        try:
            m = trimesh.creation.extrude_polygon(poly, height)
        except Exception as exc:  # triangulation genuinely fails on degenerate rings
            log.debug("skipping polygon that failed to triangulate: %s", exc)
            continue
        if z_bottom:
            m.apply_translation((0.0, 0.0, z_bottom))
        meshes.append(m)

    return _combine(meshes)


def extrude_varying(items: list[tuple[Polygon, float]], z_bottom: float) -> trimesh.Trimesh | None:
    """Extrude each polygon to its own height (buildings)."""
    meshes: list[trimesh.Trimesh] = []
    for poly, height in items:
        if height <= 0 or poly.is_empty:
            continue
        for part in _polygons(poly):
            try:
                m = trimesh.creation.extrude_polygon(part, height)
            except Exception as exc:
                log.debug("skipping building that failed to triangulate: %s", exc)
                continue
            m.apply_translation((0.0, 0.0, z_bottom))
            meshes.append(m)
    return _combine(meshes)


def _combine(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh | None:
    if not meshes:
        return None
    if len(meshes) == 1:
        return meshes[0]
    return trimesh.util.concatenate(meshes)


def tree_cones(points_mm: list[tuple[float, float]], z_base: float,
               height: float, radius: float, sections: int = 8) -> trimesh.Trimesh | None:
    """A little cone per mapped tree, standing on the land."""
    meshes: list[trimesh.Trimesh] = []
    for x, y in points_mm:
        cone = trimesh.creation.cone(radius=radius, height=height, sections=sections)
        cone.apply_translation((x, y, z_base))
        meshes.append(cone)
    return _combine(meshes)


def mesh_from_arrays(vertices, faces) -> trimesh.Trimesh | None:
    """Wrap raw (vertices, faces) - e.g. a 3DBAG LoD2.2 solid - as a mesh."""
    if vertices is None or len(vertices) == 0 or faces is None or len(faces) == 0:
        return None
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def write_stls(bodies: list[Body], out_dir: Path, prefix: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for body in bodies:
        if body.is_empty:
            continue
        path = out_dir / f"{prefix}_{body.slot}_{body.key}.stl"
        body.mesh.export(path)
        written.append(path)
    return written


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
<Default Extension="config" ContentType="application/octet-stream"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

_MODEL_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Target="/3D/Objects/object-1.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

_MODEL_NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
    'requiredextensions="p"'
)
_OBJECTS_PATH = "/3D/Objects/object-1.model"
_MATERIALS_ID = 1000

# Neutral grey for filament slots no body actually uses.
_UNUSED_SLOT_COLOR = "#B0B0B0"


def _uuid(group: int, index: int) -> str:
    """Deterministic UUIDs. The production extension requires them to exist."""
    return f"00000000-0000-4000-8000-{group:02d}{index:010d}"


def write_3mf(bodies: list[Body], path: Path, object_name: str = "3D kaart",
              bed_size_mm: float = 300.0) -> Path | None:
    """Write a 3MF that Creality Print / OrcaSlicer opens as one object with parts.

    Creality Print descends from Bambu Studio, not from PrusaSlicer, so the part
    layout it understands is: every part is its own <object> inside
    3D/Objects/object-1.model, tied together by <components> in 3dmodel.model,
    with the filament slots listed in Metadata/model_settings.config. Writing
    PrusaSlicer's Slic3r_PE_model.config instead makes it import as one lump.

    Geometry is centred on the origin and the build item translates it to the
    middle of the bed, the same way Bambu Studio writes it.
    """
    live = [b for b in bodies if not b.is_empty]
    if not live:
        return None

    # Centre the whole assembly on the origin.
    mins = np.min([b.mesh.bounds[0] for b in live], axis=0)
    maxs = np.max([b.mesh.bounds[1] for b in live], axis=0)
    shift = np.array([(mins[0] + maxs[0]) / 2.0, (mins[1] + maxs[1]) / 2.0, 0.0])

    # One <base> per filament slot; pindex is a 0-based index into this list.
    max_slot = max(b.slot for b in live)
    slot_colors = {}
    for body in live:
        slot_colors.setdefault(body.slot, body.color)
    bases = "".join(
        f'<base name="filament_{slot}" '
        f'displaycolor="{slot_colors.get(slot, _UNUSED_SLOT_COLOR)}"/>'
        for slot in range(1, max_slot + 1)
    )

    object_chunks: list[str] = []
    component_lines: list[str] = []
    part_lines: list[str] = []

    for index, body in enumerate(live, start=1):
        mesh = body.mesh
        verts = np.asarray(mesh.vertices, dtype=np.float64) - shift
        faces = np.asarray(mesh.faces, dtype=np.int64)

        vertex_xml = "".join(
            f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts
        )
        triangle_xml = "".join(
            f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces
        )
        object_chunks.append(
            f'<object id="{index}" type="model" p:UUID="{_uuid(3, index)}" '
            f'name="{escape(body.label)}" pid="{_MATERIALS_ID}" pindex="{body.slot - 1}">'
            f"<mesh><vertices>{vertex_xml}</vertices>"
            f"<triangles>{triangle_xml}</triangles></mesh></object>"
        )
        component_lines.append(
            f'  <component p:path="{_OBJECTS_PATH}" objectid="{index}"/>'
        )
        part_lines.append(
            f'  <part id="{index}" subtype="normal_part">\n'
            f'   <metadata key="name" value="{escape(body.label)}"/>\n'
            f'   <metadata key="extruder" value="{body.slot}"/>\n'
            f"  </part>"
        )

    objects_model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<model unit=\"millimeter\" xml:lang=\"en-US\" {_MODEL_NS}>"
        '<metadata name="BambuStudio:3mfVersion">1</metadata>'
        f'<resources><basematerials id="{_MATERIALS_ID}">{bases}</basematerials>'
        + "".join(object_chunks)
        + "</resources></model>"
    )

    centre = bed_size_mm / 2.0
    root_model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" {_MODEL_NS}>
<metadata name="Application">3D Kaart Bouwer NL</metadata>
<metadata name="BambuStudio:3mfVersion">1</metadata>
<metadata name="Title">{escape(object_name)}</metadata>
<resources>
 <object id="1" type="model" p:UUID="{_uuid(1, 1)}" name="{escape(object_name)}">
  <components>
{chr(10).join(component_lines)}
  </components>
 </object>
</resources>
<build>
 <item objectid="1" p:UUID="{_uuid(2, 1)}" transform="1 0 0 0 1 0 0 0 1 {centre:.5f} {centre:.5f} 0" printable="1"/>
</build>
</model>
"""

    settings = f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
 <object id="1">
  <metadata key="name" value="{escape(object_name)}"/>
  <metadata key="extruder" value="{live[0].slot}"/>
{chr(10).join(part_lines)}
 </object>
</config>
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("3D/3dmodel.model", root_model)
        z.writestr("3D/_rels/3dmodel.model.rels", _MODEL_RELS)
        z.writestr("3D/Objects/object-1.model", objects_model)
        z.writestr("Metadata/model_settings.config", settings)
    return path
