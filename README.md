# Virtual World Core

这是一个可持久化、自主推进、与模型供应商无关的虚拟世界内核。当前版本先建立世界事实、人物、地点、行动、事件、主观记忆和独立推进 worker；表现层和第三方模型适配器将在内核稳定后选择。

## 已实现的边界

- SQLite WAL 保存唯一客观世界状态。
- 世界时间按轮次推进，API 与 worker 共用同一套推进逻辑。
- 人物只能提交结构化行动，规则裁判负责最终执行。
- 每个成功行动都会产生客观事件与人物主观记忆。
- 世界版本号与写事务防止同一轮被并发重复结算。
- 决策器通过 `DecisionProvider` 协议替换；模型不可直接写数据库。
- Web API 不包含定时器，自动推进由独立 worker 负责。

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

## 命令行快速体验

```powershell
.\.venv\Scripts\python.exe -m world_engine.cli create --name "初始小镇"
.\.venv\Scripts\python.exe -m world_engine.cli list
.\.venv\Scripts\python.exe -m world_engine.cli tick <world_id>
.\.venv\Scripts\python.exe -m world_engine.cli show <world_id>
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
2. 选择第三方模型 API，并通过官方 SDK 实现新的决策器。
3. 增加模型降级、成本预算、批量人物决策与记忆压缩。
4. 根据体验目标选择文字、网页、视觉小说或游戏表现层。
5. 本地稳定后再为 2 核 2GB Linux 服务器生成 systemd 与 Caddy 配置。

