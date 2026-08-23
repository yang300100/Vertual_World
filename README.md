# Virtual World Core

这是一个可持久化、自主推进、与模型供应商无关的虚拟世界内核。当前版本先建立世界事实、人物、地点、行动、事件、主观记忆和独立推进 worker；表现层和第三方模型适配器将在内核稳定后选择。

## 设计文档

完整目标设计从 `docs/design/README.md` 开始阅读。该索引会明确区分“已经实现”“已确认待实现”和“仍待决定”，目前包含：

- 现实一分钟状态心跳、可调世界时间比例和模型裁判周期。
- 世界、地区、地方、人际四级事件范围及独立影响强度。
- 世界事件、人物状态更新和主观记忆分离。
- 人物身份、长期特质和当前状态分离。
- 客观世界图与主视角知识图。
- 物品实例、所有权、小背包与库存日志。
- 分阶段数据库迁移和2核2GB服务器约束。

`docs/ARCHITECTURE.md` 只描述当前代码实际运行边界；目标设计不能视为已经实现。

## 已实现的边界

- SQLite WAL 保存唯一客观世界状态。
- 世界时间按轮次推进，API 与 worker 共用同一套推进逻辑。
- 人物只能提交结构化行动，规则裁判负责最终执行。
- 每个成功行动都会产生客观事件与人物主观记忆。
- 状态修订号与写事务防止同一轮被并发重复结算。
- 决策器通过 `DecisionProvider` 协议替换；模型不可直接写数据库。
- Web API 不包含定时器，自动推进由独立 worker 负责。
- 可直接使用DeepSeek模型批量生成结构化人物行动，失败时自动降级为规则决策。
- 独立worker每现实60秒执行一次轻量状态心跳，默认现实时间与世界时间1:1。
- 世界时间比例可运行时调整，0表示暂停；服务器重启后不补算离线时间。
- 调速会先按旧比例结算到生效时刻，再在同一事务中修改比例，调速事务本身只增加一次状态修订号；重复设置相同比例不写事件也不增加修订号。若结算恰好跨过模型裁判点，随后发生的裁判是独立事务和独立修订。
- 自主模型裁判固定在世界时间00:00和12:00，裁判本身不推进时间。
- 人物状态心跳、世界事件和模型裁判过程分别记录。
- 人物采用0至100的正向饱食度：100最舒适，随世界时间下降，进食后上升。

## DeepSeek模型配置

复制 `.env.example` 为Git忽略的 `.env`，只在 `.env` 中填写真实密钥：

```dotenv
WORLD_DECISION_PROVIDER=deepseek
DEEPSEEK_API_KEY=请填写轮换后的真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash-vision-exp
```

模型只收到本轮世界时间、地点、活跃人物状态和同地点人物ID。它不能读取数据库文件、执行SQL或直接写入世界状态。API密钥不得进入代码、前端、日志或Git。

## 本地启动

### Windows一键运行

直接双击项目根目录中的：

```text
启动虚拟世界.cmd
```

脚本会自动：

1. 检查并按需创建 `.venv`。
2. 检查并按需安装项目依赖。
3. 后台隐藏启动FastAPI。
4. 等待 `/api/health` 返回本项目标识。
5. 后台隐藏启动独立世界worker。
6. 自动打开 `http://127.0.0.1:8000/`。

重复双击不会重复启动API或worker。运行日志写入：

```text
logs/runtime/
```

停止时双击：

```text
停止虚拟世界.cmd
```

停止脚本只终止本项目 `.venv` 中、命令行明确匹配世界API或worker的进程，不会广泛终止其他Python或浏览器进程。

PowerShell高级用法：

```powershell
# 使用其他端口且不自动打开浏览器
.\scripts\start_world.ps1 -Port 8766 -NoBrowser

# 停止本项目进程
.\scripts\stop_world.ps1
```

### 手动运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m uvicorn world_engine.api:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/` 会进入世界控制台；`http://127.0.0.1:8000/docs` 可以直接操作 API。

## 世界控制台

FastAPI直接托管 `world_engine/web/` 中的HTML、CSS和原生JavaScript，不需要Node服务。控制台支持：

- 查看世界时钟、速度、下一裁判点、状态修订号和离线策略。
- 在worker在线时依据服务器时间平滑显示当前世界时间，两次分钟心跳之间也会连续走动。
- 显示worker心跳是否正常；worker离线或延迟时停止外推，避免显示虚假时间。
- 查看人物地点、精力、饱食度、金钱、特质和目标。
- 暂停、0.5倍、1倍、2倍、10倍、60倍及自定义速度。
- 手动状态心跳、立即模型裁判和主视角人物介入。
- 查看最近客观事件、裁判记录并同步历史日志。
- 每5秒只读刷新数据，页面刷新本身不会推进世界。

当前控制台面向本地开发环境，尚未实现登录和权限控制。部署到公网前必须增加认证，并限制调速、心跳和裁判接口的管理员权限。

另开一个终端运行独立世界 worker：

```powershell
.\.venv\Scripts\python.exe -m world_engine.worker
```

只推进一次并退出：

```powershell
.\.venv\Scripts\python.exe -m world_engine.worker --once
```

## 查看世界运行历史

每次成功推进都会把SQLite客观事件自动同步到：

```text
logs/worlds/<world_id>/history.md
logs/worlds/<world_id>/history.jsonl
logs/worlds/<world_id>/ticks/<sequence>_<tick_id>.json
logs/worlds/<world_id>/heartbeats.jsonl
logs/worlds/<world_id>/state_updates.jsonl
logs/worlds/<world_id>/state_manifest.json
logs/worlds/<world_id>/characters/<character_id>.state.jsonl
```

- `history.md`：适合直接阅读，按轮次显示决策器、行动、失败原因和行动理由。
- `history.jsonl`：每行一个完整客观事件，保留人物与轮次ID。
- `ticks/`：每个轮次一个结构化快照，方便精确复盘。
- `heartbeats.jsonl`：每分钟世界时钟和状态更新摘要。
- `state_updates.jsonl`：人物连续状态差值，不污染世界编年史。
- `state_manifest.json`：独立保存心跳和人物状态日志的最新计数。
- `characters/`：按人物拆分的状态变化历史。

旧数据库也可以随时回填日志：

```powershell
.\.venv\Scripts\python.exe -m world_engine.cli history <world_id>
```

日志目录已被Git忽略。数据库是权威事实源，日志损坏或删除后仍可重新生成。

## 命令行快速体验

```powershell
.\.venv\Scripts\python.exe -m world_engine.cli create --name "初始小镇"
.\.venv\Scripts\python.exe -m world_engine.cli list
.\.venv\Scripts\python.exe -m world_engine.cli tick <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli heartbeat <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli speed <world_id> 2
.\.venv\Scripts\python.exe -m world_engine.cli adjudicate <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli adjudications <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli show <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli history <world_id>
```

## 核心因果链

```text
现实一分钟心跳
  -> 按当前比例推进世界时间
  -> 确定性更新人物连续状态
  -> 检查00:00/12:00裁判边界
  -> 到期时选择活跃人物
  -> 决策器提出结构化行动
  -> 状态修订号与规则校验
  -> 事务内执行行动
  -> 写入客观事件与主观记忆
```

## 后续阶段

1. 增加环境、经济、关系传播与长期目标系统。
2. 增加模型成本预算、调用统计、批量人物决策优化与记忆压缩。
3. 轮换已经在聊天中暴露过的测试密钥，并仅在服务器环境变量中配置新密钥。
4. 根据体验目标选择文字、网页、视觉小说或游戏表现层。
5. 本地稳定后再为 2 核 2GB Linux 服务器生成 systemd 与 Caddy 配置。
