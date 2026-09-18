# 意图优先的交互重构（设计）

日期：2026-09-18
状态：已与项目所有者确认，待写实现计划

## 一、背景

旅人书当前的交互有一条根本性的错位：**表单漏到了玩家一侧**。

玩家的世界里本该只有语言。价格是否等于登记价、物品是否在卖家库存里、钱够不够——这些都是引擎的私事。但现在它们以常驻表单和参数弹窗的形式暴露在界面上，于是同时造成两种病症：

- **不真实**：你说"我想买点东西"，世界给你弹一张 `数量: [__]` 的表格。
- **学习成本高**：你得先学会"买卖要去哪儿点、制作要去哪儿填"，才能开始玩。

三个具体抱怨都指向这同一个根因：

1. 场景页并排放着 6 个独立表单区（`index.html:59-74`），把一个语言世界拆成了操作面板。
2. 意图检测误触发——随口提到"购买"就会弹购买对话框。
3. 购买弹窗只能填一个数字，无法与对方商讨价钱。

## 二、主线原则

> **玩家只面对语言；表单是引擎的私事。**

展开为两条方向相反的默认值：

1. **你不明确说的，世界当没发生。**（默认静止——治误触发）
2. **你一旦明确说了，世界不拿表单挡你。**（默认放行——治栏目碎片化）

第 3 个抱怨是第 2 条的必然推论：如果世界不拿表挡你，它就该像人一样跟你**商量**，而不是让你填数量。

## 三、已确认的决策

| 议题 | 决策 | 备选与否决理由 |
|---|---|---|
| 动作入口 | **输入框 + 动态芯片** | 芯片只是替你打字，不是旁路，入口仍唯一 |
| 判定门槛 | **语气定意图；可行性用语言回答** | 严格三重校验会造成"说了没反应"，比误弹更伤 |
| 弹窗形态 | **弹窗内自由对话** | 固定按钮把玩家锁在预设的几条路里 |
| 承接判定 | **收紧：买家要有货，卖家要有钱** | "在场有商人就弹"太宽，且三处商贩词表互相不一致 |
| 议价代价 | **不做**——砍价就是砍价 | 不加好感惩罚，也不引入库存压力 |
| 交易窗续谈 | **不做**——关了就是关了 | 不做会话存活期，避免管生命周期 |
| 浮动桌面 | **保留**（技能/背包/装备/交通） | 它们是"查看"性质，不与动作入口冲突 |
| 活动状态条 | **保留并提升为顶部条** | 进行中的活动必须可见 |

## 四、第一部分：场景页重构

### 4.1 重构后的场景页

```
┌─ 场景 ──────────────────────────────────┐
│ 当前场景                        时间 · 1× │
│ 地点 · 坐标                               │
├──────────────────────────────────────────┤
│ 人物 HUD：生命 / 精力 / 饱食 / 钱币        │
│ 叙述……                                    │
│ ── 行动结果 ──                            │
├──────────────────────────────────────────┤
│ ◆ 进行中的活动                ▓▓▓░░ 40%  │ ← 原 #life-progress
│   [ 结束当前活动 ]  [ 等候到下一处变化 ]   │ ← 原 #life-stop / #life-advance
├──────────────────────────────────────────┤
│ 生活与见闻 ▸（只读折叠，原样保留）          │
│ 在场的人                                  │
└──────────────────────────────────────────┘

┌─ 底部输入框（#player-intent-form）────────┐
│ 你此刻想做什么？                          │
│ [_________________________________]      │
│ ⟨休息 30 分⟩ ⟨吃块面包⟩ ⟨看货摊⟩           │ ← 动态芯片
│ ⟨和铁匠说话⟩ ⟨打一把匕首⟩                  │
│ 说话方式 [普通 ▾]          [ 写入世界 ]    │
└──────────────────────────────────────────┘
```

### 4.2 旧栏目去向

| 原栏目（`index.html`） | 新形态 |
|---|---|
| 在这里停留（`#life-form`） | 芯片「休息 30 分」；说"我想睡会儿"→ 弹窗只问时长 |
| 进行工作或制作（`#task-start-form`） | 芯片「打一把匕首」；说"我去干点活"→ 弹窗选配方 |
| 房间与家具（`#interior-panel`） | 说"把门关上"直接执行；芯片「打开箱子」 |
| 眼前物品与随身物品（`#life-items`） | 芯片「吃面包」「捡起匕首」 |
| 附近的买卖与住宿（`#living-market`） | 说 / 芯片 → **交易会话窗** |
| 生活与见闻（`.life-panel` 第一个） | 原样保留（只读） |
| 进行中的活动进度 | **提升为顶部状态条**（见 4.1） |

### 4.3 动态芯片

改造现有的 `#suggested-actions`（`app.js:415-419`，当前写死四条）。

- **来源**：后端按「所在地点 + 在场的人 + 背包 + 精力饱食 + 当前活动」计算。
- **行为**：点击 → 填进输入框（复用现有 `fillIntent`，`app.js:421`）→ 照常走判定。**不是旁路**。
- **限量**：最多 6 个，其余收进「…」菜单。背包有 20 件东西时不得撑爆布局。
- **排序**：最小可解释规则，不引入打分模型——①在场的人（可交谈/可交易）→ ②背包里此刻可用的物（能吃能穿能用）→ ③所在地点的活动（可采资源、可做的工作）→ ④基础项（观察四周、休息）。同类内按相关性截断。
- **接口**：新增 `GET /api/worlds/{world_id}/player/suggestions`，返回 `list[SuggestedAction]`。

## 五、第二部分：意图判定链

### 5.1 现状与缺口

当前判定链（`intent_parser.py`）：

```
LLM 分类 → operation="purchase" → requires_form=True（:74，无条件）
    ↓
_visible_targets（:143-154）：只过滤"同房间 + 100m 内 + 活着"
    ↓
LLM 未给对象时，静默采用 preferred_target_id（:76-78）
```

**缺口**：`learn` 有意愿显式性守卫（`_is_explicit_learning_request`，`:126-141`），prompt 里也有对应反触发条款（`:106-107`）；`purchase` 两样都没有。这不是难修的 bug，是漏写的一层。

`:76-78` 的静默采用是"随口提个买字就弹窗"的直接帮凶：提到附近某人的名字 + 句中出现"买"字，就能凑出一张填好的购买表单。

### 5.2 新的判定链

```
玩家输入
   │
   ├─ 显式步骤（动作:/说话:）→ 现有序列逻辑，不变（:66-67）
   │
   ▼
LLM 分类 → operation + explicit（新增字段）
   │
   ├─ explicit == false            → operation=none，普通对话
   ├─ 规则兜底判定为疑问/假设/否定  → operation=none（防 LLM 误判）
   ├─ operation == none            → 普通对话
   │
   └─ 承接判定：找得到能卖这件货的人吗？
        · 指名了人 → 他必须先通过身份判定，且库存里确实有这件在售
        · 没指名   → 在场必须有通过身份判定的人，且他有在售的货
        · 指名了物品 → 该物品必须能匹配到在售货品
        · 通过     → 弹交易窗
        · 不通过   → 普通对话（走 /player/act，对方会开口回你）
```

**判不准时走普通对话，不是沉默。** 这一点不需要任何新机制——`/player/act` 本来就会让 NPC 回一句话。反倒是"沉默"才需要专门写。

### 5.3 语气判定

`IntentParseResult`（`intent_parser.py:19-27`）新增字段：

```python
explicit: bool = False   # 这是不是"我此刻要执行"的宣告
```

prompt（`:100-113`）补充：

```
explicit=true 仅当玩家以祈使或宣告语气明确表示此刻要执行该操作；
以下一律 explicit=false，operation=none：
  · 疑问（"我该去哪买药"）
  · 假设（"如果我有钱就买下它"）
  · 否定（"我今天不买了"）
  · 闲谈中提及（"我听说铁匠卖匕首"）
```

规则兜底（沿用 `learn` 的做法，从 `:127` 提升为通用机制，按 operation 分表）：

```python
_EXPLICIT_MARKERS = {          # 命中则强制 explicit=true
    "purchase": r"(我要|我想|给我|替我|帮我|请|麻烦).{0,8}(买|购|收下|买下)"
                r"|^(买|要|来|收)(这|那|一|两|三|几|\d)",
    "sell":     r"(我要|我想|替我|帮我|请).{0,8}(卖|出售|脱手)"
                r"|^(卖|出|处理)(这|那|一|两|三|几|\d)",
    # 其余 operation 同理
}
_NEGATIVE_MARKERS = re.compile(   # 命中则强制 explicit=false
    r"[？?]\s*$|(吗|呢|怎么|哪里|哪儿|多少|是不是|愿不愿)\s*[。！!]?$"
    r"|^\s*(如果|要是|假如|若是|万一)"
    r"|(不买|别买|不卖|不想买|没打算)"
)
```

**规则是双向兜底，LLM 的 `explicit` 才是主判**：

| 情况 | 结果 |
|---|---|
| `_NEGATIVE_MARKERS` 命中 | 强制 `explicit=false` —— 防 LLM 把疑问/假设/否定当成宣告 |
| `_EXPLICIT_MARKERS` 命中 | 强制 `explicit=true` —— 防 LLM 漏掉"买这个"这类不带"我要"的直接祈使 |
| 两者都不命中 | 听 LLM 的 |

> **这是 `learn` 现有语义的放宽，属于预期变化。** 现在是"必须命中 `_is_explicit_learning_request` 才放行"（`:71-73`），统一后变成"LLM 说 explicit 且规则不否决即放行"。学技能会比现在更容易弹窗——可以接受，因为承接判定（5.4）会兜住真正做不成的那些。

**LLM 不可用时一律当普通对话，绝不按关键词臆测**——这是项目原有原则（`:54-55` 的类注释、`:95-96`），继续守。

### 5.4 承接判定与商贩词表统一

**当前三处商贩判定词表不一致**，导致"铁匠算不算商人"在不同代码路径下答案不同：

| 位置 | 用途 | 接受的词 |
|---|---|---|
| `economy.py:82-85` | 找供货商 | `商` `店` `贩` `医` `匠` `厨` |
| `intent_effects.py:74` | 玩家卖出的买家 | `商` `贩` `店` |
| `intent_effects.py:128` | 玩家购买的卖家 | `商` `贩` `店` |
| `intent_effects.py:114` | 修理服务承接人 | `铁匠` `修理` `马具` |
| `intent_effects.py:120` | 治疗服务承接人 | `药` `医` `店` |
| `daily_life.py:374` | NPC 自动生产 | `商` `匠` `厨` `店` `医` |

**统一为一个模块**：新建 `world_engine/merchants.py`，按业务语义分组而非按调用点复制：

```python
SELLER_MARKERS  = ("商", "店", "贩", "医", "匠", "厨")   # 能买卖货物的人
REPAIR_MARKERS  = ("铁匠", "修理", "马具")               # 能接修理的人
HEALER_MARKERS  = ("药", "医")                           # 能接治疗的人

def is_seller(identity: str) -> bool
def is_repairer(identity: str) -> bool
def is_healer(identity: str) -> bool
```

**合并词表必然改变单点行为**，逐点列明，且这些变化都是预期内的：

| 调用点 | 原词表 | 统一后 | 变化 |
|---|---|---|---|
| `economy.py:82` 找供货商 | 商店贩医匠厨 | 同左 | 不变 |
| `intent_effects.py:74` 玩家卖出的买家 | 商贩店 | 商店贩医匠厨 | **变宽**：铁匠/医生/厨师也能收货 |
| `intent_effects.py:128` 玩家购买的卖家 | 商贩店 | 商店贩医匠厨 | **变宽**：口径与供货商一致 |
| `intent_effects.py:114` 修理承接人 | 铁匠修理马具 | 同左 | 不变 |
| `intent_effects.py:120` 治疗承接人 | 药医店 | 药医 | **变窄**：单纯的"店"不再能治病 |
| `daily_life.py:374` NPC 自动生产 | 商匠厨店医 | 同左 | 不变 |

两处变宽是修复——"铁匠能不能卖匕首"不该在不同代码路径下有两个答案。一处变窄也是修复——"杂货店治病"本来就不成立；药店仍含"药"字，不受影响。

**承接判定的实现**：在 `intent_parser.py` 内新增，按方向分别判定。

买入方向（玩家说"我要买X"）：

```
可承接 = is_seller(identity)
         且 该人 character_inventory 里有 quantity > 0 的货
         且 该货在 world_item_profiles 里有 price
```

卖出方向（玩家说"我把X卖给他"）：

```
可承接 = is_seller(identity)
         且 该人有足够余额支付
       （不要求他持有这件货——他是买家，不需要先有货）
```

`trade`（方向未定）按买入方向判定，交易窗同时展示两个方向（见 6.5）。

库存查询沿用 `economy.py:86-92` 的模式（`item_instances` 中 `container_type='character_inventory'` 且 `owner_character_id = 本人`）。

### 5.5 顺带修掉的静默采用

`intent_parser.py:76-78` 改为：**只在 LLM 明确给出 `target_character_id`、且该 id 在可见范围内时**才使用。`preferred_target_id` 不再对操作类意图兜底。

（`preferred_target_id` 仍用于普通对话的目标解析，那条路径不变。）

## 六、第三部分：交易会话

### 6.1 核心分层

```
玩家说话
   ↓
LLM 只解析「议价动作」  →  {action: 问价|还价|接受|放弃|闲谈, offer_amount: 8}
   ↓
规则层裁定「价格」      →  _resolve_haggle(seller, player, item, offer) → 12
   ↓                        （依据：性格强度 + 亲和 + 信任）
LLM 只负责「把它说出来」 →  “最少十二个。”
   ↓
前端：当前价 12 铜币，[成交] 才扣钱
```

> **LLM 永远碰不到价格。** 它只能产出"玩家出价"和"NPC 的那句话"。否则模型一高兴打个三折，整个经济就废了。

原来的 `intent_effects.py:139-140`（报价必须严格等于登记价）继续保留，但它只服务 `/player/act` 那条老链路。交易会话走 `/commit`，价格由会话自己携带，不经过那句正则。

### 6.2 接口

沿用项目的 `build_*_router` 工厂模式（`api.py:317-328`），新建 `world_engine/api_trade.py`，导出 `build_trade_router(database, engine)`，在 `create_app` 中 `include_router`。

| 方法 | 路径 | 请求体 | 返回 |
|---|---|---|---|
| POST | `/api/worlds/{world_id}/player/trade/sessions` | `{target_character_id}` | `TradeSessionView` |
| POST | `/api/worlds/{world_id}/player/trade/sessions/{sid}/say` | `{text}` | `TradeTurnView` |
| POST | `/api/worlds/{world_id}/player/trade/sessions/{sid}/commit` | `{item_id, quantity}` | `PlayerActionResult` |
| DELETE | `/api/worlds/{world_id}/player/trade/sessions/{sid}` | — | `{status: "closed"}` |

与现有 `POST /player/trade`（`living_api.py:405-469`，结构化交易）**并存**，路径不冲突。前端新增交易走会话，旧结构化接口保留给脚本与测试。

### 6.3 数据结构

```python
class TradeOffer(BaseModel):
    item_id: str
    name: str
    listed_price: int      # world_item_profiles.price
    current_price: int     # 议价后的当前价
    quantity: int          # 卖家可售件数
    direction: Literal["buy", "sell"]   # 玩家买入 / 玩家卖出

class TradeSessionView(BaseModel):
    session_id: str
    seller_id: str
    seller_name: str
    seller_identity: str
    offers: list[TradeOffer]
    transcript: list[TradeTurn]
    opened_at: str         # 世界时间

class TradeTurn(BaseModel):
    speaker: Literal["player", "npc"]
    text: str
    price_change: dict[str, int] | None   # item_id → 新价

class TradeTurnView(BaseModel):
    npc_reply: str
    offers: list[TradeOffer]              # 价格刷新后
```

会话状态**持久化到数据库**，不留在内存——世界可能在世界时间上跳跃，进程也可能重启。两张新表：

```sql
trade_sessions(
  id                  TEXT PRIMARY KEY,
  world_id            TEXT NOT NULL,
  player_id           TEXT NOT NULL,
  seller_id           TEXT NOT NULL,
  status              TEXT NOT NULL,          -- open | committed | closed
  opened_world_time   TEXT NOT NULL,
  closed_world_time   TEXT,
  transcript_json     TEXT NOT NULL DEFAULT '[]'
);

trade_session_prices(
  session_id     TEXT NOT NULL,
  item_id        TEXT NOT NULL,
  listed_price   INTEGER NOT NULL,
  current_price  INTEGER NOT NULL,
  PRIMARY KEY (session_id, item_id)
);
```

对话记录用 JSON 列存（会话短、顺序读、不需要跨会话检索）；价格单独成表（`/commit` 要按 `item_id` 取当前价，需要索引）。迁移沿用 `migrations.py` 的增量机制。

`/commit` 返回现有 `PlayerActionResult`（`domain.py:268`），以便前端沿用 `renderActionResult`（`app.js:713`）渲染结果，不新增渲染路径。

### 6.4 议价规则

```python
floor_ratio = 0.85                                   # 默认最多让 15%
            - max(0, affinity) / 100 * 0.20          # 关系好 → 让更多
            - max(0, trust)    / 100 * 0.15
            - 慷慨性格(豪爽/慷慨/大方) * 0.10
            + 算计性格(吝啬/精明/贪婪) * 0.10
floor_ratio = clamp(floor_ratio, 0.50, 1.00)
floor_price = max(1, int(listed_price * floor_ratio))
```

依据来源：

| 依据 | 位置 | 取值 |
|---|---|---|
| 性格强度 | `character_traits.intensity`（`schema.py:69-76`） | 0–100，`CharacterGrowthService.level()`（`character_growth.py:125-129`）可读 |
| 亲和 / 信任 | `relationships.affinity/trust`（`database.py:255-264`） | −100..100，有向；读玩家→卖家的那条 |
| 性格词 | `characters.traits_json`（`database.py:86`） | 字符串匹配「豪爽/慷慨/大方」与「吝啬/精明/贪婪」 |

**不引入**库存压力依据：没有一个商人因库存积压而压资金，"持有件数多就愿意让价"没有因果，删掉。

**不引入**砍价惩罚：砍价就是砍价。关系的回报体现在**能砍到多低**，而不是砍价会被记账。

议价裁定的行为：

- `offer >= floor_price` → 接受该出价。
- `offer < floor_price` → 还价到 `floor_price`（不接受低于下限的出价）。
- 玩家未出价（只问价）→ 报 `current_price`。
- 玩家接受 → `current_price` 保持，等待 `/commit`。

`/commit` 时重新校验身份、钱、货、房间，任一不符返回 409 并说明。

### 6.5 前端

`openIntentForm`（`app.js:706`）与 `canonicalIntentFromForm`（`app.js:712`）的购买/出售分支**退役**，改为打开交易会话 `<dialog>`。

`intentFormLabels`（`app.js:704`）中 purchase/sell/trade 三项移除；其余（`transfer`/`learn`/`repair`/`heal`/`letter_exchange`）**保持现状不动**——它们没有议价需求，本轮不碰。

交易窗结构：

```
┌─ 铁匠铺 · 交易 ──────────────────┐
│ 对话记录（可滚动）                 │
│                                  │
│ 你的话：________________          │
│ [ 说话 ]                         │
│                                  │
│ 他卖 ────────────────            │
│   匕首     · 14 铜币  ×1          │
│   铁锭     ·  3 铜币  ×7          │
│ 他收 ────────────────            │
│   毛皮     ·  2 铜币  ×3          │
│                                  │
│ [ 成交 ]  [ 走人 ]                │
└──────────────────────────────────┘
```

窗口**同时展示两个方向**：他卖的（玩家买入）与他收的（玩家卖出，回收价沿用 `living_api.py:394` 的标价一半）。玩家直接说"这把匕首我要了"或"这些毛皮你收不收"来定方向，**不需要先在表单里选"交易方向"**——原 `trade` 的那个下拉（`app.js:706`）随之退役。

原生 `<dialog>` + `showModal()`，与现有 `#intent-form-dialog`、`#photo-dialog` 同机制。「走人」= `DELETE` 会话并 `close()`；**不做续谈**。

## 七、实施分期

| 阶段 | 内容 | 为什么这样切 |
|---|---|---|
| **一** | 5.2–5.5 判定链 + 4.1–4.3 场景页重构 | 不引入新概念，纯收敛。做完抱怨 ② 立刻消失 |
| **二** | 6.1–6.5 交易会话 | 依赖阶段一（弹窗触发逻辑要先对） |

阶段一可独立上线并验收；阶段二依赖阶段一完成。

## 八、测试策略

现有 `tests/test_player_intent.py` 有一处结构性缺口：`:443` 的用例**直接桩掉 LLM**、喂 `operation="purchase"` 进去，测的是"表单长什么样"，从未测过"这句话该不该弹"。而 `:490` 给 `learn` 写了反误判用例，`purchase` 一条都没有。

**新增用例：**

```
语气判定
  "我该去哪买药呢"          → 不弹（疑问）
  "我今天不买了"            → 不弹（否定）
  "如果我有钱就买下它"       → 不弹（假设）
  "我听说铁匠卖匕首"         → 不弹（闲谈提及）
  "我要买这个面包"           → 弹（宣告）
  "买这个"                   → 弹（直接祈使，规则一票通过）
  "怎样才肯教我打铁"         → 不弹（learn 的条件探询，现有用例 :490 继续有效）

承接判定
  "我要买这个面包" + 铁匠有面包    → 弹
  "我要买这个面包" + 铁匠只有匕首   → 不弹，走普通对话
  "我要买把剑"     + 在场只有农夫   → 不弹，走普通对话
  "我要买把剑"     + 商人在场且库存有货 → 弹
  "我把毛皮卖给他" + 他是商人且有余额   → 弹
  "我把毛皮卖给他" + 他是商人但余额不足 → 不弹，走普通对话
  "我把毛皮卖给他" + 在场只有农夫       → 不弹，走普通对话

议价规则
  陌生人（affinity=0, trust=0）  → floor_ratio = 0.85
  老交情（affinity=100, trust=100） → floor_ratio = 0.50
  吝啬性格                        → floor_ratio 上抬
  出价低于下限                    → 还价到下限，不成交
  出价高于下限                    → 接受该出价

商贩词表
  统一前后各调用点对同一 identity 的判定结果一致

交易会话（阶段二）
  开 → 说 → 成交 全流程
  /commit 时卖家离开房间 → 409
  /commit 时钱不够       → 409
  会话关闭后再 /say      → 404

回归
  现有 tests/test_player_intent.py 全部通过
  现有 tests/test_api.py 中前端契约断言（:336, :373, :389）需同步更新
```

## 九、风险与未决

| 风险 | 说明 | 处置 |
|---|---|---|
| 阶段一改了 `requires_form` 语义 | 现有依赖"operation 非 none 就弹窗"的测试会红 | 属预期，随实现同步更新断言 |
| 前端契约测试覆盖面窄 | `tests/test_api.py:336,373,389` 只断言 DOM 钩子存在，`openIntentForm`/`canonicalIntentFromForm` 完全无测试 | 阶段一补上芯片与判定分支的契约测试 |
| 交易会话持久化 | 新表需要迁移；`migrations.py` 已有增量迁移机制 | 实现计划中给出具体迁移步骤 |
| `_NEGATIVE_MARKERS` 误杀 | 规则兜底可能把"你能帮我买吗？"（请求）误判为疑问 | 一票否决仅作用于纯疑问句式；以测试用例固定边界 |
| 商贩词表合并改变行为 | 六处调用点中有两处变宽、一处变窄 | 已在 5.4 逐点列明并判定为修复；实现时同步更新受影响的测试断言 |
