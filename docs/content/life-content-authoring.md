# 文字生活世界：给内容填写模型的说明

适用日期：2026-09-12。此文件用于填写内容草案，不授权修改代码或正式数据库。

## 交接方式

将本说明、所选地点的 `catalogue.json`、五个 `schema-*.json` 和相关世界设定片段
交给内容模型。只给本批地点需要的设定，不必发送整个仓库。不要发送 `.env`、密钥、
运行日志、玩家私人笔记、人物私人记忆或未经筛选的存档文件。

模型输出一个 `content-draft.json`。文件先经过本地校验，再按条目提交元素注册，
由编年者核对和确认。模型声称“已经存在”“已经完成”不会使内容进入世界。

## 可以直接复制给模型的任务

```text
你负责为已有虚构世界编写生活内容草案。仅填写数据，不修改代码，不调用模型API，
不操作数据库，不替玩家或NPC执行行为。

请先阅读提供的填写说明、catalogue.json、schema-*.json和设定片段。
只使用目录中实际存在的地点、人物、房间和物品类型ID，不猜测UUID，不新增权限字段，
不把“建议添加”说成“现有事实”。遵守人物知识、地域法律、货币、物种和技术水平约束。
保留原设定；有冲突或依据不足时列出问题，不自行改写世界历史或人物身份。

每批围绕一个小镇、最多20项内容。先提供缺少的物品类型、资源来源、房间和工资规则，
等这些条目被确认且收到新版目录后，再写引用它们的配方及内层房间。

输出严格JSON，根字段为format_version、world_id、entries。
entries每项只有key、source_note、payload。payload符合对应schema。
source_note说明依据的设定文件/章节；自行提出的生活细节须明确标为“建议，待作者确认”。
不要写占位ID、markdown代码围栏、随机种子、初始骰点、人物台词或自动执行脚本。
不会填写或前置对象缺失的条目不要塞进JSON；另附简短issues.md解释即可。
```

## 生成材料的命令

先从本项目接口 `/api/worlds` 和 `/api/worlds/{world_id}` 获取世界与地点标识。
在项目根目录运行下面命令，将占位参数替换成实际 ID；`--location` 可以重复。

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_life_content --world "实际世界ID" --location "实际地点ID" --output "run/content-packet"
```

脚本以 SQLite 只读方式访问现有数据库，不调用 `initialize()`，不请求模型，不推进时间。
它只导出所选地点和相关人物的基础资料，以及配方所需的物品目录。默认不导出私人记忆、
角色提示词、人物目标、聊天历史或完整世界设定。输出的世界时间和版本是导出时的快照。

填写结束后：

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_life_content --validate "run/content-packet/content-draft.json" --catalogue "run/content-packet/catalogue.json"
```

通过检查只表示结构和目录引用没有已知错误，不代表故事设定、经济平衡或权属已经获准。
提交前重新导出目录，防止 ID、库存规则、房屋用途或账户状态已经变化。

## JSON 外壳

下面只是结构示意，带占位文字的示例不能直接导入。

```json
{
  "format_version": 1,
  "world_id": "从catalogue.world.id原样复制",
  "entries": [
    {
      "key": "town-food-01",
      "source_note": "依据所提供设定的粮食章节；具体储量为建议，待作者确认",
      "payload": {
        "element_type": "commodity",
        "name": "符合当地设定的食物名称",
        "category": "food",
        "stack_limit": 10,
        "slot_size": 1,
        "price": 3,
        "nutrition": 30,
        "resource_location_id": null,
        "resource_key": null,
        "resource_owner_id": null,
        "initial_resource": 0,
        "daily_growth": 0,
        "resource_capacity": 100
      }
    }
  ]
}
```

`key` 是本批审阅编号，不是数据库对象 ID，也不能被其他条目用作房间或物品引用。
`source_note` 是作者审阅信息，不自动注入 NPC 知识。只把 `payload` 送入相应登记接口。

## 五种可填写内容

精确类型、默认值、范围及禁止的额外字段，以随包从当前代码生成的 schema 为准。

| 类型 | 关键字段 | 需要守住的规则 |
| --- | --- | --- |
| `commodity` | name、category、price、nutrition、堆叠与格数、可选资源来源 | category 只支持 food/material/tool/map；售价单位沿用引擎整数货币，不自行增加兑换制 |
| `workplace_budget` | location_id、initial_funds、wage | 一个地点只能初始化一次账户；初始资金须有作者认可的来源，不代表每日无限补钱 |
| `interior_room` | location_id、parent_room_id、owner_character_id、access_policy、fixtures、nightly_rate | 私人房间、储物柜、出租房须有真实所有者；不能替玩家凭空取得房产 |
| `activity_recipe` | kind、location_id、duration_minutes、energy_cost、ingredients、产物或修理参数 | 原料和产物类型必须已登记；同一种原料合并数量，不把工具同时当消耗原料 |
| `npc_routine` | character_id、slots、时区、劳动额度、生活目标、expected_revision | 只定义习惯偏好，必须引用真实地点/配方；不补算旧工作或授予收益 |

### 商品、食物与资源

- 先查目录是否存在同类物品；已有类型应复用，不用换个名称反复造等价商品。
- 登记商品类型不会自动给商人背包补一堆商品。初始实物仍需已有合法来源，或通过
  已登记资源收取、生产配方产生。不要在草案里加 `initial_inventory` 等不存在的字段。
- `resource_location_id` 指定资源所在的地点；`resource_key` 是资源键。填写地点时**必须**
  同时填写资源键，否则无法定位要增长哪一池资源。
- 只填 `resource_key`、把 `resource_location_id` 留为 null 表示**通用资源**：任何地点都能采集
  该资源，适合“到处都能找到的野果、粮食”这类设定。两者都为 null 则是没有资源来源的普通物品。
- `resource_owner_id` 为 null 表示可合法公共收取；不是“主人暂时没想好”。通用资源也可以有所有者。
- 同地点的同一资源键只能定义一套来源规则，不能用不同商品重复增长同一资源池。此规则只针对
  有地点的资源；通用资源没有地点，可与其它来源并存。
- `initial_resource` 只在该资源键原本不存在时使用，不覆盖旧储量；容量不得低于旧储量。通用资源
  没有地点，`initial_resource` 不会写入任何地点；其存量由每日再生逐步累积。
- 食物的 `nutrition` 表示恢复饱食的规则数值。生原料不一定可直接食用，不能靠说明文字
  假装已经实现烹饪、食物腐败或天气对收成的影响。
- `daily_growth` 只能表示设定允许的可再生来源。矿石、稀有制品、货币不能无依据每日再生。

### 工资和生计

- 账户登记会让该地点具备有预算的工作机会；每次工作一小时，工资启动时预留，完成支付。
- 未登记账户的旧 workplace 仍保留既有兼容工资规则；草案要说明是否是在补齐该地点的预算。
- 给出一份简单的收支估算：一日基本食物、住宿费用、合理工作次数及账户能支付几天。
  这是作者审阅材料，写在 `source_note` 或另附说明，不能增加不支持的 schema 字段。
- 不修改正式人物的 money、skills、relationships 或所有权来让经济“看起来能运作”。

### 房间和家具

- 先登记外层房间，取得真实 ID 后再登记内层房间；嵌套最多四层。
- 同一层和地点的名称要易区分，不用多个同名“房间”“柜子”制造操作歧义。
- 家具当前支持 container 和 seat；床铺尚无独立家具规则，不能擅自填写 kind=bed。
- 房间可有最多十六件家具；命名不等于附带特殊效果。
- `nightly_rate=0` 表示未作为出租房登记；大于零表示每世界日租金。
- 出租内层房间需要可通行的外层公共通道。外层私人产权不能因为子房间出租而被绕过。
- 当前登记的是已有布局；新建房屋、房产转让继续走原建设/产权流程，不用此表冒充施工。
- `key_item_type_id` 可引用本世界已登记、不可堆叠的工具类型作为实体钥匙。不填则沿用旧权限锁；登记房间不会自动发钥匙。先核对真实钥匙来源，钥匙和合法进入许可缺一不可。
- `visitor_policy` 可选 `manual`、`authorized_only`（默认）、`trusted_contacts`。后者允许空闲在家的NPC开门查看后邀请认识且信任≥20/好感≥10的访客；不编写NPC台词。
- 应门邀请仅15世界分钟内可进入一次，不含柜子权限。租期内由当前租客接待；设定中需要完全不自动应门的房间使用 manual。

### 制作与修理

食品批次补充：食物商品可填 `shelf_life_hours`（1–8760）。前75%时长新鲜，后25%饱食收益减半，到期变质。
不填则保持旧规则并显示“未登记保鲜期限”；不要为旧实例猜测生产时间。柜中、转手、材料预留和成果等待领取都不重置期限。
配方需留足食材可用时间，原料在工序中变质会中止；当前没有冷藏、温度和密封修正字段。
请在 source_note 写保鲜时长的设定依据，不手填实际批次时间或人物不适。

- kind=craft 必须有 output_item_type_id；只有原料真实存在并完成耗时才产出。
- kind=repair 必须有 repair_category，不能同时填写额外产物。
- 修理对象是一件玩家自己持有、类别匹配且已损坏的物品，不由草案直接指定正式物品实例。
- 修理可填 check_skill、check_difficulty、check_min_proficiency、check_tool_type_id。
  内容模型只能给有依据的规则建议；随机结果由服务器生成，不能填成功与失败的实际结果。
- `tools` 为最多8种工具的数组，每项为 `{"item_type_id":"目录中的类型ID","wear":2}`；每种占用1件，`wear` 为完整工序磨损（1–100）。工具不得重复或与原料重合。
- 工具必须由人物自己随身持有，完好度至少达到 wear；开始后预留，完成（包括修理失败）按规则磨损，中止按时间比例向上取整磨损，零耗时中止不磨损。归还物品留在原活动地点领取。
- `check_tool_type_id` 仍是修理的检定工具；与 tools 同类型只占用一件并使用 tools 的磨损值。未在 tools 声明的旧检定工具，每次新开工默认磨损1。升级前已开始的活动沿用旧快照。
- 周期制作可填写时段 `supply_purchase_budget`（默认0、不自动购买）；这是每次时段总预算，不是每件预算。消费后还须保留人物 `income_reserve`。NPC每15世界分钟最多准备1份真正短缺的材料或工具，只收取当前地点的公共/本人资源，或从同地点、同房间、100米内商人的真实库存购买。
- 备料还需要留足15分钟及完整工序时间；失败、预算不足或缺少可用来源时暂缓。未用原料继续属于购买者。不要为使配方运行而凭空登记资源或资金。
- 制作质量随机检定、开锁、火候、辅助人数和多阶段工序尚不是本表支持的字段。

## 推荐的分批顺序

NPC作息可增填 `ambitions`（最多8项）作为一次性生活目标。每项有 key、title、kind、target、priority，
可指定 depends_on、deadline_world_time。储蓄使用 `reserve_money`，制作随身库存使用 `craft_stock` 并引用 recipe_id。
采购上限由 purchase_budget 控制，仍保留 income_reserve；工作地点来自 work_location_ids 和已声明工作时段。
依赖不能缺失或成环，工作和配方ID必须先登记；不要在草案中填写实际进度、完成状态、步骤或资金。
目前不支持学习、社交、迁居等目标类型。详见[目标实施记录](../design/24-p0-npc-life-goals.md)。

1. 核对所选地点现有居民和设定，列缺口；不要重复生成已有 NPC。
2. 填基础商品、确有依据的资源来源、外层房间和工资账户。
3. 编年者审核并确认后，重新导出目录，获得真实的新物品/房间 ID。
4. 填生产或修理配方、内层房间；先让一个小镇的基本食宿和工作成立。
5. 实际观察 NPC 是否能拿到原料、完成工作、获得和食用食物，再调整数量与价格建议。

不要一次铺满全部城市。人物数量、技术、货币和法律约束要以提供的当地设定为准。
角色卡、秘闻和世界历史扩写不属于此工具的五种内容；单独审阅，不混进 payload。

## 确认登记的接口

| 内容 | 提交接口及请求体 |
| --- | --- |
| 商品、工资 | `POST /api/worlds/{world_id}/economy-definitions`，`{"idempotency_key":"新唯一请求键","payload": payload}` |
| 房间 | `POST /api/worlds/{world_id}/interior-layouts`，`{"idempotency_key":"新唯一请求键","room": payload}` |
| 配方 | `POST /api/worlds/{world_id}/activity-recipes`，`{"idempotency_key":"新唯一请求键","recipe": payload}` |

提交产生 proposed 登记，并不直接生效。编年者核对后使用页面“确认登记”，或
`POST /api/worlds/{world_id}/registrations/{registration_id}/confirm`。
同一网络请求重试复用 idempotency_key；修改了内容则使用新键，不能重放不同内容。
本工具没有批量自动确认或直接写库选项。


## 周期作息填写补充（2026-09-12）

在房间、工资规则和制作配方确认后，再填写 `npc_routine`。
`catalogue.npc_routines` 包含所选人物已有配置及当前 revision；修改已有作息时，
把当前 revision 填入 expected_revision。首次登记不填 expected_revision。
一天的时段应留出通勤、吃饭和临时事务余量，不要排满24小时。

- `weekdays` 使用0至6，分别是周一至周日；不重复。`starts_at` 为24小时制 HH:MM。
- `utc_offset_minutes` 是该人物作息时刻相对世界标准时的偏移。默认0；480表示 UTC+08:00。
  不要直接把填写模型或开发电脑的当地时区当作世界设定。
- `duration_minutes` 是可安排活动的时间窗，允许跨午夜，最多720分钟；跨周重叠也会拒绝。
- `target_minutes` 是本时段希望完成的实际活动量，可省略。工作按完整60分钟单位，
  制作按配方的完整耗时；默认取窗口内可容纳的完整活动量。剩余不足一整次时不会凭空补齐。
- `kind` 当前支持 work、rest、craft；craft 必须引用目录中的真实 recipe_id。
  作息可以引用既有寝处所在地点，但不会凭空获得房间权限或占用已租给别人的客房。
- `visibility` 默认为 private；只把明确可以告知居民的营业或工作时间设为 public。
  公开视图只显示时段、用途与地点，不公开私人储备目标或替代工作选择。
- `goal=stable_routine` 表示保持固定习惯；`maintain_reserve` 表示达到 income_reserve 后
  可以略过该时段的额外补工。未执行会记录为略过，不能写作完成。
- `max_work_minutes_per_day` 控制日常规划器的劳动额度；实际已有工作/制作消耗也计入。
  人物的其他独立决策仍由既有动作规则处理，作息不是替代所有行动权限的强制命令。
- `allow_alternative_work` 允许原工作地点关闭或缺少工资预算时，在 local_search_radius_km
  范围内选择可抵达的替代工作点。它不授权改雇佣合同、转移所有权或凭空补发工资。
- `enabled=false` 用于停用已有作息，同样需要 expected_revision。修改配置不追溯执行过去，
  也不取消已经开始的活动；这些活动仍按各自冻结的规则结算。

提交：`POST /api/worlds/{world_id}/npc-routines`，请求体为
`{"idempotency_key":"新唯一请求键","routine": payload}`。先提交 proposed，再由编年者确认登记。
不得填写“已完成多少分钟”“今天赚了多少钱”或实际失败结果；这些由运行记录生成。
