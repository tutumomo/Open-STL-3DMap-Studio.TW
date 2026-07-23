"""Print design settings and the colour-band layout.

The whole design principle of this tool: every colour gets its own Z band so the
CFS only has to swap filament a handful of times for the entire print, instead of
twice per layer. See README.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# EPSG:28992 = Rijksdriehoek (RD New). Everything server-side is metres in RD.
CRS_RD = "EPSG:28992"
CRS_WGS84 = "EPSG:4326"

RoadStyle = Literal["recessed", "raised"]


@dataclass
class PrintSettings:
    """Everything the user can tune before exporting."""

    # --- overall size -----------------------------------------------------
    max_size_mm: float = 200.0
    """Longest edge of the finished plate. The selected area is scaled to fit."""

    layer_height: float = 0.2
    first_layer_height: float = 0.2

    bed_size_mm: float = 300.0
    """Used to centre the model on the plate in the 3MF. K1 Max is 300x300."""

    # --- colour band heights (mm, measured from the build plate) ----------
    base_top: float = 1.2
    """Top of the solid base slab. Prints in the water colour; only visible
    through the holes in the land layer, plus as a stripe around the edge."""

    road_top: float = 1.7
    """Top of the road band. Below land_top => roads look engraved."""

    land_top: float = 2.0
    """Top of the land layer. Buildings and your own design sit on this."""

    # --- buildings --------------------------------------------------------
    height_exaggeration: float = 1.5
    """1.0 = true to scale. Dutch cities are flat, so a little lift reads better.
    Push it to 2-3x for areas above ~2 km, where true scale goes flat."""

    min_building_height_mm: float = 0.6
    max_building_height_mm: float = 30.0
    """Clamp, so one cathedral does not triple the print time of the whole map."""

    detailed_buildings: bool = False
    """Off: fast flat blocks from the 2D WFS. On: real LoD2.2 roof shapes from
    the slower 3DBAG API - minutes for a whole city, but true rooflines."""

    # --- vegetation -------------------------------------------------------
    shrub_height_mm: float = 1.0
    """Low green (scrub, heath, grass, parks) rises this far above the land."""

    tree_height_mm: float = 3.0
    """Trees and woods rise this far above the land - taller than the shrubs so
    the two read apart even when they share a filament colour."""

    tree_radius_mm: float = 0.9
    """Printed radius of a single mapped tree, rendered as a small cone."""

    # --- feature filtering (all in final mm, i.e. after scaling) ----------
    min_road_width_mm: float = 0.9
    """A 0.4 nozzle cannot render anything thinner. Narrow streets get widened."""

    min_feature_area_mm2: float = 0.8
    """Drop specks smaller than this so the slicer does not choke on them."""

    simplify_mm: float = 0.06
    """Douglas-Peucker tolerance. Keeps triangle counts sane."""

    # --- content ----------------------------------------------------------
    road_style: RoadStyle = "recessed"

    include_water: bool = True
    include_roads: bool = True
    include_buildings: bool = True
    include_shrubs: bool = False
    include_trees: bool = False

    # Per-body CFS slot override, body-key -> slot (1..N). Empty = use the
    # default from DEFAULT_BANDS. The CFS-C has 4 slots; using more is allowed
    # (the UI warns) since you can still print it with a manual swap.
    slots: dict[str, int] = field(default_factory=dict)

    def slot_for(self, key: str) -> int:
        spec = BANDS_BY_KEY.get(key)
        default = spec.slot if spec else 4
        return int(self.slots.get(key, default))

    def snapped(self) -> "PrintSettings":
        """Round every band boundary to a whole number of layers.

        A band boundary that falls mid-layer makes the slicer round it silently,
        which shifts a colour change by a layer and looks like a bug in the model.
        """
        out = PrintSettings(**asdict(self))
        lh = max(self.layer_height, 0.01)

        def snap(z: float, minimum: float) -> float:
            # Layer N ends at first_layer_height + (N-1)*lh.
            n = max(1, round((z - self.first_layer_height) / lh) + 1)
            return round(self.first_layer_height + (n - 1) * lh, 4)

        out.base_top = snap(self.base_top, lh)
        out.road_top = snap(self.road_top, lh)
        out.land_top = snap(self.land_top, lh)

        # Keep the ordering valid even if the user typed something odd.
        out.base_top = max(out.base_top, self.first_layer_height)
        out.land_top = max(out.land_top, round(out.base_top + lh, 4))
        if self.road_style == "recessed":
            out.road_top = min(max(out.road_top, round(out.base_top + lh, 4)),
                               round(out.land_top - lh, 4))
        else:
            out.road_top = max(out.road_top, round(out.land_top + lh, 4))
        return out


@dataclass
class BandSpec:
    """One printable body = one filament slot."""

    key: str
    label: str
    color: str  # hex, used by the web UI and written into the 3MF
    slot: int  # CFS slot 1..4


# Default slot assignment. Order matters: it is the print order bottom to top.
# Shrubs and trees default to slot 2 (share the land colour) so a fresh map
# still fits the 4-slot CFS; the user can move them onto their own slot.
DEFAULT_BANDS: list[BandSpec] = [
    BandSpec("base", "Water / onderplaat", "#2f6f9f", 1),
    BandSpec("roads", "Wegen", "#e8e4d9", 3),
    BandSpec("land", "Land / maaiveld", "#7d9a6a", 2),
    BandSpec("shrubs", "Struiken / laag groen", "#6f9a52", 2),
    BandSpec("trees", "Bomen / bos", "#3f6a2e", 2),
    BandSpec("buildings", "Gebouwen", "#b7643c", 4),
    BandSpec("design", "Eigen ontwerp (IFC)", "#d9b310", 4),
]

BANDS_BY_KEY = {b.key: b for b in DEFAULT_BANDS}


@dataclass
class DesignPlacement:
    """A user model (IFC/STL/OBJ) dropped into the map."""

    filename: str = ""
    # Where it lands, as lon/lat so the browser can set it directly.
    # None => centroid of the hidden buildings.
    anchor_lonlat: tuple[float, float] | None = None
    rotation_deg: float = 0.0
    # 1.0 = print the design at the same scale as the map around it.
    relative_scale: float = 1.0
    z_offset_mm: float = 0.0
    slot: int = 4
    enabled: bool = True


@dataclass
class Project:
    """Serialisable state of one map."""

    id: str = ""
    name: str = "Naamloos"
    bbox_wgs84: tuple[float, float, float, float] | None = None  # w,s,e,n
    settings: PrintSettings = field(default_factory=PrintSettings)
    hidden_building_ids: list[str] = field(default_factory=list)
    design: DesignPlacement = field(default_factory=DesignPlacement)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Project":
        settings = PrintSettings(**(d.get("settings") or {}))
        design_d = {k: v for k, v in (d.get("design") or {}).items()
                    if k in DesignPlacement.__dataclass_fields__}
        anchor = design_d.get("anchor_lonlat")
        design = DesignPlacement(**{**design_d,
                                    "anchor_lonlat": tuple(anchor) if anchor else None})
        bbox = d.get("bbox_wgs84")
        return Project(
            id=d.get("id", ""),
            name=d.get("name", "Naamloos"),
            bbox_wgs84=tuple(bbox) if bbox else None,
            settings=settings,
            hidden_building_ids=list(d.get("hidden_building_ids") or []),
            design=design,
        )
