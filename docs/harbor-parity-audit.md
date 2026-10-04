# Harbor 与原版导航 Agent 的一致性检查（历史审计）

当前基线已按实际正式 GLM 记录更新：原生工具、500 步、6 小时兜底、100 轮摘要和坐标锁 v2。下面的 XML/1000 步/50 轮数值描述历史版本，不是当前默认。

更新：该审计记录的是已替换的简化循环。当前入口已改为运行原版 `agent.main`，
见 [修复后的运行说明](harbor-pilot.md) 和任务 validation.json。以下保留审计时的证据。

再次核对初始提交（来源编号已匿名化） 后发现，之前“原版”指的是已整理过的匿名 runner，
其中仍有 system prompt 改写；Harbor instruction.md 也另写了后台阈值说明。
现已恢复默认 XML system prompt 全文及动作事件格式，以初始提交的全文哈希校验；
Harbor 导航指导由同一份提示词生成。此前 GLM 记录保留旧提示词，不追溯改标。

日期：2026-09-30。检查对象是本匿名源码中的 finalpool 原版 Agent、正式导航设置、当前 `NavigationVisionAgent` 与 Harbor 0.23.0 生命周期。没有用主仓库后续实验配置替代这个基线。

结论：游戏、任务及完成规则沿用原版；目前的 Harbor 模型循环是独立编写的简化实现，尚未达到原版 Agent 行为一致。此前通过的是针对性的隔离、文件接口和判分防篡改验证，并不证明 Agent 策略一致。本次只检查和记录，未修改运行代码或启动新模型任务。

## 先纠正重试说明

通用 `Agent` 构造函数默认 SDK 重试 2 次，但正式导航配置覆盖为 0。实际运行同时设置 `max_consecutive_llm_failures=3`，原版决策循环在请求失败后等 2 秒继续，成功后清零连续失败计数。第三次连续失败才停止，并记录 `llm_failure_limit` 基础设施错误。Harbor SDK 重试也是 0，但第一次异常就抛出并结束，缺的是外层失败恢复。直接改成 SDK 重试 2 次并不等于恢复全部原版行为。

证据：[正式设置](../eval/navigation/settings/final-navigation-v1.json)、[启动时配置注入](../scripts/eval/run-navigation-task-inside.sh)（694 行）、[原版决策循环](../agent/agent.py)（3820 行）、[Harbor 调用](../eval/harbor_agents/vision.py)（130、148 行）。

## 已确认的差异

| 优先级 | 项目 | 正式导航原版 | 当前 Harbor | 影响与对齐方向 |
|---|---|---|---|---|
| 高 | 请求失败恢复 | 外层连续失败上限 3，失败等 2 秒，成功清零；上下文溢出另有恢复 | 首次请求异常直接终止 | 本次 HTTP 500 中断的直接原因；复用外层恢复及失败计数 |
| 高 | 自动摘要和图片预算 | 图片超过 100、窗口达到 50 个 assistant turns、单次 usage total_tokens 超过 200000 等条件触发摘要；保留原始任务并重放未读反馈 | 保留全文，直接删除第 20 张以前的图片，没有摘要或上下文溢出恢复 | 模型记忆、输入长度和长期导航策略改变；20 万不是整次运行累计 token |
| 高 | 到点确认反馈 | 消费 evaluator-events.jsonl，将必经点记录及终点到达消息实时注入上下文 | 没有事件读取接口或消费逻辑；只有申报反馈与最终结果 | 模型收不到原本可见的到点确认，可能反复验证或漏判自身进度；不能以额外 oracle 坐标替代原事件 |
| 高 | 执行、观察与中断 | exec_async；observe_after_sec 默认 2 秒、至少 1 秒；执行中可观察、skip、stop_execute，新 exec 会停止旧动作 | 同步 /exec，完成后才进入下一轮；没有 observe_after_sec 或 stop_execute，stop 表示放弃整题 | 对长动作的反馈和纠错能力改变；应复用原执行状态机 |
| 高 | 系统提示词 | build_navigation_system_prompt，含边界警告、坐标/转向语义、操作说明，以及到点 2.5 格、垂直 1.5 格、等待 3 秒的操作建议 | 自写短提示词；用户指令后追加 nav 使用说明和真实评分半径 3.5 格 | 模型指导内容改变。2.5 格/3 秒是原提示词建议，不是修改后的评分条件；实际评分半径仍是 3.5 格 |
| 高 | 动作协议及计数 | 默认 XML，可显式选原生工具；即使原生工具也严格要求每轮一个动作；成功决策上限 1000，摘要请求不计入 | 固定自定义原生工具；遍历执行回复中的全部 tool_calls；按 for 循环轮次计数 | 同样“1000 决策”不保证相同动作预算；本次 23 轮回复执行了 24 次 exec |
| 高 | 终止归因和判分生命周期 | decision_limit、llm_failure_limit、agent_crash 等分别记录；LLM 连续失败属于基础设施错误 | finish 统一写 harbor_agent_finished；未捕获的 RuntimeError 使 Harbor 跳过正常 verifier 步骤 | 本次 trial 报 API 错误且 verifier_result=null，但收集到的 completion 标记普通停止、infrastructure_error=false；记录归因不一致，需要可信宿主生命周期传递原因 |
| 中 | 命令默认超时 | 正式配置每次 exec 300 秒，超时产生 exit code 124 和明确状态 | 默认 30 秒，模型显式传值才能达到 300；同步 /exec 超时响应也未统一为原版命令结果事件 | 原本合法的长脚本可能提前结束；默认值和返回语义都需对齐 |
| 中 | 截图链路 | JPEG quality 75；失败默认额外重试一次；动作前/事件/执行中/执行后观察与帧过滤机制 | PNG；单次捕获和下载失败即异常；每个回复批次结束取一张新图 | 图片内容编码、时机、数量、请求体积和暂时故障容错不同；不能据此断言本次 500 的服务端根因 |
| 中 | 空回复和非法动作 | 空 assistant 不追加，进入失败计数；非法动作注入明确纠正反馈后继续 | 直接追加 message；无工具调用连续三轮就异常，多工具调用不拒绝 | 可能污染后续历史；“连续无动作三次”和“连续请求失败三次”不是同一规则 |
| 中 | GLM/provider 兼容 | GLM 图片解析 1210 错误有最多三次额外退避；支持参数兼容修正、可选 429/请求门控、Responses 和 Gemini 回放等 | 直接 AsyncOpenAI Chat Completions；没有这些兼容层 | 1210 对 GLM 直接相关；429 门控等为可选能力，不能说正式基线默认开启；Gemini/Responses 不直接解释本次 GLM 500 |
| 中 | 配置来源 | setting/run.json、model_parameters、api/action protocol 与采样设置经统一启动链解析 | 多个行为参数写死；模型参数直接从外部本地模型配置读取 | 已沿用用户选定的 glm-5.3-flash/max/thinking/max_tokens=32000，但未证明与某个历史正式 GLM run 的完整配置相同 |
| 中 | 证据与日志 | messages.jsonl、positions.jsonl、metrics、supervisor、动作/观察/摘要时间绑定等 | 自定义 events/frames/progress；显式 world artifact 只收 completion.json | 原版分析和查看器不能完整复用；未显式导出的游戏轨迹随临时容器删除而丢失，需收集到宿主可信目录 |
| 范围差异 | 处理图片及文件接口 | 原版动作集合没有独立 read_image；原 /tmp 每次动作重置的问题已在本匿名源码共同修复 | 增加 read_image 工具和两个 workspace 间的图片传输；容器拆分与独立 verifier | 图片接口改善可用性，但也是工具能力变化；与隔离改动分开记录，不能自动视为历史基线一致 |

主要代码位置：

- [原版 Agent](../agent/agent.py)：到点反馈 816/864、摘要触发 2361、摘要重建 3066、provider 兼容 3199/3239、决策循环 3694、严格单工具解析 4377。
- [原版环境](../agent/env.py)：JPEG 与截图重试 302、异步启动 618、任务监控及超时 668。
- [原版提示词](../agent/main.py)：660、874。
- [Harbor 模型 Agent](../eval/harbor_agents/vision.py)：默认超时 57、图片裁剪历史 75、捕获 104、请求 130/148、多工具循环 170、无动作退出 201。
- [Harbor 网关](../eval/harbor/innopolis-006/environment/world/gateway.py)：review 模式 48、同步 remote 72、统一终止 116、截图 179。
- [原版启动与监控](../scripts/eval/run-navigation-task-inside.sh)：694–695 设置重试及失败上限，728–739 区分终止原因；[monitor](../scripts/eval/monitor-navigation-goal-inside.py) 226 定义基础设施错误。
- Harbor 0.23.0 安装代码 `harbor/trial/single_step.py`：`_run` 在 agent 正常返回后判分，`_run_agent` 只专门捕获 AgentTimeoutError 和 NonZeroAgentExitCodeError；一般 RuntimeError 不走正常判分路径。本项同时由本次日志确认。

## 本次实际运行证据

对 2026-09-29 修复后的 GLM 运行只统计可见动作、错误及 usage 元数据，没有用模型推理文本推测原因：

- 23 次模型响应、24 次 exec；所有已记录命令退出码为 0。
- 第 24 次请求返回 InternalServerError / HTTP 500；无重试即中止。
- 首次与最后一次成功请求的 prompt_tokens 分别为 1663、24915。这个增长不能证明 500 由上下文或图片引起。
- Harbor `verifier_result=null`；独立收集的完成记录却为 `harbor_agent_finished`、`success=false`、`infrastructure_error=false`，表明终止原因没有传递。
- 尚无该任务的成功 GLM 导航结果。失败发生在 50 轮摘要阈值前，不能说本次已触发摘要缺失导致的故障。

## 已对齐的部分和修复顺序

原始俄文任务、起点、三个有序中间目的地和终点、修正后的点位、真实位置采样、3.5/1.5 格到达判据、三次申报限制、死亡/越界处理、5400 秒游戏计时均复用原版 monitor/setting。600 秒单次模型超时也一致。Harbor 另有包含启动的 6000 秒 agent 上限，预算边界仍需验证。图片分辨率的现有 smoke 为 800×600，本审计没有将编码差异误写为分辨率改变。

原版导航规则复用与 Agent 行为一致是两项不同验收：前者已有基础测试，后者尚未通过。

建议复用原 Agent 和 provider/上下文/动作状态机，让 Harbor 只负责容器生命周期、传输和独立判分。优先完成：外层失败恢复及可信终止归因 → 原到点事件传递 → 原提示词、动作协议和异步观察 → 摘要/图片预算、截图恢复 → 完整日志与配置绑定。若保留当前简化 Agent，应明确标记为另一种 Agent 配置，不能与原正式结果直接混用。
