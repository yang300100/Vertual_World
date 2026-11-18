# Virtual_World 项目交接

更新时间为 2026 年 11 月 17 日。新对话先读取本文，再核对实际代码、存档和进程状态。
用户使用中文，称呼用户为“哥哥”。代码注释使用中文，任务正确性优先。

## 本轮收尾与当前状态

本轮任务是完成局部通路功能交付、正式存档迁移、服务验收及交接。
局部通路代码已应用到 D 盘项目，共 19 个文件，包含 11 个替换和 8 个新增。
API 与 worker 已按本次用户收尾要求启动，正常启动已执行预演过的数据库迁移。
启动阈值为 `2026-11-17T11:53:56.8907817Z`，对应北京时间 19:53。
最终只读服务验收已通过，证据为 [服务验收报告](D:/Virtual_World/run/closeout-20261117/service-verification.json)。
收尾快照的已提交世界时间为 `2040-04-11T08:30:50.452054+00:00`，正式数据库共有 292 张表。服务会继续推进，后续工作应重读最新状态。
本轮功能交付完成与长期“邂逅、关系、同行、重逢”总目标分别记录，G1 至 G8 总验收仍在进行。
新增优化留到新对话安排，先保留这次交付的代码、证据和真实世界状态。

## 实际目录

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| 实际项目 | `D:\Virtual_World` | 包含 `.git`、当前源码、测试、运行配置和正式存档 |
| 正式数据库 | `D:\Virtual_World\data\world.db` | 连同当前 WAL 构成完整已提交状态 |
| Python 环境 | `D:\Virtual_World\.venv\Scripts\python.exe` | 已验证可运行 |
| 当前聊天工作区 | `C:\Users\User\Desktop\Virtual_World` | 保存隔离副本、项目梳理及本交接文档 |
| 隔离工作副本 | `C:\Users\User\Desktop\Virtual_World\local_route_work` | 保存本轮测试、预演、部署备份和证据 |
| 历史科学副本 | `C:\Users\User\.codex\worktrees\science-protocol-repair\Virtual_World` | 较旧，不作为当前主项目 |

主项目曾从 C 盘迁至 D 盘。开工时核对实际路径和可写权限，避免修改旧副本。
D 盘有大量既有 dirty/untracked 内容，先识别来源，再决定如何处理。

## 核心架构与阅读入口

SQLite 保存客观事实。模型提出结构化候选，领域规则在事务内复核并执行。
人物知识、感受与记忆按本人来源保存，不能覆盖世界事实或代替另一方同意。
API 提供接口，worker 独立推进时间。心跳使用世界时间比例，并按事件边界结算。
实际规则入口分布在多个领域服务，不能把旧文档的“所有动作必经 ActionService”当作严格调用事实。

| 子系统 | 优先阅读 |
| --- | --- |
| 运行与存档 | `api.py`、`engine.py`、`clock.py`、`worker.py`、`database.py`、`repository.py` |
| NPC 与认知 | `npc_agency.py`、`daily_life.py`、`society.py`、`epistemics.py`、`conversation/context.py`、`outreach.py` |
| 生活与经济 | `life.py`、`interiors.py`、`inventory.py`、`economy.py`、`food.py`、`contracts.py` |
| 关系与同行 | `intimacy.py`、`relationship_continuity.py`、`appointments.py`、`companions.py`、`journeys.py`、`group_journeys.py`、`rides.py` |
| 空间通行 | `routing.py`、`movement.py`、`navigation.py`、`player_steering.py`、`cross_layer.py`、`local_routes.py` |
| 登记与建设 | `registration/registry.py`、`registration/handlers/`、`registration/construction.py`、`elements.py` |
| 科研 | `science_programs.py`、`science_runner.py`、`collective_runner.py`、`principle_runner.py`、`materials.py`、`science_freeze.py` |
| 界面与运维 | `world_engine/web/`、`api_*`、`scripts/start_world.ps1`、`config.py`、`history.py` |

## 局部通路交付范围

- `local_route` 是严格登记类型，提交强制候审，编年者明确确认后参与规划。
- `world_local_routes` 保存实际折线、来源、已批准数据集、方向、限速、开放状态和版本。
- 管理 API 提供来源选项、列表、查看、关闭及重开，沿用现有本地编年者信任边界。
- `RoutePlanner` 比较原地形路线与单条核实通路，首尾连接继续经过地形规划。
- 移动保存并核对根绑定和逐段绑定，人物沿折线实际行走。
- 闭路按真实生效时刻与进入位置处理，尚未进入的人走到入口再停。
- 同行成员统一结算，共乘保留车辆与乘员，再由既有暂停或恢复流程处理。
- 编年者登记页提供通路表单、全部坐标审核详情和开闭管理。

首版支持已勘察的既存陆路、单条完整通路及两端连接，多通路串联仍是后续事项。
正式道路表初始为空，当前世界的实际通路由编年者逐条核实登记。
11 月 14 日的损坏路线防护也已保留，残余导航标记存在时禁止回退直线。
若历史路线的全部导航标记一起丢失，现有格式仍无法将其与旧版直线移动区分。

## 验证范围与证据

最终固定源码整合回归覆盖 33 个文件、597 项测试，全部通过且前后文件哈希一致。
部署后从 D 盘实际源码运行 15 个文件、223 项测试，全部通过。
597 和 223 存在重叠，不能相加成唯一测试数，本轮没有运行全仓全部测试。
界面证据包括实际前端脚本的 8 个 VM 模拟场景，真实浏览器验收尚待补充。

以下相对路径均位于 `C:\Users\User\Desktop\Virtual_World\local_route_work`。

| 证据 | 相对路径 |
| --- | --- |
| 597 项最终回归 | `run/local-route-regression-20261116T105228Z-78000fb3.json` |
| 223 项部署后核验 | `run/post-local-route-deployment-83ddde28f64d476c9715c4e5edacb3af.json` |
| 部署与原件备份 | `run/local-route-deploy-9b245fdc42034ab580395dba2ef40f45/report.json` |
| 独立迁移预演 | `run/local-route-migration-20261116T104425Z-d897efd6.json` |
| 迁移审阅 | `run/local-route-migration-review-ebf439fa92e24e9ba7b241b61e55bd2b.json` |
| 本轮正式服务验收 | `run/local-route-closeout-20261117/service-verification.json` |
| 收尾快照 | `run/local-route-closeout-20261117/closeout-status.json` |
| 前端模拟 | `tests/fixtures/local_routes_ui.cjs`、`tests/test_local_routes_ui.py` |

## 存档与运行要点

本轮迁移前一致备份保存在 `C:\Users\User\Desktop\Virtual_World\local_route_work\run\local-route-before-20261116T104425Z-d897efd6.db`，SHA-256 为 `824156f01bae353ac27f55b8caa7a93bb8af40683b7b91076d4116915682f082`。
关键验收报告和 11 份更新前源码的便携副本位于 `D:\Virtual_World\run\closeout-20261117`。

备份使用 SQLite online backup，读取 URI 为 `mode=ro` 并开启 `PRAGMA query_only=ON`。
现存 WAL 含有已提交内容，单独复制 `world.db` 不能代替一致性备份。
迁移先在独立副本执行，检查完整性、外键及关键旧数据，再根据当前授权处理正式库。
本轮预演保留全部旧登记行、列、外键、索引和触发器，新增道路表初始为空。
84 张科研相关表和玩家全字段保持一致。
已审阅的既有初始化差异为 3 位 NPC 激活原因规范化及 3144 条目录更新时间。
此前一次正式启动曾被自动审批拒绝，该历史记录保留，当前状态以本轮最终服务报告为准。
后续操作依据当前用户指令、已有授权和实际权限判断，不把历史拒绝扩展成永久门禁。

```powershell
Set-Location -LiteralPath D:\Virtual_World
.\scripts\start_world.ps1 -Port 8765 -SkipInstall -NoBrowser -NoLogViewer
```

先检查现有 API、worker 和监听，复用健康进程，避免重复启动。
实际配置解析到 D 项目，API 端口 8765，worker 周期 60 秒。
验收核对健康、实际脚本、新表结构及真实提交的心跳，模拟时间不能直接作为墙钟耗时。
`scripts/stop_world.ps1` 使用强制结束，正常停止需先核对进程及可用的优雅关闭方式。

## 后续优化与长期目标

1. 统一移动入口。`execute_approach_action` 仍直接结算近距坐标，方向输入尚未使用局部通路。
2. 明确赠物的接受规则。当前入口先转移所有权，再生成 NPC 反应，事前接受、拒收与归还流程待产品讨论。
3. 实际体验主线需要持续验证，涵盖真实相识、对话、双方选择、同行、分离和重逢。
4. 真实生计与微观空间仍有断点，339 位受阻 NPC 的道路和食物获取需要实际可用事实支持。
5. 补多通路串联、有限燃料行驶及补给恢复、无法回应者救援、多人及复杂跨层。
6. 补自然首次相识、主动重逢、多人物差异与拒绝样本，以及至少两个世界年的连续验收。
7. 整理 Git 变更与阶段文档，按来源保留用户内容，统一当前状态与历史交付记录。

总目标见 `D:\Virtual_World\docs\design\44-encounter-companionship-goal.md` 的 G1 至 G8。
README 和阶段文档含有历史状态，新对话应结合当前代码、证据和存档核对。
当前阶段完成后，继续以可实际体验的相处链为主线，保留每个人真实的选择与后果。
