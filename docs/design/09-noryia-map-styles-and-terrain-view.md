# Noryia 地图样式切换与地形图实施方案

状态：实施前方案。目标是让政治图与地形图共用同一套经纬度和空间事实，并让路径规划实际读取地形图的数据来源。

## 目标

前端地图增加样式切换器，至少提供：

1. **政治图**：当前无标注 Noryia SVG 为底，叠加国家、省份、城镇标签。
2. **地形图**：由 Noryia 完整 JSON 的格网高度、生物群系、水体、河流和道路生成，不从 SVG 颜色猜测。
3. **高程图**：用连续色带显示海拔；可作为地形图的可选叠加层。
4. **通行图**：显示当前载具可走、不可走、道路加速、桥梁/渡口/港口/山口；仅在导航数据审核通过后启用。

同一个经纬度在所有样式下必须投影到同一屏幕位置。切换样式不得改变人物坐标、路线、镜头缩放或已选目的地。

## Noryia JSON 中的地形来源

`Noryia Full *.json` 的 `pack.cells` 已提供运行时所需的原始地理字段：

| 字段 | 含义 | 用法 |
|---|---|---|
| `p` | 格网中心像素坐标 | 转换为经纬度，建立空间索引 |
| `h` | 高度码 | 生成相对高程、坡度和海陆阈值 |
| `biome` | 生物群系 ID | 生成草原、森林、湿地、沙漠、冰原等地表类型 |
| `r` | 河流 ID | 关联河流中心线和跨越限制 |
| `harbor` / `haven` | 港口、避风港信息 | 水陆换乘、船只路线节点 |
| `state` / `province` / `burg` | 人文归属 | 政治图标签与行政边界 |
| `routes.points` | 道路、步道、海路折线 | 道路叠加与路线网络 |

高度码不是米；实施时必须把高度码映射公式记录到 `navigation/noryia/metadata.json`。在该映射审核完成前，地形图显示“相对高度”，路径引擎不得宣称精确海拔米数。

## 生成产物

在 `scripts/generate_noryia_navigation_data.py` 中一次生成以下静态资产：

```text
docs/worldbuilding/maps/navigation/noryia/
  terrain-base.svg              # 生物群系与海陆的矢量地形底图
  elevation.svg                 # 相对高度色带
  waterways.geojson             # 河流与可航行等级
  roads.geojson                 # 道路/步道/海路及等级
  crossings.geojson             # 桥、浅滩、渡口、港口、山口
  terrain-index.json            # 经纬度 -> 格网属性
  movement-rules.json           # 地表/坡度/道路速度倍率
  audit-overlay.svg             # 审核用叠加图
```

优先选择 SVG 或矢量切片，而不是单张低分辨率 PNG；这样 3000% 缩放时地形边界和道路不会模糊。大规模格网若导致 SVG 过大，可将地形底图按 Noryia 世界边界分为 4×2 或 8×4 切片，但切片经纬边界必须连续、无重叠。

## 数据审核

生成后不能直接启用。审核顺序：

1. 海洋、湖泊、河流与海岸线是否和 SVG/城镇港口一致。
2. 高地、山脉、平原和山口是否合理；高度码映射是否单调。
3. 生物群系是否与地形视觉一致，湿地/沙漠/冰原是否没有误判。
4. `roads / trails / searoutes` 的分类和道路点是否连接正确。
5. 桥梁、浅滩、渡口、港口、山口是否覆盖所有允许跨越点。
6. 审核者把数据集从 `candidate` 标为 `approved` 后，路径规划器才可使用。

## 数据库与 API

复用 `08-noryia-terrain-routing-implementation.md` 中的 `navigation_datasets`。地图样式本身不写进人物表；仅保存用户界面偏好。

新增只读接口：

```text
GET /api/worlds/{world_id}/map-styles
GET /api/worlds/{world_id}/terrain?longitude=&latitude=
GET /api/worlds/{world_id}/navigation-dataset
```

`terrain` 返回当前坐标的高程、坡度、生物群系、水体、道路、河流和通行摘要；只有 `approved` 数据集可被移动引擎调用。

可选的 UI 偏好表：

```sql
CREATE TABLE world_map_preferences (
  world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
  style TEXT NOT NULL CHECK(style IN ('political','terrain','elevation','passability')),
  updated_at TEXT NOT NULL
);
```

未保存偏好时默认政治图。偏好影响展示，不影响规则。

## 前端交互

在现有地图缩放控件旁加入“样式”下拉框：政治图、地形图、高程图、通行图。

- 切换时保留 `mapView.scale / x / y` 和当前地图层级。
- 地形图显示生物群系底色、水系和道路；国家/省份/城镇标签仍遵守现有缩放层级。
- 高程图显示色带与可选等高线；悬停提示相对高程/已审核海拔、坡度、地表类型。
- 通行图根据当前人物的 `movement_type`、载具能力和封路状态，用颜色显示可达性；禁止前端自行决定规则。
- 路径规划成功后，在任意底图上叠加同一条路线折线；样式切换不重新规划。

## 与路径规划的绑定

地形图不只是视觉功能。运行时 `TerrainSampler.sample(longitude, latitude)` 必须与地形图使用同一份 `terrain-index.json` 和 `movement-rules.json`。

```text
政治图：显示国家与行政信息
地形图：显示 TerrainSampler 的输入事实
路线规划：读取同一 TerrainSampler + 路网
持续移动：沿规划路线再次读取同一 TerrainSampler
```

这样玩家在地形图上看到的森林、道路、河流、高坡和海域，才会和实际移动成本、桥梁限制和 ETA 一致。

## 实施顺序与验收

1. 完成 Noryia 导航数据生成器和来源哈希测试。
2. 生成并人工审核地形图、高程图与道路/河流叠加。
3. 增加样式 API 和前端切换器；验证切换不改变镜头与坐标。
4. 实现 `TerrainSampler`，让悬停提示读取真实格网数据。
5. 让 `RoutingPlanner` 与 `MovementService` 使用同一数据集。
6. 增加视觉、接口和路线一致性测试。

验收条件：在同一坐标切换四种样式，人物/路线屏幕位置不变；悬停地形与移动日志的地表描述一致；道路路线 ETA 低于荒野路线；陆行不能穿越地形图标示的外海或无门户河流。
