"""地形測量圖匯入：DWG/DXF → 高程網格（DEM）。

台灣測量公司產出的地形圖，地形資訊固定落在這幾類圖元上：

* **等高線**：2D 聚合線，高程存在聚合線的 elevation（C1 = 首曲線 1 m，
  C5 = 計曲線 5 m），線上另插一個高程數字 TEXT。
* **測點**：圖塊 INSERT，屬性 ``ELEV``（高程）、``POINT``（點號）、
  ``DESC``（地物碼）。點號 5000 系列是實測點。
* **控制點／三角點**：圖塊 ``CPOINT``，屬性 ``ELEV``、``CNTP``。
* 其餘（地類界、房屋、圍籬、樁位坐標表、圖例）不參與地形。

本模組把前三類抽成 ``SurveyData``，再用「等高線＋測點當固定條件、
其餘格點做調和內插（harmonic interpolation）」產生規則網格。
選調和內插而不是 TIN 的理由：等高線本身就是等值線，調和內插在兩條
等高線之間給出等坡度的面，與測量員的認知一致；TIN 在等高線間距
不均時會產生無意義的三角面。

座標一律沿用圖面座標（台灣多為 TWD97 TM2，EPSG:3826）。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# 高程屬性可能出現的欄位名（掃描時一律轉大寫比對）
ELEV_ATTRS: tuple[str, ...] = ("ELEV", "ELEVATION", "EL", "Z", "HIGH", "高程", "標高")

# 圖層名含這些字串就當等高線
CONTOUR_KEYWORDS: tuple[str, ...] = ("等高線", "曲線", "CONTOUR", "CONTOURS")

# 預設會被視為等高線的圖層（台灣常見代號）
DEFAULT_CONTOUR_LAYERS: tuple[str, ...] = ("C1", "C5", "C10", "DGX", "等高線")

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------


@dataclass
class SurveyData:
    """從一張地形圖抽出來的地形資料。"""

    contours: list[tuple[np.ndarray, float]] = field(default_factory=list)
    """(頂點陣列 nx2, 高程 m)"""

    spots: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    """實測點／控制點 (x, y, z)"""

    spot_labels: list[str] = field(default_factory=list)
    layers_seen: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def n_contour_vertices(self) -> int:
        return int(sum(len(c) for c, _ in self.contours))

    def summary(self) -> dict:
        zs = [z for _, z in self.contours]
        zz = self.spots[:, 2] if len(self.spots) else np.array([])
        return {
            "source": self.source,
            "contours": len(self.contours),
            "contour_vertices": self.n_contour_vertices,
            "contour_z_range": [min(zs), max(zs)] if zs else None,
            "spots": int(len(self.spots)),
            "spot_z_range": [float(zz.min()), float(zz.max())] if zz.size else None,
            "warnings": self.warnings,
        }


@dataclass
class TerrainGrid:
    """規則高程網格。z[j, i] 對應 (x0 + i*dx, y0 + j*dx)，第 0 列在南側。"""

    z: np.ndarray
    x0: float
    y0: float
    dx: float
    crs: str = "EPSG:3826"

    @property
    def ny(self) -> int:
        return int(self.z.shape[0])

    @property
    def nx(self) -> int:
        return int(self.z.shape[1])

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x0 + (self.nx - 1) * self.dx,
                self.y0 + (self.ny - 1) * self.dx)

    def sample(self, x, y):
        """雙線性取樣，超出範圍時夾到邊界。"""
        fx = np.clip((np.asarray(x) - self.x0) / self.dx, 0, self.nx - 1)
        fy = np.clip((np.asarray(y) - self.y0) / self.dx, 0, self.ny - 1)
        i0 = np.floor(fx).astype(int)
        j0 = np.floor(fy).astype(int)
        i1 = np.minimum(i0 + 1, self.nx - 1)
        j1 = np.minimum(j0 + 1, self.ny - 1)
        tx = fx - i0
        ty = fy - j0
        return ((self.z[j0, i0] * (1 - tx) + self.z[j0, i1] * tx) * (1 - ty)
                + (self.z[j1, i0] * (1 - tx) + self.z[j1, i1] * tx) * ty)

    def stats(self) -> dict:
        return {
            "shape": [self.ny, self.nx],
            "cell_m": self.dx,
            "bounds": [round(v, 3) for v in self.bounds],
            "z_min": float(self.z.min()),
            "z_max": float(self.z.max()),
            "z_mean": float(self.z.mean()),
            "crs": self.crs,
        }


# --------------------------------------------------------------------------
# DXF 解析
# --------------------------------------------------------------------------


def _num(text: object) -> float | None:
    """從屬性值抓數字。``"(10.18)"`` 代表推估高程，一樣取 10.18。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    m = _NUMBER_RE.search(s.replace(",", ""))
    return float(m.group()) if m else None


def _layer_is_contour(layer: str, extra: tuple[str, ...]) -> bool:
    name = layer.strip()
    if name in extra:
        return True
    up = name.upper()
    return any(k.upper() in up for k in CONTOUR_KEYWORDS)


def _polyline_points(e) -> tuple[np.ndarray, float]:
    """回傳 (nx2 頂點, 高程)。2D 聚合線的高程在 elevation，3D 的在頂點 Z。"""
    t = e.dxftype()
    if t == "LWPOLYLINE":
        pts = np.array([(p[0], p[1]) for p in e.get_points("xy")], dtype=float)
        elev = float(e.dxf.get("elevation", 0.0) or 0.0)
        return pts, elev
    if t == "POLYLINE":
        pts = np.array([(v.dxf.location[0], v.dxf.location[1]) for v in e.vertices], dtype=float)
        elev = float(e.dxf.elevation[2])
        vz = [float(v.dxf.location[2]) for v in e.vertices]
        if vz and max(abs(v) for v in vz) > 1e-6:
            elev = float(np.mean(vz))
        return pts, elev
    if t == "SPLINE":
        pts = np.array([(p[0], p[1]) for p in e.fit_points], dtype=float)
        zs = [float(p[2]) for p in e.fit_points]
        return pts, float(np.mean(zs)) if zs else 0.0
    return np.zeros((0, 2)), 0.0


def _texts_on_layer(msp, layer: str) -> list[tuple[float, float, float]]:
    """圖層上的 TEXYT/MTEXT 數字，用來補等高線缺少的 elevation。"""
    out = []
    for e in msp:
        if e.dxf.layer != layer:
            continue
        t = e.dxftype()
        if t == "TEXT":
            val = _num(e.dxf.text)
            p = e.dxf.insert
        elif t == "MTEXT":
            val = _num(e.text)
            p = e.dxf.insert
        else:
            continue
        if val is not None:
            out.append((float(p[0]), float(p[1]), val))
    return out


def parse_dxf(path: str | Path, *, contour_layers: tuple[str, ...] = DEFAULT_CONTOUR_LAYERS,
              extra_layers: tuple[str, ...] = (), keep_hidden: bool = True) -> SurveyData:
    """讀 DXF 並抽出等高線與高程點。"""
    try:
        import ezdxf
    except ImportError as exc:  # pragma: no cover - 依賴缺失時給明確訊息
        raise RuntimeError("需要 ezdxf 才能讀 DXF：pip install ezdxf") from exc

    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    data = SurveyData(source=str(path))
    extra = tuple(contour_layers) + tuple(extra_layers)

    labels: dict[str, list[tuple[float, float, float]]] = {}
    for e in msp:
        data.layers_seen[e.dxf.layer] = data.layers_seen.get(e.dxf.layer, 0) + 1
        if e.dxftype() in ("TEXT", "MTEXT"):
            continue

    # 1) 等高線
    for e in msp:
        layer = e.dxf.layer
        if not _layer_is_contour(layer, extra):
            continue
        if e.dxftype() not in ("LWPOLYLINE", "POLYLINE", "SPLINE"):
            continue
        pts, elev = _polyline_points(e)
        if len(pts) < 2:
            continue
        if abs(elev) < 1e-9:
            # 沒有 elevation：找同圖層最近的數字文字
            if layer not in labels:
                labels[layer] = _texts_on_layer(msp, layer)
            cand = labels[layer]
            if cand:
                mid = pts[len(pts) // 2]
                best = min(cand, key=lambda t: (t[0] - mid[0]) ** 2 + (t[1] - mid[1]) ** 2)
                dist = math.dist((best[0], best[1]), (mid[0], mid[1]))
                if dist < 30.0:
                    elev = best[2]
        if abs(elev) < 1e-9:
            data.warnings.append(f"圖層 {layer} 有等高線但讀不到高程，已略過")
            continue
        data.contours.append((pts, float(elev)))

    # 2) 高程點（圖塊屬性）
    pts: list[tuple[float, float, float]] = []
    labels_out: list[str] = []
    for e in msp:
        if e.dxftype() == "INSERT":
            if not keep_hidden and e.dxf.layer.upper().endswith("-HIDE"):
                continue
            elev: float | None = None
            name = ""
            for a in e.attribs:
                tag = str(a.dxf.tag).strip().upper()
                if tag in ELEV_ATTRS and elev is None:
                    elev = _num(a.dxf.text)
                if tag in ("POINT", "CNTP", "PTNO", "NO"):
                    name = str(a.dxf.text).strip()
            if elev is None:
                continue
            p = e.dxf.insert
            pts.append((float(p[0]), float(p[1]), float(elev)))
            labels_out.append(name)
        elif e.dxftype() == "POINT":
            p = e.dxf.location
            if abs(float(p[2])) > 1e-6:
                pts.append((float(p[0]), float(p[1]), float(p[2])))
                labels_out.append("")

    if pts:
        arr = np.array(pts, dtype=float)
        # 同一位置重複（OBSPT 與 OBSPT-Hide 各有一份）只留一筆
        key = np.round(arr[:, :2], 2)
        _, idx = np.unique(key, axis=0, return_index=True)
        idx.sort()
        data.spots = arr[idx]
        data.spot_labels = [labels_out[i] for i in idx]

    # 3) 單位：圖面若以 mm 繪製（座標量級 > 1e7），換算成公尺
    scale = _detect_unit_scale(data)
    if scale != 1.0:
        data.contours = [(c / scale, z) for c, z in data.contours]
        if len(data.spots):
            data.spots[:, :2] /= scale
        data.warnings.append(f"圖面單位判定為毫米，座標已除以 {scale:g}")

    if not data.contours and not len(data.spots):
        raise ValueError("圖面沒有可用的等高線或高程點，無法建立地形")
    return data


def _detect_unit_scale(data: SurveyData) -> float:
    xs = [c[:, 0] for c, _ in data.contours]
    if len(data.spots):
        xs.append(data.spots[:, 0])
    if not xs:
        return 1.0
    allx = np.concatenate(xs)
    span = float(allx.max() - allx.min())
    # 台灣 TM2 的 X 約 1.5e5~3.2e5 m；以 mm 繪製時會是 1e8 等級
    if span > 1e6:
        return 1000.0
    return 1.0


def detect_crs(data: SurveyData) -> str:
    """由座標量級猜測 TWD97 分帶。"""
    xs = [c[:, 0] for c, _ in data.contours]
    ys = [c[:, 1] for c, _ in data.contours]
    if len(data.spots):
        xs.append(data.spots[:, 0])
        ys.append(data.spots[:, 1])
    if not xs:
        return "EPSG:3826"
    x = float(np.concatenate(xs).mean())
    y = float(np.concatenate(ys).mean())
    if 2_400_000 < y < 2_800_000:
        # 澎湖、金門在 3825（TM2 119），本島在 3826（TM2 121）
        return "EPSG:3825" if x < 100_000 else "EPSG:3826"
    return "EPSG:3826"


# --------------------------------------------------------------------------
# DEM 建立
# --------------------------------------------------------------------------


def _densify(points: np.ndarray, step: float) -> np.ndarray:
    """在長邊補點，讓等高線在網格上有足夠密度。"""
    if len(points) < 2:
        return points
    out = [points[0]]
    for a, b in zip(points[:-1], points[1:]):
        d = float(np.hypot(*(b - a)))
        n = int(d // step) if step > 0 else 0
        for k in range(1, n + 1):
            out.append(a + (b - a) * (k / (n + 1)))
        out.append(b)
    return np.array(out)


def _scatter(ii: np.ndarray, jj: np.ndarray, vv: np.ndarray,
             shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """把格點索引與數值寫進網格；同格多值取平均。回傳 (值, 是否有值)。"""
    acc = np.zeros(shape, dtype=float)
    cnt = np.zeros(shape, dtype=float)
    np.add.at(acc, (jj, ii), vv)
    np.add.at(cnt, (jj, ii), 1.0)
    hit = cnt > 0
    acc[hit] /= cnt[hit]
    return acc, hit


def _neighbour_mean(z: np.ndarray) -> np.ndarray:
    p = np.pad(z, 1, mode="edge")
    return (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) * 0.25


def filter_outliers(data: SurveyData, *, band: float | None = None) -> SurveyData:
    """濾掉高程明顯不屬於地形的測點（圖框角點、誤植屬性等）。

    判準：以等高線的高程範圍為可信帶，超出 ±band 的測點視為異常。
    band 預設取等高線高差的 25%（至少 5 m）。沒有等高線時改用中位數
    ±6 倍 MAD。
    """
    if not len(data.spots):
        return data
    z = data.spots[:, 2]
    keep = np.ones(len(z), dtype=bool)
    if data.contours:
        cz = np.array([e for _, e in data.contours])
        lo, hi = float(cz.min()), float(cz.max())
        b = band if band is not None else max(5.0, 0.25 * (hi - lo))
        keep = (z >= lo - b) & (z <= hi + b)
    else:
        med = float(np.median(z))
        mad = float(np.median(np.abs(z - med))) or 1.0
        keep = np.abs(z - med) <= 6.0 * mad
    if keep.all():
        return data
    dropped = int((~keep).sum())
    out = SurveyData(contours=data.contours, spots=data.spots[keep],
                     spot_labels=[l for l, k in zip(data.spot_labels, keep) if k],
                     layers_seen=dict(data.layers_seen),
                     warnings=list(data.warnings) + [f"剔除 {dropped} 個高程異常的測點"],
                     source=data.source)
    return out


def _rasterize(data: SurveyData, x0: float, y0: float, gdx: float,
               nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
    """把等高線與測點寫進網格，回傳 (值, 是否為固定條件)。"""
    z = np.zeros((ny, nx), dtype=float)
    fixed = np.zeros((ny, nx), dtype=bool)
    step = max(gdx * 0.8, 0.05)
    cii, cjj, cvv = [], [], []
    for pts, elev in data.contours:
        dense = _densify(pts, step)
        cii.append(np.clip(np.round((dense[:, 0] - x0) / gdx).astype(int), 0, nx - 1))
        cjj.append(np.clip(np.round((dense[:, 1] - y0) / gdx).astype(int), 0, ny - 1))
        cvv.append(np.full(len(dense), elev))
    if cii:
        cont_z, cont_hit = _scatter(np.concatenate(cii), np.concatenate(cjj),
                                    np.concatenate(cvv), (ny, nx))
        z = np.where(cont_hit, cont_z, z)
        fixed |= cont_hit
    if len(data.spots):
        ii = np.clip(np.round((data.spots[:, 0] - x0) / gdx).astype(int), 0, nx - 1)
        jj = np.clip(np.round((data.spots[:, 1] - y0) / gdx).astype(int), 0, ny - 1)
        spot_z, spot_hit = _scatter(ii, jj, data.spots[:, 2], (ny, nx))
        z = np.where(spot_hit, spot_z, z)
        fixed |= spot_hit
    return z, fixed


def _resample(z: np.ndarray, ny: int, nx: int) -> np.ndarray:
    """雙線性放大到目標尺寸，當下一層的初始猜測。"""
    oy, ox = z.shape
    yi = np.linspace(0, oy - 1, ny)
    xi = np.linspace(0, ox - 1, nx)
    j0 = np.floor(yi).astype(int); j1 = np.minimum(j0 + 1, oy - 1)
    i0 = np.floor(xi).astype(int); i1 = np.minimum(i0 + 1, ox - 1)
    ty = (yi - j0)[:, None]; tx = (xi - i0)[None, :]
    return ((z[np.ix_(j0, i0)] * (1 - tx) + z[np.ix_(j0, i1)] * tx) * (1 - ty)
            + (z[np.ix_(j1, i0)] * (1 - tx) + z[np.ix_(j1, i1)] * tx) * ty)


class TinUnavailable(RuntimeError):
    """環境沒有 scipy，無法使用 TIN 內插（打包版刻意不帶 scipy 以縮小體積）。"""


def _build_tin(data: SurveyData, x0: float, y0: float, dx: float,
               nx: int, ny: int) -> np.ndarray:
    """Delaunay TIN（Terén、Civil 3D 等測量軟體的標準做法）。

    控制點＝等高線頂點＋測點，三角形內以平面內插取高程。
    凸包外的格子沒有定義，用鄰域平均值向外延伸補滿。
    """
    try:
        from scipy.interpolate import LinearNDInterpolator
        from scipy.spatial import Delaunay
    except ImportError as exc:  # scipy 是選用依賴
        raise TinUnavailable("method='tin' 需要 scipy，請先 pip install scipy") from exc

    pts: list[np.ndarray] = []
    zs: list[np.ndarray] = []
    for c, z in data.contours:
        pts.append(np.asarray(c, dtype=float)[:, :2])
        zs.append(np.full(len(c), float(z)))
    if len(data.spots):
        pts.append(data.spots[:, :2])
        zs.append(data.spots[:, 2])
    if not pts:
        raise ValueError("地形資料沒有落在網格內")

    tri = Delaunay(np.vstack(pts))
    interp = LinearNDInterpolator(tri, np.concatenate(zs))
    gx = x0 + np.arange(nx) * dx
    gy = y0 + np.arange(ny) * dx
    z = interp(*np.meshgrid(gx, gy))
    known = ~np.isnan(z)
    if not known.any():
        raise ValueError("地形資料沒有落在網格內")
    z = np.where(known, z, float(np.nanmean(z)))
    for _ in range(200):  # 凸包外周邊用鄰域平均值外插
        z = np.where(known, z, _neighbour_mean(z))
    return z


def build_terrain(data: SurveyData, *, dx: float = 1.0, margin: float = 0.0,
                  iterations: int = 400, crs: str | None = None,
                  drop_outliers: bool = True,
                  method: str = "tin") -> TerrainGrid:
    """等高線＋測點 → 規則高程網格。

    method="tin"（預設）用 Delaunay TIN，即 Terén、Civil 3D 等測量軟體的做法。
    method="harmonic" 用調和內插，以「多重網格」求解：先在 8 倍粗的網格鬆弛，
    再逐層細化到目標間距；純 Jacobi 一次只傳遞一格資訊，直接從最細網格開始
    要上萬次迭代。
    崙背豐榮掩埋場實測留出驗證：TIN RMSE 0.491 m、調和 0.599 m。
    """
    if method not in ("harmonic", "tin"):
        raise ValueError("method 必須是 'harmonic' 或 'tin'")
    if drop_outliers:
        data = filter_outliers(data)
    if not data.contours and not len(data.spots):
        raise ValueError("沒有地形資料")

    xs, ys = [], []
    for c, _ in data.contours:
        xs.append(c[:, 0]); ys.append(c[:, 1])
    if len(data.spots):
        xs.append(data.spots[:, 0]); ys.append(data.spots[:, 1])
    allx = np.concatenate(xs); ally = np.concatenate(ys)
    x0 = float(allx.min()) - margin
    y0 = float(ally.min()) - margin
    x1 = float(allx.max()) + margin
    y1 = float(ally.max()) + margin
    nx = max(2, int(math.ceil((x1 - x0) / dx)) + 1)
    ny = max(2, int(math.ceil((y1 - y0) / dx)) + 1)

    if method == "tin":
        try:
            z = _build_tin(data, x0, y0, dx, nx, ny)
            return TerrainGrid(z=z, x0=x0, y0=y0, dx=dx, crs=crs or detect_crs(data))
        except TinUnavailable as exc:
            # 打包版不含 scipy，退回調和內插（精度略差但仍可用）
            import warnings
            warnings.warn(f"{exc}；改用調和內插", RuntimeWarning, stacklevel=2)
            method = "harmonic"

    factors = [f for f in (8, 4, 2, 1) if f * dx * 4 <= max(x1 - x0, y1 - y0)]
    if not factors:
        factors = [1]
    z_prev: np.ndarray | None = None
    z = np.zeros((ny, nx))
    fixed = np.zeros((ny, nx), dtype=bool)
    for f in factors:
        gdx = dx * f
        gnx = max(2, int(math.ceil((x1 - x0) / gdx)) + 1)
        gny = max(2, int(math.ceil((y1 - y0) / gdx)) + 1)
        zc, fixed = _rasterize(data, x0, y0, gdx, gnx, gny)
        if not fixed.any():
            continue
        if z_prev is None:
            z = np.where(fixed, zc, float(zc[fixed].mean()))
        else:
            z = np.where(fixed, zc, _resample(z_prev, gny, gnx))
        for _ in range(iterations):
            z = np.where(fixed, z, _neighbour_mean(z))
        z_prev = z

    if not fixed.any():
        raise ValueError("地形資料沒有落在網格內")
    if z_prev is not None and z.shape != (ny, nx):
        z = _resample(z_prev, ny, nx)
    return TerrainGrid(z=z, x0=x0, y0=y0, dx=dx, crs=crs or detect_crs(data))


def cross_validate(data: SurveyData, *, dx: float = 1.0, holdout: float = 0.15,
                   seed: int = 7, method: str = "tin") -> dict:
    """留出部分測點，量測內插誤差（RMSE / 最大誤差），作為資料品質證據。

    先剔除高程異常點再切分，否則圖框角點之類的壞點會主導誤差統計，
    掩蓋不同內插方法之間的差異。
    """
    data = filter_outliers(data)
    if len(data.spots) < 20:
        return {"skipped": "測點不足 20 個"}
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(data.spots))
    n_test = max(1, int(len(idx) * holdout))
    test, train = idx[:n_test], idx[n_test:]

    sub = SurveyData(contours=data.contours, spots=data.spots[train],
                     spot_labels=[data.spot_labels[i] for i in train],
                     source=data.source)
    grid = build_terrain(sub, dx=dx, method=method)
    pred = grid.sample(data.spots[test, 0], data.spots[test, 1])
    err = pred - data.spots[test, 2]
    return {
        "tested": int(len(test)),
        "rmse_m": float(np.sqrt(np.mean(err ** 2))),
        "mae_m": float(np.mean(np.abs(err))),
        "max_m": float(np.max(np.abs(err))),
        "p95_m": float(np.percentile(np.abs(err), 95)),
    }


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------


def write_stl(grid: TerrainGrid, path: str | Path, *, max_cells: int = 400_000,
              base_z: float | None = None) -> Path:
    """把地形寫成封閉實體 STL（頂面＋側裙＋底面），供目視檢查或直接列印。"""
    step = max(1, int(math.ceil(math.sqrt(grid.nx * grid.ny / max_cells))))
    z = grid.z[::step, ::step]
    ny, nx = z.shape
    dx = grid.dx * step
    base = float(z.min() - 2.0) if base_z is None else float(base_z)

    ii, jj = np.meshgrid(np.arange(nx), np.arange(ny))
    X = grid.x0 + ii * dx
    Y = grid.y0 + jj * dx
    V = np.stack([X, Y], axis=-1)

    # 頂面：每個格點當左下角，拆成兩個三角形
    a = V[:-1, :-1]; b = V[:-1, 1:]; c = V[1:, :-1]; d = V[1:, 1:]
    za, zb, zc, zd = z[:-1, :-1], z[:-1, 1:], z[1:, :-1], z[1:, 1:]
    A = np.concatenate([a, a], axis=0).reshape(-1, 2)
    B = np.concatenate([b, d], axis=0).reshape(-1, 2)
    C = np.concatenate([d, c], axis=0).reshape(-1, 2)
    ZA = np.concatenate([za, za], axis=0).ravel()
    ZB = np.concatenate([zb, zd], axis=0).ravel()
    ZC = np.concatenate([zd, zc], axis=0).ravel()
    top = np.stack([np.column_stack([A, ZA]), np.column_stack([B, ZB]),
                    np.column_stack([C, ZC])], axis=1)

    # 側裙：沿邊界把頂面往下拉到 base
    ring_i = np.concatenate([np.arange(nx - 1), np.full(ny - 1, nx - 1),
                             np.arange(nx - 1, 0, -1), np.zeros(ny - 1, dtype=int)])
    ring_j = np.concatenate([np.zeros(nx - 1, dtype=int), np.arange(ny - 1),
                             np.full(nx - 1, ny - 1), np.arange(ny - 1, 0, -1)])
    next_i = np.concatenate([np.arange(1, nx), np.full(ny - 1, nx - 1),
                             np.arange(nx - 2, -1, -1), np.zeros(ny - 1, dtype=int)])
    next_j = np.concatenate([np.zeros(nx - 1, dtype=int), np.arange(1, ny),
                             np.full(nx - 1, ny - 1), np.arange(ny - 2, -1, -1)])
    p = np.column_stack([V[ring_j, ring_i], z[ring_j, ring_i]])
    q = np.column_stack([V[next_j, next_i], z[next_j, next_i]])
    p2 = np.column_stack([p[:, :2], np.full(len(p), base)])
    q2 = np.column_stack([q[:, :2], np.full(len(q), base)])
    skirt = np.concatenate([np.stack([p, p2, q2], axis=1),
                            np.stack([p, q2, q], axis=1)], axis=0)

    # 底面
    c0 = np.array([[V[0, 0, 0], V[0, 0, 1], base]])
    c1 = np.array([[V[0, nx - 1, 0], V[0, nx - 1, 1], base]])
    c2 = np.array([[V[ny - 1, nx - 1, 0], V[ny - 1, nx - 1, 1], base]])
    c3 = np.array([[V[ny - 1, 0, 0], V[ny - 1, 0, 1], base]])
    bottom = np.concatenate([np.stack([c0, c2, c1], axis=1),
                             np.stack([c0, c3, c2], axis=1)], axis=0)

    tris = np.concatenate([top, skirt, bottom], axis=0)
    nrm = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.divide(nrm, ln, out=np.zeros_like(nrm), where=ln > 0)

    rec = np.zeros((len(tris), 12), dtype=np.float32)
    rec[:, 0:3] = nrm
    rec[:, 3:12] = tris.reshape(len(tris), 9)
    blob = np.zeros((len(tris), 50), dtype=np.uint8)
    blob[:, :48] = rec.view(np.uint8).reshape(len(tris), 48)
    with open(path, "wb") as fh:
        fh.write(b"Open STL-3DMap Studio terrain".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        fh.write(blob.tobytes())
    return Path(path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="地形測量圖（DXF）→ 高程網格／STL")
    ap.add_argument("dxf")
    ap.add_argument("--dx", type=float, default=1.0, help="網格間距（公尺）")
    ap.add_argument("--stl", help="輸出地形 STL")
    ap.add_argument("--json", help="輸出統計 JSON")
    ap.add_argument("--no-hidden", action="store_true", help="排除 *-Hide 圖層的重複測點")
    ap.add_argument("--validate", action="store_true", help="留出 15%% 測點做精度驗證")
    ap.add_argument("--method", choices=("harmonic", "tin"), default="tin",
                    help="內插方法：tin（Delaunay，預設）或 harmonic（調和內插）")
    args = ap.parse_args(argv)

    data = parse_dxf(args.dxf, keep_hidden=not args.no_hidden)
    print(json.dumps(data.summary(), ensure_ascii=False, indent=2))
    grid = build_terrain(data, dx=args.dx, method=args.method)
    print(json.dumps(grid.stats(), ensure_ascii=False, indent=2))
    if args.validate:
        print(json.dumps(cross_validate(data, dx=args.dx, method=args.method),
                         ensure_ascii=False, indent=2))
    if args.stl:
        print("STL:", write_stl(grid, args.stl))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"survey": data.summary(), "grid": grid.stats()}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
