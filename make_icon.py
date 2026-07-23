"""Generate the application icon: the four stacked colour bands of a map plate.

    .venv\\Scripts\\python.exe make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path("packaging/app.ico")

# Same colours the bands use in the UI, bottom to top.
WATER = (47, 111, 159)
LAND = (125, 154, 106)
ROAD = (232, 228, 217)
BUILDING = (183, 100, 60)
BACKDROP = (28, 32, 39)


def draw(size: int) -> Image.Image:
    """An isometric stack of plates, seen from the front-left."""
    s = size * 4  # supersample, then downscale for clean edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(s * 0.16)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BACKDROP)

    cx = s / 2
    half_w = s * 0.36
    half_h = s * 0.18
    top = s * 0.30
    step = s * 0.115

    def plate(y: float, colour: tuple[int, int, int], inset: float = 0.0) -> None:
        w = half_w * (1.0 - inset)
        h = half_h * (1.0 - inset)
        d.polygon([(cx, y - h), (cx + w, y), (cx, y + h), (cx - w, y)], fill=colour)

    # Bottom up, so each plate overlaps the one below it.
    plate(top + step * 3, WATER)
    plate(top + step * 2, LAND)
    plate(top + step * 1, ROAD, inset=0.14)
    plate(top + step * 0, BUILDING, inset=0.34)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    # Pillow builds every entry by downscaling the image it is given, so hand it
    # the largest one. Saving the 16px version first yields a 16px-only icon.
    master = draw(256)
    master.save(OUT, format="ICO", sizes=[(n, n) for n in sizes])
    print(f"geschreven: {OUT.resolve()} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
