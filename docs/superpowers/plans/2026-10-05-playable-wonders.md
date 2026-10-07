# 可玩的自主奇观实施计划

> 由当前会话执行；使用 executing-plans 与 test-driven-development，保留用户现有未提交修改，不启动子代理。

**Goal:** 在独立世界中自主建成联合浮空城，验证地图、登乘、拍摄上下文和有限认知。

**Architecture:** 复用 WorldElementRegistry 和 ConstructionProjectService；新增 WonderService 管理意向、代表决定、空间连接与完成证据。空间层贯穿地图、移动、交流、拍摄。

**Tech Stack:** Python、SQLite、FastAPI、现有 JavaScript 前端、pytest；不增加依赖。

**Spec:** docs/superpowers/specs/2026-10-05-playable-wonders-design.md

## 全局约束

- 不调用生图或其他付费模型 API；离线规则判断须明确标注。
- 不 reset、clean、整体提交或覆盖既有修改；独立测试数据库。
- 只有完整验收证据才能声称目标达成。

## 审查重点

同坐标不同层；未完成不可登城；普通移动不可越平台；未经消息不可知道；多次初始化和心跳不能重复造城。

## 任务一：自主生命周期与数据契约

- [x] tests/test_wonders.py：从一次初始事件出发，测试两位不同政体授权代表自行决定、登记和开工；未知/拒绝代表不参与。
- [x] world_engine/wonder_schema.py：幂等 schema 与默认 ground/0 迁移。
- [x] world_engine/wonders.py：WonderSpec、WonderService.offer/tick/complete/view/traverse/context。
- [x] database.py 初始化迁移；clock.py 两条时间推进路径接入 tick；construction.py 读取工期并原子完成地点。
- [x] 运行 `.venv/Scripts/python.exe -m pytest tests/test_wonders.py -q`，确认先失败后通过。

## 任务二：玩家空间与有限认知

- [x] 扩充 tests/test_wonders.py：同坐标层隔离、距离与活动限制、城内行走、越界拒绝、入住亲历和消息传播对照。
- [x] proximity.py、spatial.py、movement.py、player_steering.py 统一空间层处理。
- [x] society.py personal_context 只增加本人的奇观知识与所处环境。
- [x] game_scene.py 输出分层 NPC、奇观和连接。

## 任务三：实际 API、地图与拍摄预览

- [x] tests/test_wonders.py：使用 TestClient 验证地图、登乘与预览端点；使用抛错生成器证明预览不生图。
- [x] api_wonders.py 与 api.py 注册；photos.py 完成状态、方向与高度场景。
- [x] web/app.js、web/index.html：奇观标记、访问按钮与拍摄预览，复用现有地图和弹窗。
- [x] 验证现有 photos/game_scene/interiors/movement/news 测试，检查 JavaScript 语法。

## 任务四：独立世界实际演示与证据

- [x] scripts/demo_wonders.py：仅准备独立世界与初始事件，调用 WorldClockService.heartbeat 直到竣工；再以玩家 API 实测登城、移动、拍摄预览和离城。
- [x] 输出 run/wonders 下数据库、事件、拍摄上下文、验收 JSON 和中文报告。
- [x] 用真实前端展示独立存档，验证地图奇观标记与登乘反馈；不使用假图代替生图验证。
- [x] 重启服务/重新初始化后再次读取城与知识，核对幂等与持久化。


## 实际验收记录

2026-10-05：全部四项任务完成。最终证据位于 `run/wonders/floating-city-audited/最终验收.md` 与 `completion-audit.json`。136项回归、20项独立演示、真实前端与实际角色提示词核对通过。界面入口由 app.js 注入既有相机/地图 DOM，无需改动 index.html 结构。
