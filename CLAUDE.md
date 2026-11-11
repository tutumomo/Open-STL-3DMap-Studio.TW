# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案定位

Open STL-3DMap Studio 的台灣化分支（上游：OpenAEC-Foundation/Open-STL-3DMap-Studio，MIT）。在地圖上框選範圍，產生可多色 3D 列印的城市模型（3MF + 每色一個 STL），預設機型為 Creality K1 Max + CFS（4 槽）。

上游是荷蘭專用工具：資料源、座標系、UI 字串、錯誤訊息、README 全為荷蘭文／荷蘭服務。本分支目標是改用台灣地區資料源（見文末「台灣化耦合點」）。

## 指令

僅支援 Windows，需要 `uv` 與 Python 3.12。

```bat
install.bat                                   :: uv venv --python 3.12 .venv + 安裝 requirements.txt
start.bat                                     :: uvicorn app.main:app --host 127.0.0.1 --port 8765
.venv\Scripts\python.exe smoketest.py         :: 端到端：實際抓資料 → 建模 → 輸出到 output/smoketest
.venv\Scripts\python.exe verify_output.py     :: 檢查 smoketest 的輸出（3MF 版型、design 放置、無懸空）
build_installer.bat                           :: make_icon.py → PyInstaller → Inno Setup
```

- 沒有測試框架、沒有 linter。`smoketest.py` + `verify_output.py` 是唯一的驗證手段，兩者都需要網路，且 `verify_output.py` 依賴 `smoketest.py` 的輸出，必須依序執行。
- `smoketest.py` 的 `BBOX` 目前寫死荷蘭 Utrecht；更換資料源時要一併換成台灣測試範圍。
- 版本號有兩處必須同步：`app/appinfo.py` 的 `APP_VERSION` 與 `packaging/3dmaps.iss` 的 `#define AppVersion`。
- CI（`.github/workflows/build-release.yml`）在推送 `v*` tag 時建置 installer 與 portable ZIP。

## 架構

**伺服器無狀態。** 瀏覽器持有整份 project JSON（localStorage key `3dmaps.project`），每次請求整包 POST 給 FastAPI（`app/main.py` 的 `ProjectRequest`）。前端是無建置流程的 Leaflet 頁面（`web/`）。

**三段式流程**（`app/pipeline.py`）：

1. `load_area(project)` — WGS84 bbox 轉投影座標，呼叫 `fetch_buildings` 與 `fetch_surfaces`，回傳 `AreaData`。面積上限 `MAX_AREA_KM2 = 16.0`。Overpass 全部鏡像失敗時拋 `OverpassUnavailable`，此時只回建物且不寫快取。
2. `build_model(area, project, design, detailed)` — 產生各色 body 的 mesh。
3. `export_model(model, out_dir, prefix)` — 寫出 STL、3MF 與說明文字檔。

**每色一個 Z 帶。** 這是整個設計的核心：底板、道路、綠地、建物各佔一段高度區間，讓切片後換料次數最少。`app/config.py` 的 `PrintSettings.snapped()` 會把帶界對齊到整數層高。`DEFAULT_BANDS` 定義各 body 的 CFS 槽位。

**`app/config.py` 與 `web/app.js` 的預設值是手動同步的**（`BODY_DEFS`、`DEFAULT_PROJECT`）。改一邊要改另一邊；目前 `road_top` 兩邊已不一致（1.7 對 1.8）。

**幾何單位。** 伺服端全程使用投影座標（公尺），最後才由 `app/geo.py` 的 `PlateTransform.fit()` 仿射轉成列印 mm；所有幾何門檻都以列印 mm 表示。模型是完全平坦的，沒有地形（DEM）概念，建物高度 = 屋頂高 − 地面高。

**地形來源（`app/survey.py`，尚未接進 `pipeline.py`）。** 把測量公司的地形圖轉成規則高程網格：`parse_dxf()` 抽等高線（`C1`/`C5` 這類圖層；高程在 POLYLINE 的 `dxf.elevation`，不在頂點 Z）與測點圖塊（`POINT`/`CPOINT`/`Ditch(溝底)` 的 `ELEV` 屬性，值可能帶括號如 `(10.18)`），`filter_outliers()` 以等高線高程範圍剔除圖框角點之類的壞點，`build_terrain()` 出網格，`cross_validate()` 留出測點算精度，`write_stl()` 寫封閉實體。CLI：`python -m app.survey <dxf> --dx 1.0 --stl out.stl --validate --method tin`。
- `build_terrain(method=...)`：`tin`（預設，Delaunay，需 scipy）或 `harmonic`（多重網格 Jacobi 調和內插）。崙背豐榮掩埋場實測（dx=1 m、留出 15%、seed 7）：TIN RMSE 0.491 m / 調和 0.599 m，故預設用 TIN。
- 調和內插是「同時更新」的 Jacobi，**不能**配 SOR 鬆弛因子 ω>1（會發散到 1e202）；ω>1 只適用逐點更新的 Gauss-Seidel。
- DWG→DXF 不要用 `accoreconsole.exe`（中文路徑直接失敗，ASCII 路徑則無聲卡死）。改用 Open CAD Studio 的 `ocs_save_verified(..., target_format="dxf")`：即使回 `semantic_mismatch`（Polyline/Polyline2D 型別重分類）檔案仍可用。

**資料源替換介面**（`app/sources.py`）：`Building` dataclass（`id`、`footprint_rd`、`height_m`、`ground_m`、`year`、`floors`、`status`）與 `SurfaceData` 是下游唯一依賴的型別。只要新資料源能填出這兩個結構，`pipeline.py` 與 `meshing.py` 不必更動。

**3MF 版型。** `app/meshing.py` 的 `write_3mf` 刻意輸出 Bambu/Orca 版型（`3D/Objects/object-1.model` + `Metadata/model_settings.config`），不是 PrusaSlicer 版型；`verify_output.py` 會檢查這一點。

**資料目錄。** `app/paths.py` 的 `data_dir()` 在開發時是 repo 根目錄（`cache/`、`uploads/`、`projects/`、`settings.json`），打包後是 `%LOCALAPPDATA%\Open STL-3DMap Studio`。HTTP 回應以 sha1 檔名快取在 `cache/`，調整資料源時記得清快取或傳 `refresh=True`。

**打包進入點。** `run_app.py` 會以 `--pick-folder` 自我重啟來開資料夾對話框（`app/folder_dialog.py`）。

## 台灣化耦合點

| 項目 | 位置 | 現況 | 台灣替代 |
|---|---|---|---|
| 座標系 | `app/config.py` `CRS_RD = "EPSG:28992"`；`app/sources.py` WFS bbox 字串另有一處硬寫 | 荷蘭 RD New | 本島 `EPSG:3826`（TWD97 TM2 121）；澎湖金馬 `EPSG:3825` |
| 命名 | 全專案 `*_rd`（`bbox_rd`、`footprint_rd`、`wgs84_to_rd`…） | 指 RD 座標 | 純命名問題，可留可改 |
| 建物 | `app/sources.py` `fetch_buildings` | 3DBAG WFS / OGC API | OSM building（Overpass），高度需推估 |
| 精細屋頂 | `app/buildings3d.py` `fetch_buildings_detailed` | 3DBAG LoD2.2 | 無對等開放 API，需停用 |
| 地表 | `app/sources.py` `fetch_surfaces` | Overpass（全球） | 可直接沿用 |
| 地理編碼 | `app/sources.py` `geocode` | PDOK Locatieserver | Nominatim（`countrycodes=tw`） |
| 底圖 | `web/app.js` 初始視角與三個 PDOK WMTS 圖層 | 荷蘭 | NLSC WMTS：`https://wmts.nlsc.gov.tw/wmts/{EMAP,PHOTO2}/default/GoogleMapsCompatible/{z}/{y}/{x}`（注意是 z/y/x） |
| 文字 | `web/`、`app/config.py`、`app/pipeline.py`、`app/main.py`、`app/meshing.py` 的 3MF metadata、README | 荷蘭文 | 繁體中文 |

已排除的建物／地形來源（2026-11-11 實測）：

- Google Open Buildings v3：涵蓋台灣的 tile 347 內沒有任何一筆落在台灣（只有菲律賓巴丹群島）。2.5D Temporal（含高度）官方說明的範圍同樣不含東亞。
- Microsoft GlobalMLBuildingFootprints：台北、台中、高雄、花蓮的 quadkey 都沒有資料。
- Google Maps Platform（Photorealistic 3D Tiles、Elevation API、2D Tiles）：都需要 API key 與計費，且政策禁止擷取、快取、離線使用內容，不能拿來產生 STL／3MF。

## 已知問題

- `run_app.py` 比對的是舊字串 `"3d-kaart-bouwer-nl"`，但 `app/appinfo.py` 的 `INSTANCE_MARKER` 是 `"open-stl-3dmap-studio"`，所以「已在執行」偵測永遠不成立。
- README 的版本號落後於 `APP_VERSION`。
