# NPC 人格、记忆与对话管线（设计）

日期：2026-09-18
状态：诊断与实验已完成，待实施

## 一、问题

与 NPC 对话时观察到四个症状：

1. **腔调都一个样**——铁匠、司簿、商贩说话分不出谁是谁。
2. **记不住该记的事**——提过的、答应过的，下一轮就忘。
3. **反应太平淡**——只回答问题，不主动、不拒绝、不追问。
4. **前后对不上**——同一场对话里态度和语气会漂。
5. **过度专注本职工作**——与绘图员对话时，她每轮都要汇报画图进度。

第 5 条是触发本次调查的直接观察，也是最有诊断价值的一条。

## 二、诊断

五个症状归到四个根因，全部可追溯到具体代码。

### 2.1 根因 A：角色卡贫瘠（对应症状 1、3、5）

`ConversationService.default_card`（`conversation/cards.py:17-57`）按 `traits` 走分支，
**全世界的说话风格只有 4 种**：

```python
if   traits & {"热情", "健谈"}:       speech_style = "语气亲切，句子稍长，会自然补充…"
elif traits & {"警觉", "谨慎", …}:    speech_style = "措辞克制，先确认来意…"
elif traits & {"直率", "务实", …}:    speech_style = "表达直接，偏好具体的人、物、时间…"
else:                                speech_style = "语气自然含蓄，会随关系和场合调整回答长短"
```

且三个字段对**所有** NPC 完全相同：

| 字段 | 现状 |
|---|---|
| `preferred_address` | 永远是同一句"根据关系与场合自然称呼对方，不固定使用尊称" |
| `private_tension` | 填空题：`f"在{trait_text}与眼前责任之间寻找不失分寸的做法"` |
| `dialogue_examples` | **永远是空 tuple** |

`traits` 来自 `npc_generation.py` 的硬编码角色表，因此相同角色模板的 NPC 得到**逐字相同**的卡。

### 2.2 根因 B：检索被 goals 污染 + 词面匹配（对应症状 2、5）

- 记忆召回的查询串把 NPC 的目标拼了进去（`conversation/context.py:64`）：

  ```python
  query=" ".join((player_text, interaction, " ".join(npc.goals))),
  ```

  知识检索同理（`conversation/context.py:326-329`）。**无论玩家说什么，召回的内容天然偏向该 NPC 的职业。**

- 打分是字面重叠：`score = overlap * 8 + importance * 1.5 + recency`（`context.py:309`），
  `overlap` 来自 CJK 2-gram/3-gram 交集（`_search_tokens`）。
  **语义相关但用词不同的记忆召不回来**——"你上次答应我的事"接不上"承诺三天后交付绞盘"。

### 2.3 根因 C：提示词通篇是"别演"（对应症状 3）

`roleplay.py:11-52` 的否定式约束有十几处：不代写玩家、不补造食物工具天气、不套用客服开场、
不反复盘问、不强加话题钩子、不介绍人设、不必每轮追问……

每一条单独看都对（防幻觉、防出戏）。但合起来，模型收到的总信号是**"生动 = 风险"**。
叠加 500 字正文上限与"简短寒暄可以只回一句"，产出即"说得都对，但平"。

### 2.4 根因 D：每轮无状态重建（对应症状 4）

`DialogueContextAssembler` 是无状态门面，`build_npc_reply_context` 每次调用都重新查库，
进程内没有任何跨请求的记忆体。好处是世界状态永远最新，代价是**同一场对话内没有连续状态**，
人物在轮次之间会漂移。

> 注：原始设想"每次对话启动全新上下文的 agent"对准的正是 D，但**"全新的上下文"会加重 D 而非减轻**。
> D 的解法是让同一场对话**有**连续状态，不是更彻底的隔离。

### 2.5 根因 E：输出契约掐住了台词（对应症状 3，且影响最大）

`agent_llm.py:96-98` 在 roleplay 路径强制启用 JSON 模式：

```python
if roleplay:
    payload["messages"] = build_npc_reply_messages(system_prompt, user_payload)
    payload["response_format"] = {"type": "json_object"}
    payload["max_tokens"] = min(self.max_tokens, 1200)
```

对当前推理模型（响应含 `reasoning_content`），该组合会**概率性**地让模型把正文输出成空白。
实测数据见第五节。而 `agent_llm.py:108` 把它判为失败并抛错：

```python
if not isinstance(content, str) or not content.strip():
    raise AgentLLMError(f"Agent[{label}]返回了空的决策内容")
```

结果是 NPC 无故失语，玩家看到 `npc_reply_error`。

### 2.6 附带发现

| 发现 | 位置 | 影响 |
|---|---|---|
| `npc_social_move` 只写不读 | `engine_player_intent.py:159`、`engine_group_dialogue.py:133` 写入；全项目零处读取，前端未使用 | 该字段可移除，移除无功能损失 |
| `get_card` 永不补全残缺卡 | `conversation/cards.py:71-81` 读到即返回 | `default_card` 再改进也更新不到已有卡；库中 3 张卡有 4 个字段缺失 |
| 商贩身份词表三处不一致 | `economy.py:82`、`intent_effects.py:74/128` | 详见意图线设计文档 5.4 节 |

## 三、SillyTavern 对照

对照本地 `C:\Users\User\Desktop\SillyTavern-release`（1.18.0）。其角色扮演效果好，
**核心不在于某条特殊咒语，而在于"让模型自由说话、客户端负责收拾"**。

### 3.1 它从不要求模型输出 JSON

请求体构造处（`public/scripts/openai.js:2742-2767`）**没有 `response_format` 字段**。
该能力只在显式传 `jsonSchema` 时按需注入，且用的是 `json_schema`（带完整 schema），
**全项目没有任何一处发送 `type: 'json_object'`**（`src/endpoints/backends/chat-completions.js:2542-2551`）。

提取台词就一行（`public/script.js:6217` `extractMessageFromData`）：取 `content` 即台词，
**普通聊天回复从不做 JSON 解析**。

### 3.2 收拾残局在客户端

`cleanUpMessage()`（`public/script.js:6383`）串行执行十几步：

```
停止串截断 → 用户正则替换 → 折叠换行 → 去行尾空白
→ trimWrongNames（开头是错误名字则整条丢弃；中途出现 "用户:" 则截断）
→ <|endoftext|> 截断 → 群聊清理 → 剥离 "角色名:" 前缀
→ fixMarkdown → trim_sentences → trim()
```

**它接受"这条废了"，而不是要求模型永不犯错。**

### 3.3 content 为空不算失败

`public/script.js:5354`：

```js
const shouldDeleteMessage = type !== 'swipe' && ['', '...'].includes(lastMessage?.mes)
    && !lastMessage?.extra?.reasoning && ...   // 有 reasoning 就算有效消息
```

推理内容存进 `message.extra.reasoning`，另有 1700 行专责模块 `reasoning.js`。

### 3.4 示例对话是问答对

`mes_example` 以 `<START>` 分块，块内首行是格式说明（解析时跳过），
其余为 `{{char}}:` / `{{user}}:` 交替的**问答对**，注入时每块前加 `[Example Chat]` 标记
（`openai.js:720` `parseExampleIntoIndividual`、`:1092` `populateDialogueExamples`）。

它教的是**什么话该对什么话回应**；我们的 `dialogue_examples` 是孤立单句。

### 3.5 角色卡字段对照

| SillyTavern | 我们 | 作用 |
|---|---|---|
| `description` | `public_role` 等 | 基本资料 |
| `personality` | `traits`（仅标签） | 性格 |
| `scenario` | 无（用世界状态） | 情境 |
| `mes_example` | `dialogue_examples`（默认空） | 语料 |
| `post_history_instructions` | 无 | 历史之后的角色专属指令 |
| `extensions.depth_prompt` | **无** | 插在历史中间的指令 |

两个我们完全没有的机制，共同点是**把提醒放在离生成点最近的位置**。

## 四、实验证据

全部实验脚本只读存档、不改任何数据，见 `scripts/roleplay_card_ab.py` 与
`scripts/roleplay_output_check.py`。

### 4.1 角色卡 A/B（伊蕾娜·星绘，3 轮对话）

改动只有 `npc_card` 一段，其余上下文、世界快照、问题全部相同。

| | 卡 A（旧卡，5 字段） | 卡 B（新卡，9 字段） |
|---|---|---|
| 提到工作的轮数 | 3 / 3 | 2 / 3 |
| 累计工作词 | 8 | 4 |
| 平均回复长度 | 113 字 | 58 字 |

关键证据——卡 B 第三轮：

> 玩家：你平时除了干活，还喜欢做什么？
> 伊蕾娜：算不上喜欢什么。晚上天黑下来，会抬头看一会儿，顺便对一对白天的方位，写两行。风大就不看。
> 别的……**饼价从三个铜子涨到四个了，这个我记着。**

"饼价"一条来自新卡的 `dialogue_examples` 示例：

```yaml
- 前面那家摊子的饼，昨天三个铜子。今天四个了。
```

**模型把示例内化成了她的语言习惯**——这条示例本身是非工作内容，却承载了"细致、记数字"的性格。

**结论**：角色卡内容显著影响表现；`dialogue_examples` 是单点最强的杠杆。

### 4.2 输出契约对比（同一 context，只换输出方式）

| 问题 | json 模式 | free 模式 |
|---|---|---|
| 你好，今天过得怎么样？ | 28 字 | 50 字 |
| 附近有什么值得去的地方吗？ | 68 字 | 89 字 |
| 你平时除了干活，还喜欢做什么？ | 27 字 | 70 字 |
| 你觉得镇上的粮食够吃吗？ | 92 字 | 119 字 |
| **成功率** | 4/4 | 4/4 |
| **平均字数** | 54 字 | **82 字（+52%）** |

**JSON 模式是概率性失语，不是必然失败。** 同一句"附近有什么值得去的地方吗"，
在改动卡之前实测 **3/3 全部返回空白**（`finish_reason` 仍为 `stop`，
`completion_tokens` 远低于上限——**不是被 max_tokens 截断**，
把上限提到 4000 也照样空白）。换用另一模型后 1/5 成功。

对照组（各 3 次）：

```
完整prompt + json模式      空白 3/3
简易prompt + json模式      空白 0/3
完整prompt + 无json模式     空白 0/3
简易prompt + 无json模式     空白 0/3
```

**触发条件是「完整 system prompt」×「json_object 模式」的组合。**

free 模式下模型仍常自行输出 JSON（4 次中 3 次），宽容解析全部正确接住。

### 4.3 实验边界（诚实声明）

- 样本量小（A/B 为 3 轮、输出对比为 4 问），结论方向明确但不是统计结论。
- 两次 A/B 快照之间世界状态有推进（`recalled_past_conversation` 等段变化），
  但 `npc_card` 一段的对照是干净的。
- 输出契约对比中 json 模式 4/4 成功，说明其失语是概率性的；支持改动的依据是
  **free 不劣于 json 且更丰富**，而非"json 必挂"。

## 五、设计

### 5.1 输出契约：从"填表"改为"自由说话 + 宽容解析"

**改动点**（`agent_llm.py` roleplay 分支）：

```python
if roleplay:
    payload["messages"] = build_npc_reply_messages(system_prompt, user_payload)
    # 不再发送 response_format=json_object：推理模型在该模式下会概率性输出空白正文
    payload["max_tokens"] = min(self.max_tokens, 1200)
```

**宽容解析**（替代原来的 `schema.validate_python` 强校验）：

1. 去除 ``` 围栏；
2. 若正文中含 `{...}` 且能解析出 `{"reply": "..."}`，取其值；
3. 否则**整段正文即为台词**。

**空正文不再直接判失败**：至少区分"确实失败"与"模型只给了推理"。

### 5.2 `social_move` 的处理

该字段全项目零处读取（见 2.6），移除或改为固定默认值 `"answer"` 均可。
保留字段但不再要求模型输出，避免为一个无人消费的枚举牺牲台词质量。

### 5.3 角色卡：从"模板算"改为"有人味"

- `dialogue_examples` 改为**问答对**格式（借鉴 SillyTavern 的 `mes_example`），
  每对含玩家的问与 NPC 的答；
- `default_card` 的分支模板扩展为**按角色语义**生成，而不是按 traits 集合的 4 选 1；
- `preferred_address`、`private_tension` 不再对所有 NPC 使用同一句表达式。

### 5.4 `get_card` 补全残缺卡

读到卡时检测字段完整性，缺失则与 `default_card` 合并补全后回写。
当前库中 3 张卡各有 4 个字段缺失，且永不更新。

### 5.5 检索不再注入 goals

`conversation/context.py:64` 与 `:326-329` 的查询串移除 `" ".join(npc.goals)`，
让召回由**玩家的话**主导。

## 六、分期实施

| 阶段 | 内容 | 理由 |
|---|---|---|
| **一** | 5.1 输出契约 + 5.2 `social_move` | 改动最小、收益最大，直接解决 NPC 失语与台词平淡 |
| **二** | 5.4 `get_card` 补全 + 5.5 检索去 goals | 都是小改动，一个修复陈旧数据，一个解开话题绑架 |
| **三** | 5.3 角色卡丰富化 | 需要设计卡的生产方式（手写 / 模型生成 / 分层），工作量最大 |

## 七、测试策略

现有测试中，`tests/test_roleplay.py` 断言了 messages 分段与 JSON 输出契约
（`:56-79`、`:97-123`），`tests/test_deepseek.py:118,151` 用 JSON 桩数据。
阶段一会改变这些契约，**相关断言需同步更新**。

新增用例：

```
宽容解析
  纯 JSON 正文            → 取出 reply
  ```json 围栏包裹的 JSON  → 取出 reply
  纯台词文本              → 整段作为 reply
  JSON 缺 reply 字段       → 整段作为 reply（降级而非报错）
  空正文                  → 抛出可识别的失败，不误判为成功

输出契约
  roleplay 请求体不含 response_format

角色卡
  get_card 读到字段残缺的卡 → 与 default_card 合并补全

检索
  召回结果不再被 npc.goals 主导
```

## 八、未决

| 事项 | 说明 |
|---|---|
| 角色卡的批量产出方式 | 库中有 31 个 NPC。手写不可行；用模型生成需要设计生成时机（建世界时？）与校验。待阶段三讨论。 |
| `depth_prompt` 机制 | SillyTavern 用它把角色专属提醒插在历史中间。我们是否引入，取决于阶段一之后"别演"约束是否需要重构为分角色约束。 |
| 提示词从"别演"转"这样演" | 根因 C 的修复方向，但与事实边界约束有张力，需要谨慎设计；建议在阶段一验证输出契约效果之后再动。 |
| 每轮无状态重建（根因 D） | 本轮不处理。解法可能是给同一场对话加"本场态度摘要"，而不是引入长驻会话。 |
