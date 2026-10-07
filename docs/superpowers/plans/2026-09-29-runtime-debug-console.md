# 运行时调试控制台实施计划

> **执行说明：** 按任务逐项实现；本计划不会修改或覆盖工作区中与本功能无关的未提交内容。

**目标：** 启动虚拟世界后保留本地日志窗口，观察玩家行动、实际发送给模型的提示词与返回、NPC 自主行动、裁定结果、错误和回退。

**架构：** API 和 worker 继续作为隐藏后台进程，将标准输出与错误写入现有日志文件。新增前台 PowerShell 查看器跟随四个日志文件并标注来源；Python 在模型请求和权威行动结算边界记录诊断信息。完整模型交互由启动器显式开启，普通运行日志保持简洁。

**技术栈：** Windows PowerShell 5.1、Python logging、现有 FastAPI 与 httpx。

**设计规格：** `docs/superpowers/specs/2026-09-29-runtime-debug-console-design.md`

## 全局约束

- API 和 worker 保持隐藏后台运行；关闭日志查看器不能停止它们。
- 保留当前运行日志文件和托盘停止行为。
- 完整提示词和模型响应只写入本机诊断输出；不得记录 API key、Authorization 值、环境变量或完整 HTTP 请求头。
- 调试功能不得改变世界事实、行动校验、模型提示词或 NPC 决策行为。
- 不修改无关的脏工作区文件，不把它们纳入本功能提交。
- 遵守仓库上级 `AGENTS.md`：面向用户和新增注释使用中文。

## 重点边界

- 文件被截断、替换或轮转时，查看器应从有效位置继续读取，不能重复打印整份旧日志。
- API 与 worker 同时输出、中文行跨缓冲区时，来源标签和 UTF-8 文本不能错乱。
- 启动器复用已运行服务时，必须准确说明该进程是否开启完整模型调试输出。
- 模型重试、无效 JSON、HTTP 错误、额度错误和规则回退都要可区分；有原始响应时才记录响应正文。
- 日志正文可能包含凭据样式文本，写入文件或控制台前都要遮蔽。

---

## 文件职责

- `scripts/start_world.ps1`：服务就绪、托盘启动后进入前台日志查看器。
- `scripts/watch_runtime_logs.ps1`（新增）：跟随 API/worker 的标准输出和错误日志，处理 UTF-8、部分行、截断和替换，并添加来源标识。
- `world_engine/config.py`：读取显式模型 I/O 调试设置，默认关闭。`world_engine/diagnostic_logging.py`（新增）统一遮蔽凭据并配置运行日志输出。
- `world_engine/agent_llm.py`：记录最终发送的模型消息、原始响应、解析结果、重试及耗时；敏感内容遮蔽。`world_engine/decisions.py` 记录常规玩家行动与 NPC 对话所走的 DeepSeek 直连请求。
- `world_engine/engine_player_intent.py`、`world_engine/player_action_flow.py`：记录玩家输入、行动提案、提供者/回退及权威结算结果。
- `world_engine/npc_performance.py`、`world_engine/daily_life.py`、`world_engine/engine_adjudication.py`：记录 NPC 对话结算、自主生活行动和裁判结果。
- `world_engine/api.py`：健康状态提供模型调试日志是否开启的布尔信息，便于启动器判断复用的 API 能力。
- `README.md`：说明窗口生命周期、隐私提示、日志位置和非调试服务的重启方式。`scripts/stop_world.ps1` 停止时清理 worker 调试状态 sidecar。

## 任务一：实现前台多文件日志查看器

**文件：** 新增 `scripts/watch_runtime_logs.ps1`，修改 `scripts/start_world.ps1`。

- [ ] 实现 PowerShell 5.1 兼容的文件查看器，为 API/worker 的 stdout/stderr 各保留读取偏移和 UTF-8 部分行缓冲；监视文件变化并定期检查文件长度作为补充。
- [ ] 文件截断或替换时重置对应偏移；每条完整日志行加本地时间与 `[API]`、`[Worker]` 标签。
- [ ] 窗口启动时显示本地隐私提示、访问地址、日志目录和 Ctrl+C 说明。
- [ ] 启动器完成浏览器和托盘启动后以前台调用查看器；退出查看器不得调用停止脚本或终止后台服务。
- [ ] 通过 PowerShell 解析器检查脚本，并手动核对新增、并发写入、截断/替换、关闭查看器后服务仍存活等场景。

## 任务二：增加模型提示词与响应调试记录

**文件：** 修改 `world_engine/config.py`、`world_engine/agent_llm.py`。

- [ ] 增加 `Settings.debug_model_io: bool = False`，由 `WORLD_DEBUG_MODEL_IO` 配置。
- [ ] 在模型请求体完全组装后记录实际发送的 `messages`，确保 NPC 对话记录的是预算裁剪后的最终提示词。
- [ ] 在获得 HTTP 响应后记录原始模型正文；通过 salvage/schema 校验后记录解析后的结构化结果。每条记录附上 label、model、attempt、阶段及耗时。
- [ ] 新增集中式正文遮蔽函数，对 `api_key`、`token`、`password`、`secret`、Bearer 凭据等赋值形式做替换；不序列化客户端 headers 或 Settings。
- [ ] 调试关闭时不输出提示词或响应正文；失败日志继续说明失败阶段、错误类型、尝试次数和是否降级。
- [ ] 仅做人工代码审阅和编译检查；按开发指令，本轮不新增或运行测试。

## 任务三：补齐玩家与 NPC 行动边界日志

**文件：** 修改 `world_engine/engine_player_intent.py`、`world_engine/player_action_flow.py`、`world_engine/npc_performance.py`、`world_engine/daily_life.py`、`world_engine/engine_adjudication.py`。

- [ ] 每次玩家提交生成一个关联 ID；在输入接收、provider/fallback 选择、结构化行动提案和规则结算后分别记录必要字段。
- [ ] 记录 NPC 回复生成与结算结果，复用已有事件 ID、接受状态和经过 salvage 的回复，不重复推断世界事实。
- [ ] 记录 NPC 日常意图及实际创建的移动、活动或采集事件；记录被暂缓或拒绝的原因，避免写成行动成功。
- [ ] 自主裁判日志复用既有 run/tick ID、提案与 outcome 统计，不记录整份世界快照。
- [ ] 人工检查直达行动、对话、模型降级、日常行动与拒绝路径的日志字段，并运行 Python 编译检查；不新增或运行测试。

## 任务四：启动器开启调试并说明复用状态

**文件：** 修改 `scripts/start_world.ps1`、`world_engine/api.py`、`world_engine/worker.py`、`README.md`。

- [ ] 创建 API/worker 子进程时，临时将 `WORLD_DEBUG_MODEL_IO=1` 放入子进程继承环境；用 `try/finally` 恢复启动器自身环境，兼容 Windows PowerShell 5.1。
- [ ] API 健康信息加入 `debug_model_io` 布尔值，启动器复用现有 API 时显示完整提示词日志是否已开启；未开启时明确提示需通过托盘或停止脚本重启。
- [ ] worker 启动日志报告其模型调试配置，帮助识别 API 和 worker 使用配置不一致的情况。
- [ ] README 说明：关闭查看器只关闭窗口、托盘继续控制服务、完整提示词可能含角色私密信息、日志文件路径，以及如何让已有服务进入调试模式。
- [ ] 手动从一键启动器启动，核对 API、worker、托盘、浏览器和日志窗口行为；运行 Python 与 PowerShell 语法检查，不运行测试套件。

## 任务五：最终差异审阅

- [ ] 只审阅本计划涉及的文件与本功能差异，确认没有世界行为变化或无关文件混入。
- [ ] 检查日志遮蔽覆盖模型正文和行动文本，且没有输出请求头、环境变量或密钥。
- [ ] 运行 `git diff --check` 与变更模块的 Python/PowerShell 语法检查；不新增或运行测试。
- [ ] 仅暂存和提交本功能文件，提交前逐项核对暂存差异，保留其余现有工作区改动原样。

