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
- 世界版本号与写事务防止同一轮被并发重复结算。
- 决策器通过 `DecisionProvider` 协议替换；模型不可直接写数据库。
- Web API 不包含定时器，自动推进由独立 worker 负责。
- 可直接使用DeepSeek模型批量生成结构化人物行动，失败时自动降级为规则决策。

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

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m uvicorn world_engine.api:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/docs` 可以直接操作 API。

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
```

- `history.md`：适合直接阅读，按轮次显示决策器、行动、失败原因和行动理由。
- `history.jsonl`：每行一个完整客观事件，保留人物与轮次ID。
- `ticks/`：每个轮次一个结构化快照，方便精确复盘。

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
.\.venv\Scripts\python.exe -m world_engine.cli show <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli history <world_id>
```

## 核心因果链

```text
读取世界快照
  -> 选出活跃人物
  -> 决策器提出结构化行动
  -> 世界版本与规则校验
  -> 事务内执行行动
  -> 写入客观事件
  -> 形成主观人物记忆
  -> 推进世界时间与版本
```

## 后续阶段

1. 增加环境、经济、关系传播与长期目标系统。
2. 增加模型成本预算、调用统计、批量人物决策优化与记忆压缩。
3. 轮换已经在聊天中暴露过的测试密钥，并仅在服务器环境变量中配置新密钥。
4. 根据体验目标选择文字、网页、视觉小说或游戏表现层。
5. 本地稳定后再为 2 核 2GB Linux 服务器生成 systemd 与 Caddy 配置。
