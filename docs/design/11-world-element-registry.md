# 世界元素注册器

状态：第四阶段已实现（候选确认、种族生命周期、土地权、建设资源托管与取消退款）。

## 目标

把玩家、NPC和系统事件造成的长期影响登记为可追溯世界事实，同时防止模型或前端
绕过时间、资源、地点、血缘和人物认知边界直接写数据库。

统一入口为 `WorldElementRegistry`，当前支持：

- `character_birth`：后代/收养/监护登记；`planned` 不创建人物，`born` 才落库。
- `settlement`：玩家/NPC只能从 `planned` 建设项目开始，不能瞬间创建成熟城市。
- `building`：独立保存建筑类型、位置、所有者和施工状态。
- `structure`：区分新建与发现，人物给出的来源解释只标记为 `claimed`。
- `lore`：区分人物观点、地方说法、公开知识和作者正典。

## 不可变边界

1. 每个注册必须引用当前世界中已经结算的 `source_event_id`。
2. 人物申请者必须是来源事件的参与者。
3. payload 使用 Pydantic 判别联合，拒绝任意字段和任意数据库更新。
4. 幂等键在世界内唯一；相同请求只应用一次，不同内容复用键时返回冲突。
5. 处理器副作用在 SQLite SAVEPOINT 中执行；失败时全部回滚，但失败审计保留。
6. 成功注册增加一次 `world.version`，并写入新的客观事件。
7. 人物提交的世界观不能直接成为 `author_canon`。
8. Agent 以后只能生成 `ElementRegistrationSubmit` 候选，不能持有写连接。

## 存储

统一审计与实体索引：

```text
element_registration_requests
element_registration_effects
world_entities
```

第一阶段专用事实表：

```text
character_lineages
construction_projects
buildings
world_structures
knowledge_entries
```

`world_entities` 只提供跨类型身份和来源索引；人物、地点、建筑、巨构和知识的专有字段
继续保存在各自表中，不使用一张任意 JSON/EAV 表替代领域模型。

## API

```text
POST /api/worlds/{world_id}/registrations
GET  /api/worlds/{world_id}/registrations
GET  /api/worlds/{world_id}/registrations/{registration_id}
POST /api/worlds/{world_id}/registrations/{registration_id}/confirm
POST /api/worlds/{world_id}/registrations/{registration_id}/reject
GET  /api/worlds/{world_id}/construction-projects
PATCH /api/worlds/{world_id}/registrations/{registration_id}/construction
```

提交结果可以是：

- `approved`：规则允许，但仍处于计划/等待阶段；
- `applied`：已经写入对应事实表；
- `rejected`：违反世界规则，未产生任何事实副作用；
- `failed`：处理器发生异常，SAVEPOINT 已回滚。

## 第二阶段运行链路

- 成功结算的玩家行动会由 `RegistrationIntentDetector` 检查明确表述；含糊内容不注册。
- 当前可自动识别建城/建村、建筑施工、遗迹发现、家庭计划和人物观点/地方传闻，并保存为 `proposed` 候选。
- 玩家确认后才应用候选；拒绝会保留原因但不写世界事实。
- `constructing` 项目按世界时间推进；建筑、巨构和聚落到期后分别完成对应事实。
- 聚落规划会预留地块，开工后转为有效土地权；重叠地块会被拒绝。
- 开工原子扣除地点资源和申请者资金，取消按未完成比例退款并写补偿事件。
- 种族档案控制孕育时长、成年年龄和兼容性；旧人物未记录出生时间时按已成年兼容处理。
- 动态人物观点只向作者本人可见；地方说法按事件地点可见；公开知识可被普通人物检索。

## 尚未实现

- 多代谱系、跨种族混合档案和种族专有出生后特质；
- 完整经济系统中的人口、劳动力排期、土地测绘与更细粒度的项目材料消耗；
- 多人作者审核、已应用事实的争议撤销与更复杂补偿；
- 玩家端更细致的命名修订和冲突解决流程。
