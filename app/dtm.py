"""全台 20 m DTM（內政部地政司）讀取與地形網格取樣。

資料來自專案內的 `data/dtm20m/taiwan_penghu_dem_20m.zip`（GeoTIFF，EPSG:3826，
20 m 網格，int16，NODATA = -32767）。為了避免相依於 GDAL/rasterio，這裡直接
解析 TIFF 標籤並以檔案位移讀取需要的視窗——這份檔案的每一個 strip 剛好是一列，
所以「讀某幾列」就是幾次 seek，很快。

解壓時機：第一次用到時才解壓到 `%LOCALAPPDATA%\\<APP_NAME>\\dtm\\`（開發時是
repo 的 `dtm/`），解壓後寫入 marker 記錄 zip 的大小與時間，換版時自動重解。
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .paths import bundled_data_dir, data_dir
from .survey import TerrainGrid

DTM_ZIP_NAME = "taiwan_penghu_dem_20m.zip"
DTM_MAIN_TIF = "dem_20m.tif"
EXTRACT_DIR_NAME = "dtm"
MARKER_NAME = ".extracted.json"

# GeoTIFF 標籤編號
_TAG_WIDTH = 256
_TAG_HEIGHT = 257
_TAG_COMPRESSION = 259
_TAG_STRIP_OFFSETS = 273
_TAG_SAMPLES_PER_PIXEL = 277
_TAG_ROWS_PER_STRIP = 278
_TAG_STRIP_BYTE_COUNTS = 279
_TAG_SAMPLE_FORMAT = 339
_TAG_PIXEL_SCALE = 33550
_TAG_TIEPOINT = 33922
_TAG_GDAL_NODATA = 42113

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
_TYPE_FMT = {1: "B", 3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q"}


class DtmError(RuntimeError):
    """DTM 資料不存在或格式不符。"""


# --------------------------------------------------------------------------- #
# 壓縮檔位置與解壓
# --------------------------------------------------------------------------- #
def archive_path() -> Path:
    """找出 DTM 壓縮檔：先找隨程式附帶的，再找使用者資料夾。"""
    for base in (bundled_data_dir() / "dtm20m", data_dir() / "dtm20m"):
        candidate = base / DTM_ZIP_NAME
        if candidate.is_file():
            return candidate
    raise DtmError(
        f"找不到 DTM 壓縮檔 {DTM_ZIP_NAME}；預期位置：{bundled_data_dir() / 'dtm20m'}"
    )


def extract_root() -> Path:
    return data_dir() / EXTRACT_DIR_NAME


def ensure_extracted(*, force: bool = False) -> Path:
    """第一次使用時解壓，回傳 `dem_20m.tif` 的完整路徑。

    以 zip 的檔案大小與修改時間當版本指紋，換版時自動重新解壓。
    """
    src = archive_path()
    root = extract_root()
    marker = root / MARKER_NAME
    stamp = {"zip": str(src), "size": src.stat().st_size, "mtime": int(src.stat().st_mtime)}

    if not force and marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == stamp:
                found = next(root.rglob(DTM_MAIN_TIF), None)
                if found is not None:
                    return found
        except (json.JSONDecodeError, OSError):
            pass

    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith((".tif", ".tfw")):
                continue
            zf.extract(info, root)
    marker.write_text(json.dumps(stamp), encoding="utf-8")

    found = next(root.rglob(DTM_MAIN_TIF), None)
    if found is None:
        raise DtmError(f"解壓後仍找不到 {DTM_MAIN_TIF}")
    return found


# --------------------------------------------------------------------------- #
# 最小 GeoTIFF 讀取器
# --------------------------------------------------------------------------- #
def _read_ifd(path: Path) -> tuple[dict, str]:
    """讀第一個 IFD，只取本模組需要的標籤；同時回傳位元組序。"""
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:2] == b"II":
            bo = "<"
        elif head[:2] == b"MM":
            bo = ">"
        else:
            raise DtmError(f"不是 TIFF 檔：{path}")
        magic, ifd_offset = struct.unpack(bo + "HI", head[2:8])
        if magic != 42:
            raise DtmError("只支援一般（非 BigTIFF）TIFF")
        f.seek(ifd_offset)
        (count,) = struct.unpack(bo + "H", f.read(2))
        tags: dict[int, tuple] = {}
        for _ in range(count):
            tag, typ, n = struct.unpack(bo + "HHI", f.read(8))
            size = _TYPE_SIZE.get(typ, 1) * n
            raw_field = f.read(4)
            if size <= 4:
                raw = raw_field[:size]
            else:
                (offset,) = struct.unpack(bo + "I", raw_field)
                here = f.tell()
                f.seek(offset)
                raw = f.read(size)
                f.seek(here)
            if typ == 2:  # ASCII
                tags[tag] = raw.rstrip(b"\x00").decode("ascii", "ignore")
            elif typ in _TYPE_FMT:
                fmt = _TYPE_FMT[typ]
                tags[tag] = struct.unpack(bo + fmt * n, raw[: size])
            else:
                tags[tag] = ()
    return tags, bo


@dataclass
class Raster:
    """一份帶地理定位的單波段整數柵格。"""

    path: Path
    width: int
    height: int
    origin_x: float
    origin_y: float      # 左上角（最北）的 Y
    cell: float
    nodata: int
    sample_format: int
    _offsets: tuple[int, ...]
    _lengths: tuple[int, ...]
    _rows_per_strip: int
    _byte_order: str

    # -- 建構 ---------------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path) -> "Raster":
        path = Path(path)
        tags, bo = _read_ifd(path)
        scale = tags.get(_TAG_PIXEL_SCALE) or (1.0, 1.0, 0.0)
        tie = tags.get(_TAG_TIEPOINT) or ()
        if len(tie) < 6:
            raise DtmError(f"{path.name} 缺少 GeoTIFF 定位標籤（ModelTiepoint）")
        nodata_text = tags.get(_TAG_GDAL_NODATA, "-32767")
        try:
            nodata = int(float(nodata_text))
        except ValueError:
            nodata = -32767
        return cls(
            path=path,
            width=int(tags[_TAG_WIDTH][0]),
            height=int(tags[_TAG_HEIGHT][0]),
            origin_x=float(tie[3]),
            origin_y=float(tie[4]),
            cell=float(scale[0]),
            nodata=nodata,
            sample_format=int((tags.get(_TAG_SAMPLE_FORMAT) or (1,))[0]),
            _offsets=tuple(int(v) for v in tags[_TAG_STRIP_OFFSETS]),
            _lengths=tuple(int(v) for v in tags[_TAG_STRIP_BYTE_COUNTS]),
            _rows_per_strip=int((tags.get(_TAG_ROWS_PER_STRIP) or (1,))[0]),
            _byte_order=bo,
        )

    # -- 基本資訊 -----------------------------------------------------------
    @property
    def pixel_bytes(self) -> int:
        return 2  # 這份 DTM 是 16 位元

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax)，TM2 公尺。"""
        return (self.origin_x, self.origin_y - self.height * self.cell,
                self.origin_x + self.width * self.cell, self.origin_y)

    # -- 讀取 ---------------------------------------------------------------
    def read_window(self, x0: float, y0: float, x1: float, y1: float) -> tuple[np.ndarray, float, float]:
        """讀出涵蓋指定範圍的視窗，回傳 (z[j,i] 由北而南, 視窗左上角 X, 視窗左上角 Y)。"""
        col0 = max(0, int(math.floor((x0 - self.origin_x) / self.cell)))
        col1 = min(self.width, int(math.ceil((x1 - self.origin_x) / self.cell)))
        row0 = max(0, int(math.floor((self.origin_y - y1) / self.cell)))
        row1 = min(self.height, int(math.ceil((self.origin_y - y0) / self.cell)))
        if col1 <= col0 or row1 <= row0:
            raise DtmError("指定範圍不在 DTM 涵蓋範圍內")

        ncols = col1 - col0
        dtype = np.dtype(f"{self._byte_order}i{self.pixel_bytes}")
        z = np.empty((row1 - row0, ncols), dtype=dtype)
        with open(self.path, "rb") as f:
            for out_row, row in enumerate(range(row0, row1)):
                strip = row // self._rows_per_strip
                offset = self._offsets[strip] + (row % self._rows_per_strip) * self.width * self.pixel_bytes
                f.seek(offset + col0 * self.pixel_bytes)
                z[out_row] = np.frombuffer(f.read(ncols * self.pixel_bytes), dtype=dtype)
        return z, self.origin_x + col0 * self.cell, self.origin_y - row0 * self.cell

    # -- 取樣 ---------------------------------------------------------------
    def grid(self, x0: float, y0: float, x1: float, y1: float, dx: float,
             crs: str = "EPSG:3826") -> TerrainGrid:
        """把範圍內的高程雙線性重取樣成 dx 間距的規則網格（z 的第 0 列在南邊）。"""
        raw, wx, wy = self.read_window(x0, y0, x1, y1)
        valid = raw != self.nodata
        if not valid.any():
            raise DtmError("指定範圍內沒有有效高程")
        # 先把 NODATA 用鄰域平均值填起來，避免重取樣時出現大洞
        filled = raw.astype(np.float64)
        if not valid.all():
            filled = np.where(valid, filled, float(filled[valid].mean()))
            for _ in range(50):
                nb = (np.roll(filled, 1, 0) + np.roll(filled, -1, 0)
                      + np.roll(filled, 1, 1) + np.roll(filled, -1, 1)) / 4.0
                filled = np.where(valid, filled, nb)

        nx = max(2, int(math.ceil((x1 - x0) / dx)) + 1)
        ny = max(2, int(math.ceil((y1 - y0) / dx)) + 1)
        gx = x0 + np.arange(nx) * dx
        gy = y0 + np.arange(ny) * dx
        # 原生網格的像素中心
        cols = (gx - (wx + self.cell / 2)) / self.cell
        rows = ((wy - self.cell / 2) - gy) / self.cell     # 由北而南
        c0 = np.clip(np.floor(cols).astype(int), 0, raw.shape[1] - 2)
        r0 = np.clip(np.floor(rows).astype(int), 0, raw.shape[0] - 2)
        tc = np.clip(cols - c0, 0, 1)[None, :]
        tr = np.clip(rows - r0, 0, 1)[:, None]
        z_north_up = ((filled[np.ix_(r0, c0)] * (1 - tc) + filled[np.ix_(r0, c0 + 1)] * tc) * (1 - tr)
                      + (filled[np.ix_(r0 + 1, c0)] * (1 - tc) + filled[np.ix_(r0 + 1, c0 + 1)] * tc) * tr)
        return TerrainGrid(z=np.ascontiguousarray(z_north_up[::-1]), x0=x0, y0=y0, dx=dx, crs=crs)

    def sample(self, x: float, y: float) -> float:
        """單點高程（公尺）。"""
        raw, wx, wy = self.read_window(x - self.cell, y - self.cell, x + self.cell, y + self.cell)
        j = int(round((wy - self.cell / 2 - y) / self.cell))
        i = int(round((x - (wx + self.cell / 2)) / self.cell))
        j = min(max(j, 0), raw.shape[0] - 1)
        i = min(max(i, 0), raw.shape[1] - 1)
        value = int(raw[j, i])
        return float("nan") if value == self.nodata else float(value)


_cache: Raster | None = None


def open_dtm() -> Raster:
    """取得全台 DTM（第一次呼叫會解壓縮，之後快取）。"""
    global _cache
    if _cache is None:
        _cache = Raster.open(ensure_extracted())
    return _cache


def terrain_grid(x0: float, y0: float, x1: float, y1: float, *,
                 dx: float = 10.0, crs: str = "EPSG:3826") -> TerrainGrid:
    """範圍（TM2 公尺）→ 高程網格。"""
    return open_dtm().grid(x0, y0, x1, y1, dx, crs=crs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全台 20 m DTM → 高程網格／STL")
    ap.add_argument("--bbox",
                    help="xmin,ymin,xmax,ymax（EPSG:3826 公尺）")
    ap.add_argument("--dx", type=float, default=10.0, help="網格間距（公尺）")
    ap.add_argument("--stl", help="輸出地形 STL")
    ap.add_argument("--json", help="輸出統計 JSON")
    ap.add_argument("--extract-only", action="store_true", help="只解壓，不產生網格")
    args = ap.parse_args(argv)

    if args.extract_only:
        print("解壓完成：", ensure_extracted())
        return 0
    if not args.bbox:
        ap.error("需要 --bbox，或改用 --extract-only")

    x0, y0, x1, y1 = (float(v) for v in args.bbox.split(","))
    grid = terrain_grid(x0, y0, x1, y1, dx=args.dx)
    stats = grid.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.stl:
        from .survey import write_stl
        print("STL:", write_stl(grid, args.stl))
    if args.json:
        Path(args.json).write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
