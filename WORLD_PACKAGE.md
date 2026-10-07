# 可插拔世界观包

把一份世界观放在项目的 `docs/` 目录，重启 API 和 worker，即可进入它的存档。目录位置也可用环境变量 `WORLD_PACK_DIR` 指定。世界观文件在启动时读取；更换文件后需要重启进程。

## 目录结构

```text
docs/
├── world.json                 # 必需：世界观清单与稳定 ID
├── world/
│   ├── locations.json         # 必需：地点
│   ├── characters.json        # 必需：初始 NPC；可为 []
│   ├── species.json           # 必需：物种
│   ├── maps.json              # 必需：地图；可为 []
│   └── map_features.json      # 可选：地标等地图要素
├── knowledge/                 # 可选：Markdown 世界设定，目录名由清单指定
└── assets/                    # 必需：地图图片/SVG，目录名由清单指定
```

文件名和子目录名可以改变，只要 `world.json` 的相对路径指向它们。示例清单：

```json
{
  "format_version": 1,
  "id": "my-world",
  "revision": 1,
  "name": "我的世界",
  "start_time": "2050-01-01T08:00:00+00:00",
  "locations": "world/locations.json",
  "characters": "world/characters.json",
  "species": "world/species.json",
  "maps": "world/maps.json",
  "map_features": "world/map_features.json",
  "knowledge": ["knowledge"],
  "knowledge_exclude_files": [],
  "assets_root": "assets",
  "map_layers": [{"asset_path": "world.svg", "label": "世界地图"}],
  "player": {"species": "human", "money": 0, "skills": [], "items": []}
}
```

`id` 只能由小写英文字母、数字及连字符组成，最长 64 字符。它是**存档身份**，不是显示名称。`revision` 是正整数。`start_time` 必须含时区。`map_layers` 是地图样式选择器展示的图片路径，路径相对于 `assets_root`；不提供时显示 `maps.json` 的默认地图。
`knowledge_exclude_files` 可列出知识目录里不参与运行时检索的 Markdown 文件名；新包默认不排除任何文件。

## 数据文件格式

最小示例（四段内容分别保存为所示文件）：

`world/locations.json`：

```json
[{"key":"town","name":"起点镇","kind":"town","longitude":10,"latitude":20,"resources":{},"area_radius_km":2}]
```

`world/characters.json`：

```json
[{"key":"guide","name":"向导","species":"human","location":"town","identity":"当地居民","traits":["友善"],"goals":["守护小镇"],"is_core":true}]
```

`world/species.json`：

```json
[{"id":"human","display_name":"人类","gestation_world_days":270,"adult_age_world_years":16,"compatible_species":["human"]}]
```

`world/maps.json`：

```json
[{"key":"world","name":"世界地图","asset_path":"world.svg","width_pixels":3600,"height_pixels":1800,"min_longitude":-180,"max_longitude":180,"min_latitude":-90,"max_latitude":90}]
```

地点、人物、地图和地图要素的 `key` 在各自文件中必须唯一；人物的 `location` 引用地点 `key`。子地点可用 `parent` 引用上级地点的 `key`。地点坐标使用经度 −180～180、纬度 −90～90；经纬度与地图投影应匹配。地图可用 `location` 绑定详细地图，并设置 `map_role: "detail"`、`zoom_level: 2` 和四项边界。`map_features.json` 每项包含 `key`、`name`、`feature_type`、`longitude`、`latitude`，可选 `location`、`metadata`。

`player.items` 中每项需要 `type_id`、`name`、`category`、`quantity`，可选 `container`（`character_inventory` 或 `character_equipment`）、`stack_limit`、`usable`、`attack_bonus`、`defense_bonus`、`heal`。设为空数组即可不赠送初始装备。

## 存档与切换

- 首次加载新 `id` 时自动创建世界。后续启动直接打开该 `id` 的原存档，不重新播种初始资料。
- 当前诺里亚包的 `id` 是 `iserra-noryia`，继续使用原来的 `data/world.db`。其他包使用 `data/world-saves/<id>/world.db`。`WORLD_DB_PATH` 可更改存档根路径和文件名。
- 把另一个包换入 `docs/` 并重启后，会切到它自己的存档；换回原包并重启后恢复原断点。请保留原包文件和对应数据库。
- 系统核对 `id`、`revision` 以及清单、结构化数据和 Markdown 知识的内容。已有存档遇到同 ID 的不同内容时会拒绝启动并报错，避免新设定悄悄覆盖旧事实。兼容性需要另行迁移；要创建独立世界，可改用新 ID。
- 地图二进制资源必须存在，但图片内容不计入存档兼容指纹；可以重新绘制同坐标的地图。所有清单路径必须留在 `docs/` 内。

现在的诺里亚包示例在 [docs/world.json](docs/world.json) 和 [docs/world/](docs/world/)；其图片与知识仍放在现有 `docs/worldbuilding/`、`docs/knowledge/` 中，由清单引用。模拟引擎的通用规则仍由程序实现；此格式管理世界的初始事实、知识和展示资源。
