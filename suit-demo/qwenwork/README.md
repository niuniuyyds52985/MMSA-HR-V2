# MMSA-HR V1 · 千问办公接入包

2026-10-31 本机演示更新：已有用户回传的千问原生状态、会话列表、报告摘要成功证据。新增逐题证据工具及新版 Skill 还需新对话验收；下文旧版“客户端尚未验收”描述保留历史范围。日常更新与录屏流程见 [本机演示说明](../../docs/qwenwork-local-demo.md)。

此包把本项目工作台接到千问办公桌面版的本地 STDIO MCP。它复用同一套 loopback HTTP API；不会复制数据库、启动第二套评分系统或读取任意文件。

2026-10-27 当前新场次使用 `interview.v4`：候选人只检查一次设备，逐题作答后进入本题原追问或下一主问，老师读取同场报告。提供 `quick` 短体验（3 道主问、三维分层、全场最多 1 道追问）和 `full` 完整研究卷（8 道主问、每因子 1 题、全场最多 4 道追问）；每主问最多追问一次。新场次从 24 道主问和 36 道原追问中按冻结参数轮换，旧场次保留原配置。两种卷的题级评价锚点均为待老师核对的工程草案，不表示题卷已经等值验证。音视频保存后自动运行四个基础模块 `au / gaze / expression / audio`；Gaze360、EMOTIC 和原 AU-Net 只保留为显式独立研究选项。完成部分自动进入报告，日常读取不需要再排任务或发布附录。**本包和本地协议已有验证；千问办公实际客户端导入与联调尚未验收。**

V1 交付版本为 `1.0.0`，新场次运行配置为 `interview.v4`。部署结构是“千问办公 → 本机 STDIO 连接器 → 同机工作台 API”；GitHub 仓库负责源码分发，上传仓库或安装技能本身不会托管工作台服务。完整部署与验收清单见 [V1 千问部署说明](../../docs/qwenwork-v1-deployment.md)。

## 本机准备

1. 新电脑先按 [交付说明](../DELIVERY_README.md) 安装。默认 v4 基础资产命令为 `suit-demo/setup-delivery.ps1 -DownloadModels -DownloadFastAsr`；仅需独立研究时再加 `-DownloadGaze360` 或 `-DownloadEmotic`。使用保存本机凭据的同一 Windows 账户，启动 `suit-demo/start-interview.ps1 -OpenBrowser`。工作台默认端口为 8766。
2. 在项目根目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\suit-demo\qwenwork\prepare-qwenwork.ps1`。脚本自动使用当前工程的 `.venv-suit`，只准备文件，不安装软件、启动服务或修改千问设置。也可运行 `.venv-suit/Scripts/python.exe suit-demo/qwenwork/prepare-qwenwork.py`。端口变化时分别传 `-Port 端口` 或 `--port 端口`。输出 `mcp-import.local.json`、`zhenjian-research.zip` 和 `qwenwork-package-manifest.json`，均不含密钥、数据库、媒体或会话访问令牌。
3. 千问办公进入“扩展 → 连接器 → 添加 → 粘贴 JSON 配置”，粘贴 `mcp-import.local.json` 全部内容。若客户端需要手动填写，选择 STDIO，使用 JSON 中的完整 Python 命令与参数。
4. 在“扩展 → 技能 → 安装技能”上传 `zhenjian-research.zip`，或安装其中 `zhenjian-research/SKILL.md`。官方中国版目录为 `~/.qwenworkcn/skills/zhenjian-research/`，国际版为 `~/.qwenwork/skills/zhenjian-research/`；优先通过当前客户端上传，避免选错目录。
5. 启用连接器后新建一个千问办公对话，输入：“使用 zhenjian-research，检查真才实鉴工作台状态，然后列出模拟会话。”

在另一台电脑或项目路径变化后，重新运行 `prepare-qwenwork.ps1` 或 Python 准备器生成适合新路径的配置。不要复用旧电脑的绝对路径。项目 Python 路径通常为项目根目录 `.venv-suit/Scripts/python.exe`。

`mcp-import.example.json` 只是无本机信息的格式示例，不能直接导入。真正导入目标电脑生成的 `mcp-import.local.json`。源码与可复用技能 ZIP 可以分发；本机配置随路径生成，不能沿用别人的路径。技能 ZIP 只有 `zhenjian-research/SKILL.md`，哈希写入 manifest，相同内容可重复生成同一 ZIP。

## 日常使用顺序

先在本地工作台完成模拟面试，再让千问读取 `get_report(summary_only=true)` 并固定报告版本。用 `get_report_index` 确定主问范围，按题读取 `get_question_evidence(section=source)`，完成原话与要求的对照，再读取 `assessment` 核查已有判断；`routing` 用于解释当时追问依据。工具返回分页时须取完对应部分，不能把未读内容解释为不存在。千问形成自己的证据分析草稿，区分已有 V1 分数与新核查意见。读取不启动新任务，作答完成也不等于所有分析完成；原片和采集点回到本地报告查看。

内容评分在后台共享队列处理，候选人不等待完整评分，老师可先读已完成部分。快速追问的短语义核对设有等待预算，超时或返回不可校验内容时采用本地题库规则；不能承诺云响应或整场报告瞬间完成。报告中的运行中与终态部分结果须分开：例如 Gaze360 缺未来帧的窗口属于真实缺失，不是仍在排队，不应建议只等刷新就会补齐。

v4 追问先核对原话与题级目标词典，再由短文本模型检查同义表达、否定和实际证据，只能选择该父题允许的原追问；模型不可用时使用本地规则回退。应按当时封存的 `decision_source` 说明实际路径，展示目标缺口、引用和简短理由，不把回退说成模型成功参与。词频不是能力分，词典未命中不等于没有能力，稍后的完整评分不改写当时决定；不输出模型内部思维链。声音可描述同题基线下的音调／能量变化，“低沉”“激昂”等复合听感仍未通过独立标注验证。

## 工具

| 工具 | 行为 |
|---|---|
| status | 服务健康及本机鉴权状态 |
| list_sessions / get_session | 会话概要、当前输入版本 |
| get_report | 同场当前或显式历史报告；默认compact=true省略密集观测数组，评分、可读观察与证据保留 |
| get_report_index | 指定冻结版本的主问索引及评分/完整状态，支持显式分页 |
| get_question_evidence | 指定冻结版本、父题与 source/assessment/routing 部分的证据；有界分页，不重算 |
| get_analysis_status | 同场媒体、内容、默认本地分析各阶段进度、暂停状态与已复核题数；读取不排队 |
| list_research_runs / get_research_run | 研究队列及冻结结果 |
| export_research_report | 导出到固定 `suit-demo/local-data/mcp-exports/` 目录 |
| create_simulation_session | 显式创建带 SIM-MCP 前缀的模拟会话；可选 form_mode=quick/full，省略为 quick |
| queue_local_research | 显式排入独立词频、AU、视线、基础表情或声音实验；原模型选项包括 au_net、gaze360、emotic，不是默认报告的必经步骤 |

写工具带 MCP 写操作标记。创建和排队还要求明确确认参数及稳定请求 ID；连接器不会自动付费调用百炼，不支持传入 `semantic` 模块或云授权。语义重复实验仍从工作台显式授权。

`gaze` 为 OpenFace 视线，`gaze360` 为 Gaze360 8 Hz 七帧时序估计。`emotic` 仅返回愤怒、开心、悲伤、惊讶四个场景辅助感知情绪标签的原始线性分值与阈值，不返回其余类别或 VAD，也不将分值称为概率。后两者在新 v4 场次中不默认运行，可用于显式独立实验；旧 v3 场次保留其已冻结计划。须先安装各自资产，不能拿其他模块结果代替缺失或失败。多模态不进入内容评分、潜力分或自动追问。

## 凭据与数据

工作台 operator 凭据在本机当前 Windows 账户下经 DPAPI 读取，内部取得本场 HR 权限后访问 API。凭据不会出现在导入 JSON、标准输出、工具结果或导出中。STDOUT 只写 MCP JSON-RPC 消息。

通过连接器读取的回答/报告文本会作为工具结果进入千问办公对话，遵循该客户端及账户的数据设置。此包不上传原始音视频。导出文件仍包含研究文本，应与其他项目研究资料同样保存。

若显示鉴权不可用，确认工作台已经用同一 Windows 账户打开。若连接器未显示工具，关闭旧对话并新建对话。不要在聊天里粘贴 API Key 或访问令牌。连接器拒绝远程 API 地址、HTTP 重定向、代理、任意路径和任意 shell 命令。

## 验证状态

已测试真实子进程 STDIO 握手、工具发现、loopback 合成 API 调用、读写隔离、导出和脱敏。对已启动工作台另有两类实测：

- 默认只读检查：`status → list_sessions → get_report`，仅读取明确标为模拟的会话。
- 显式 QA 写检查：在当前 Windows 账户下创建一个 `SIM-MCP-QA-STDIO-…` 空模拟会话，同一请求重试仍返回同一会话，并核对 `get_session / status`。没有上传素材、排分析任务或云调用。

复验脚本为`suit-demo/check_mcp_live.py`，仅带`--create-simulation`才执行上述新建操作。早期9工具版本的真实工作台验证实际调用了`status / list_sessions / get_report / get_session / create_simulation_session`。当前增加get_analysis_status共10工具，已用合成HTTP/真实ASGI与临时库验证发现、同场逐题复核与历史版本、默认分析进度和compact报告，不把这些测试称为千问桌面客户端验收。

**千问办公桌面客户端的实际导入与点击联调，按用户安排在本地工程完成后继续，目前仍未验收。** 上述协议与旧版本实测记录保留原结论，不能当作本轮客户端通过。升级后重新生成接入包、重启连接器并新建对话，客户端待验收顺序为 `status → list_sessions → get_analysis_status → get_report → list_research_runs → export_research_report`，逐项确认同一场次及报告版本，并验证实际默认基础阶段和可读观察真实返回；旧场次与独立研究的扩展结果单独说明。新建模式联调可分别明确要求 quick 和 full，核对主问题量为 3 和 8。创建、独立排队仅在用户要求时另测；不为检查连接而重跑已有素材。

## 官方依据

- [千问办公连接器：自定义 MCP / STDIO / JSON 导入](https://help.aliyun.com/zh/qwenwork/desktop-connectors)
- [千问办公技能：SKILL.md、安装及存储目录](https://help.aliyun.com/zh/qwenwork/skills)
- [千问办公技能包结构](https://www.alibabacloud.com/help/en/qwenwork/skills-management)
- [MCP STDIO 传输规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [MCP 工具规范](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

官方文档核对日期：2026-10-27。具体客户端界面以当前安装版本为准。
