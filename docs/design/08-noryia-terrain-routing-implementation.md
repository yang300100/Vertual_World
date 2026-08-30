# Noryia 地形、路网与移动系统实施方案

状态：实施前方案。Noryia JSON/CSV/SVG 是唯一地理输入；旧北方大陆候选地形包不参与运行时。

## 目标与不变量

目标是在不允许模型直接修改世界状态的前提下，让任意一次移动由本地规则规划、持续推进和重新规划。

- 人物位置的唯一真相仍是经纬度；地图 SVG、PNG 和城镇图仅是视觉资产。
- 每次移动必须有一条可验证的路线折线，不能按直线插值跨海、跨河或穿山。
- Noryia `Full.json` 的格网、道路、河流、港口、城镇和国家数据是初始输入；CSV 是人文元数据补充。
- 规则引擎决定能否通行、耗时和速度；LLM 只能提出目的地、偏好或行动，不能指定越权路线。
- 所有自动生成的地形派生数据先标记为 `candidate`；审核通过后才成为 `approved` 并参与正式移动。

## 输入与生成产物

输入目录：`docs/worldbuilding/maps/map_new/data/`。

| 输入 | 用途 | 不直接用于运行时的原因 |
|---|---|---|
| `Noryia Full *.json` | 格网、顶点、高度编码、生物群系、道路点、河流点、港口/城镇/国家归属 | 约八千格以上的嵌套结构不适合每次心跳解析 |
| `Noryia Burgs *.csv` | 城镇、港口、人口、海拔与坐标 | 城镇表没有连续通行几何 |
| `Noryia Rivers *.csv` | 河流长度与流域的人文补充 | 运行时仍须使用 JSON 中 `cells` 几何 |
| `Noryia Routes *.csv` | 路线名称、类别和长度 | 运行时仍须使用 JSON 中 `routes.points` 几何 |
| `Noryia.svg` | 无标注视觉底图 | 不含可靠可通行性字段 |

新增生成器：`scripts/generate_noryia_navigation_data.py`。它读取完整 JSON 并生成：

```text
docs/worldbuilding/maps/navigation/noryia/
  metadata.json                 # 来源哈希、边界、版本、审核状态
  terrain.geojson               # 格网中心点及地形属性（开发审计用）
  terrain-index.json            # 经纬度 -> 格网索引的紧凑查找表
  surface.geojson               # 合并后的地表区域
  waterways.geojson             # 河流中心线与可航行等级
  roads.geojson                 # roads / trails / searoutes
  crossings.geojson             # 桥、浅滩、渡口、港口、山口
  route-graph.json              # 高层节点、边、折线、通行条件
  movement-rules.json           # 审核后的速度与通行参数
  audit-overlay.svg             # 人工审核叠加图，不供引擎读取
```

生成器必须写入 `source_sha256`、Azgaar 版本、地图经纬边界和每类要素数量。源文件哈希变化时，旧包自动失效，不能悄悄混用。

## 地形模型

Noryia 的 `pack.cells` 是运行时基本单元。每一个单元需派生：

```json
{
  "cell_id": 123,
  "longitude": 80.761,
  "latitude": 25.440,
  "elevation_code": 31,
  "elevation_m": 420,
  "surface_type": "grassland",
  "slope_degrees": 7.2,
  "water_kind": null,
  "state_id": 13,
  "province_id": 2,
  "road_ids": ["road:81"],
  "river_ids": [],
  "port_id": null
}
```

实现约定：

1. 高度码不是米；生成器采用可审核的单调映射，并把映射公式和海平面阈值记录到 `metadata.json`。
2. `h < 20` 统一为海水候选；内陆水、河流和海洋必须依 `feature`、`r`、`haven`、`harbor` 与河流几何进一步区分。
3. 坡度由相邻格网高度差与球面距离计算；不能从 SVG 颜色推断。
4. 生物群系 ID 映射为 `marine / desert / savanna / grassland / forest / rainforest / taiga / tundra / glacier / wetland` 等受控值。
5. 道路只在路线点附近的明确缓冲区生效；缓冲宽度按道路等级确定，不能把整块国家领土都算作道路。

## 数据库迁移

不要把每个像素写入 SQLite。SQLite 保存可检索的矢量和路线，紧凑格网索引保存在版本化 JSON 文件中。

新增表：

```sql
CREATE TABLE navigation_datasets (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  asset_root TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK(review_status IN ('candidate','approved','retired')),
  bounds_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_at TEXT
);

CREATE TABLE navigation_nodes (
  id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL REFERENCES navigation_datasets(id) ON DELETE CASCADE,
  node_type TEXT NOT NULL CHECK(node_type IN ('road','bridge','ford','ferry','port','pass','tunnel')),
  longitude REAL NOT NULL,
  latitude REAL NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE navigation_edges (
  id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL REFERENCES navigation_datasets(id) ON DELETE CASCADE,
  from_node_id TEXT NOT NULL REFERENCES navigation_nodes(id) ON DELETE CASCADE,
  to_node_id TEXT NOT NULL REFERENCES navigation_nodes(id) ON DELETE CASCADE,
  edge_type TEXT NOT NULL,
  distance_km REAL NOT NULL,
  speed_multiplier REAL NOT NULL,
  allowed_modes_json TEXT NOT NULL,
  polyline_json TEXT NOT NULL,
  requirements_json TEXT NOT NULL DEFAULT '{}',
  is_open INTEGER NOT NULL DEFAULT 1
);
```

给 `character_movements` 新增字段：

```sql
ALTER TABLE character_movements ADD COLUMN route_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE character_movements ADD COLUMN route_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE character_movements ADD COLUMN route_distance_km REAL NOT NULL DEFAULT 0;
ALTER TABLE character_movements ADD COLUMN navigation_dataset_id TEXT;
ALTER TABLE character_movements ADD COLUMN replan_reason TEXT;
```

迁移顺序：备份真实数据库 -> 临时副本演练 -> 创建表/索引 -> 导入 `candidate` 数据集 -> 人工审核 -> 标记 `approved` -> 仅新发起的移动使用路线 -> 最后为进行中移动做一次受控重规划。任何失败都保持旧直线移动可继续或可取消，不能损坏人物位置。

## 通行与速度规则

基础公式：

```text
实际速度 = 载具基础速度
         × 地表倍率
         × 道路倍率
         × 坡度倍率
         × 天气倍率
         × 人物状态倍率
```

首版固定规则：

| 模式 | 可通行 | 禁止或前置条件 |
|---|---|---|
| `land` | 陆地、道路、明确的桥/浅滩/渡口 | 海洋；无跨越门户的大河；坡度 >35° |
| `ship` | 可航行海域、可航河段、港口边 | 陆地；非港口处上岸；封闭航道 |
| `water` | 小艇可通行河段、湖泊、近岸水域 | 外海需 `ocean_capable=true` |
| `flight` | 陆地、水面、高山上空 | 载具升限、能源、禁飞区与天气限制 |
| `underground` | 已登记矿道/隧道边 | 任意直线钻地；坍塌或封闭矿道 |

建议倍率：道路 1.30–1.60，草原 1.00，稀树草原 0.90，森林 0.72，雨林 0.60，湿地 0.45，沙漠 0.55，冻原 0.50，冰川 0.20。坡度 0–10° 为 1.0，10–20° 为 0.8，20–35° 为 0.45，超过 35° 禁止普通陆行。具体数字须审核后写入 `movement-rules.json`，而非散落在 Python 常量中。

## 路线规划器

新增 `world_engine/routing.py`，公开接口：

```python
plan = planner.plan(
    world_id=world_id,
    origin=(longitude, latitude),
    destination=(longitude, latitude),
    movement_type="land",
    vehicle_metadata={...},
)
```

返回：路线折线、总距离、预计耗时、经过地形摘要、所需桥/港/渡口、不可达原因与数据集版本。

算法分三层：

1. **局部连接**：起点与终点连接到可用道路、港口、桥梁或山口；仅在附近格网做 Dijkstra/A*。
2. **高层网络**：在 `navigation_edges` 上用 A*，启发值为球面距离除以该模式允许的最大速度。
3. **局部收束**：从最后节点走到精确目的地；无合法收束边则报告不可达，不退回穿模直线。

路线边成本必须同时包含距离、速度倍率和模式限制。对 `land`，跨河只能使用 `bridge / ford / ferry`；对 `ship`，陆地目的地必须通过港口换乘；对 `flight`，可跳过地形边但仍要检查载具元数据。

## 对现有 MovementService 的改造

| 现状 | 替换方式 |
|---|---|
| 起点到终点的大圆距离 | `RoutingPlanner.plan` 的路线总距离 |
| `interpolate_coordinate` 直线插值 | 沿 `route_json` 当前折线段按距离推进 |
| 固定 `speed_kmh` | 每段按地形/道路/坡度规则重新计算有效速度 |
| 无通行验证 | 发起移动前验证路线；不可达时返回结构化原因 |
| 到达才更新位置 | 每个心跳更新路线段、经纬度、地形上下文和区域归属 |

心跳过程：读取当前路线段 -> 采样当前格网 -> 计算该段有效速度 -> 推进可走距离 -> 跨越一个或多个折线段 -> 更新人物坐标/区域 -> 检查遭遇、封路和路线有效性。不得在每次心跳重新跑全图 A*。

触发重规划：桥梁/港口/道路关闭、洪水、天气使模式失效、载具损坏或被切换、目的地变更。重规划失败时移动变为 `blocked`，记录原因与当前位置，允许玩家取消或更换载具。

## API 与前端

`POST /player/move` 的响应增加：

```json
{
  "movement": {"...": "..."},
  "route": {
    "distance_km": 83.4,
    "eta_world_time": "...",
    "segments": [{"surface":"road","speed_kmh":7.2}],
    "requirements": ["桥梁：北岸桥"],
    "dataset_status": "approved"
  }
}
```

前端地图：路线以折线显示；悬停显示当前地形、海拔、坡度、道路/河流和有效速度；不可达点击显示本地规则原因，例如“徒步无法穿越外海，需要船只或飞行载具”。不要让前端自行判断通行性。

## 测试与验收

必须新增以下测试，再允许切换为正式路线：

1. 陆行不能跨外海，船只不能在陆地发起移动，飞行可跨海。
2. 没有桥/浅滩/渡口时陆行不能跨河；存在桥时路线经过桥节点。
3. 高坡/高山被避开；若唯一通道为山口，路线经过山口。
4. 相同两点在道路上 ETA 小于荒野路线；离开道路后速度恢复地表倍率。
5. 路线在心跳中逐段推进，坐标不跳跃，累计距离不超过路线总距离。
6. 中途封桥使移动 `blocked` 或重规划；不会穿过关闭边。
7. 载具切换、取消、遭遇与区域归属保持现有行为。
8. 固定 Noryia 源哈希的快照测试：格网/道路/河流数量与生成结果一致。
9. `pytest -q`、`ruff check world_engine scripts tests`、SQLite `integrity_check`、浏览器地图交互均通过。

## 实施顺序

1. 编写生成器与产物完整性测试，不改运行移动。
2. 人工审核 `audit-overlay.svg`、水体分类、主要道路、河流、桥梁、港口和山口。
3. 增加导航数据表、导入器和只读地形采样 API。
4. 实现路线规划器，并以 API 测试验证不可达原因。
5. 改造 `MovementService.start/advance`，先仅启用新发起的移动。
6. 前端显示路线与地形上下文。
7. 备份真实库、临时演练、切换 `approved` 数据集、观察日志后再淘汰直线回退。

## 明确禁止的捷径

- 不根据 SVG 颜色推断地形或水体。
- 不把 CSV 路线长度当作人物实际路线。
- 不让 LLM 判定“能否过河/过海”。
- 不在每次心跳重建全图图结构。
- 不删除旧移动记录或覆盖人物历史坐标。
