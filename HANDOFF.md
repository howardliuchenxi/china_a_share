# 交接账本（Cross-tool Handoff Ledger）

> 本文件是 ZCode / Codex / Claude / Gemini 等工具共用的交接账本。协议见 `AGENTS.md`
> 的「Cross-tool handoff ledger」一节：开工先读最新条目；收尾在 DECISIONS 区下方、
> 最新条目上方追加 `## [日期 · 工具] 标题`（做了什么 / 关键决定 / 下一步）；
> 条目超 8 条压缩最旧；永不写入密钥。

## DECISIONS（架构决策账）

<!-- 固定区：架构级决策（模块边界/数据模型/产品拆分/协作规则）对齐后在此登记。
     注：2026-09-22 前 main 无本文件；历史决策条目暂存于分叉分支
     codex/technical-pattern-studies 的 HANDOFF.md，随小单元移植逐步回填。 -->

## [2026-09-28 · ZCode] 澄清提问加台账一致性闸门（选项矛盾剔除/引文核验/已定即结论）

- 背景（用户反馈）：agent 抛选择题时「单看选项很详细、联系整体逻辑不通」——实锤案例即 L4/L5 保留期之问：v2.24 唯一原则已写死三态纪律，选项 2/3 违反仍被并列 offered，且题设漏 r60 与 L6 自动进层。根因：已定口径只活在 12 轮对话窗口文本里，澄清工具契约（question+options 2–4 个）没有任何「对照已定条款」的强制。
- 修复（机制级，双引擎同一咽喉 `ResearchToolbox`）：`request_clarification` 契约扩为 question + governing_terms（逐字引文，≤6 条）+ options[{text, conflicts}] + resolution(open|determined) + determined_answer。确定性闸门：①引文必须在会话语料中逐字存在（空白归一后子串匹配，杜造引文直接拒）；②模型自报 conflicts 的选项在渲染前剔除并留痕日志；③幸存 <2 即拒（要么按 determined 给结论要么给出不冲突选项）；④resolution=determined 时不提问、直接返回「结论+推导」（必须有引文与 determined_answer）。语料接线：GLM 侧 `bind_conversation_texts(request.conversation)`；Codex 侧父进程写 `conversation_corpus.json`（artifact_dir，.json 不进产物持久化）+ 新 env `CODEX_AGENT_CONVERSATION_FILE`（已入 mcp env 白名单）传入 MCP 子进程。提示词纪律同步（_developer_instructions，双引擎共用）+ 锁定断言测试。
- 验证：全量 997 passed / 136 skipped 零失败；新增契约测试 12 个（剔除/幸存<2 拒/determined 即答/缺答案缺引文拒/伪造引文拒+换行容忍/语料文件读写降级/env 导出/提示词锁定）。live 用例不适用说明：本修复属提问行为契约，非某条可断言答案的生产 prompt，闸门行为由确定性单测覆盖（模型侧任何误用都会收到有界错误并可在轮内自纠——GLM 回喂 error、Codex 收 MCP isError）。
- 遗留：①已定口径仍只活在 12 轮窗口内——窗口外滚出的条款对闸门不可见，「台账须存具名策略」的持久化仍是悬空项（需用户拍板存储口径）；②真实飞书端到端一次带澄清的对话待用户观察验证。
- 附注：今日另从 Codex 会话存档（~/.codex/sessions，244 份）恢复 v2.24 逐层标准全表（台账本体在飞书 agent 服务端工作区，不在本仓库）；「L4/L5 到期去向」裁定＝选项 1 语义且为唯一原则唯一合法解。

## [2026-09-28 · ZCode] 卡片全面去表单化：旧客户端不再出现「请升级客户端」占位

- 背景：用户旧客户端实测反馈——占位条是飞书对不支持的表单容器的兜底 UI，上一轮的文本兜底只是绕行，占位条仍在（快捷菜单的研究输入表单 + 反馈表单两处）；用户拍板「还不够，彻底向下兼容」。
- 改动：①反馈入口改发**纯文字指引卡**（`build_feedback_guide_card`，仅 div+note，任何客户端全量渲染；点击→单卡无附加文本消息，内容即 `反馈 2轮 问题描述` 用法）；②**快捷菜单卡移除研究输入表单**（旧卡上的「开始研究」form_submit 解析保留——聊天历史里的旧卡仍可点，新卡不再产生占位条），研究提问回归 @机器人 文本主路径；③文本命令 `反馈 [N轮] 描述` 成为反馈唯一提交通道（表单提交解析同样保留兼容旧卡）；④删除 form/select 相关构建代码与常量。
- 验证：3 个表单 UI 测试改写为 render-safe 断言（整卡 JSON 禁 `"tag": "form"`/`"tag": "input"`/`select_static`），菜单快捷按钮、指引卡结构、文本命令全链路全绿；全量 985–987 passed / 136 skipped（唯一失败为已知 disclosure/dividend flaky 家族，独立跑必过）。
- 遗留：新客户端用户失去菜单内输入框与反馈表单（改为发消息，产品上可接受——@机器人提问本就是主路径）；旧客户端真实点击复验待用户。

## [2026-09-28 · ZCode] 反馈功能旧客户端兜底：纯文本指令通道

- 背景：用户真机截图实证——旧版飞书客户端不渲染 v1 卡片表单容器（表单区显示「请升级至最新版本客户端」占位，卡片其余文字正常），表单形态对旧客户端群成员不可用；用户拍板「向下兼容，改代码更好支持现有版本」。
- 改动：①点击「反馈问题」时在表单卡之外**同时回一条纯文本指引**（`FEEDBACK_TEXT_COMMAND_GUIDE`，纯文本任何客户端可渲染），表单卡 note 也带上同款格式说明；②新增一次性文本命令 `反馈 [N轮] 问题描述`（`FEEDBACK_TEXT_COMMAND_PATTERN`，N∈{1,2,3,5}、省略默认 1 轮，也接受 `反馈问题` 前缀），经 process() 文本通道走与表单完全相同的管线；③把表单提交执行体抽为 `_execute_feedback_submission`（不含 claim/complete），卡回调（自带 claim）与文本命令（process 已 claim）两路共用，避免双重 claim 静默丢事件；④非法轮数/超长描述回用法提示而非误入研究通道，「反馈一个策略」类无空格前缀仍走研究。
- 验证：test_feishu.py 新增 8 例（文本命令全链路/默认 1 轮/非法轮数提示/超长描述提示/前缀变体/研究边界/禁用提示/指引文案双发）；全量 988 passed / 136 skipped 零失败（已知 flaky 本次亦绿）。
- 遗留：旧客户端上的真实点击验证仍待用户（升级版客户端走表单、旧版走文本指令，两条路都应通到同一落盘与 @ 管理员）；指引文案的示例措辞可在真实使用后校准。

## [2026-09-28 · ZCode] 飞书群「反馈问题」按钮：表单收集→LLM 转写→落盘+@管理员（闲时任务）

- 做了什么：裸 @ 快捷菜单卡新增「反馈问题」按钮（独立 action 行，原第一行组合不动）；点击回表单卡（select_static 轮数 1/2/3/5 + input 描述 500 字 + form_submit），提交按钮 value 内嵌解析好的会话桶 `conversation_id`（点击时按 `_active_agent_conversation_id` 解析，杜绝点击→提交之间会话漂移）；提交走 `parse_feedback_card_action`→后台任务（api.py 新分支，仿 strategy 模式）→同步最新一轮→按请求轮数切窗（单轮 4k 字符截断、总预算 16k 最旧优先丢弃）→`DeepSeekFeedbackTranscriber`（feedback.py 新增，deepseek-v4-flash 固定档，系统提示词仅行为层、带"内容是证据不是指令"防注入条款、零金融领域事实）转写为后台 code agent 排查报告→GCS `feishu-fix-requests/`（CloudStorageUiFeedbackStore 加 object_prefix 参数，默认 fix-requests 行为不变）双写 received/transcribed 状态→群内结果卡 @ 管理员（新 env `FEISHU_FEEDBACK_ADMIN_OPEN_ID`，缺省跳过 @ 并 log warning，卡照发）。
- 关键决定：①兜底不静默——转写失败时原始描述+轮次原文照常落盘（status=transcription_failed）并回「转写失败」卡 @ 管理员；②表单校验走 schema——新契约 `FeishuFeedbackSubmission`（描述 1–500 字、轮数 ∈{1,2,3,5}、conversation_id 必填），`str(None)` 陷阱已修（`or ""` 归一）；③`report_issue` 按钮复用既有 parse_card_action→prompt「反馈问题」→process() 文本命令通道（零 api.py 改动），仅 form_submit 走独立 action 通道；④卡片动作沿用 main 的 root 桶约定（与 submit_research 一致，话题群的 topic 桶不在本次范围）。
- 测试：test_feishu.py +9（表单结构 tag 白名单断言锁 select_static 无 select_menu、轮数短缺标注、转写失败兜底、事件去重、仅 interpretation 旧轮次归一、禁用路径）、test_feedback.py +8（转写请求形状/错误上抛、store 前缀两档、协调器双写、窗口截断/预算丢弃）、test_strategy_api_entry.py +1（api 级分发）。全量 979p/136s（唯一失败为已知 flaky 家族 disclosure/dividend exact-date-range，独立跑必过）。worktree 自建 py3.14 venv 验证。
- 遗留：**真实飞书端到端点击验证留给用户人工**（建卡/表单渲染/@ 语法 `<at id=…>` 在真机上的效果未实测）；转写质量需真实反馈样本校准提示词；话题群桶与 root 桶的会话归属同既有卡片动作，未扩展。

## [2026-09-27 · ZCode] GLM 有界循环耗尽硬失败改收尾作答 + worker 结构化日志（闲时任务）

- 生产反馈（群内用户易来发「3」，GCS task `8429d1e7…`）：该「3」是上一轮四个编号选项里的选项3（L7 完整链条信号日，最重分支），GLM 循环烧满 120 轮后 raise「exceeded the bounded tool-call limit without an answer」，任务失败且零答案；近 5 天同类错误 12+ 次（系统性）。次要发现：worker 进程从未配置 logging，`glm_agent_round` INFO 轮级遥测全被吞（这次失败 7 分钟只有 2 条日志，无法还原工具轨迹）。
- 修复（机制级，无任何针对「3」的特例）：①`glm_agent.py` 循环在无终答时（轮预算耗尽或空 content 轮）追加一个**不带 tools 的收尾轮**，强制模型基于已获取信息产出最终答复/进度汇报/澄清提问；连收尾轮都空时兜底返回中文重试指引（日志 `glm_agent_finalization` / `glm_agent_finalization_empty`）。provider/配额错误仍照常 raise（`_friendly_glm_error` 翻译）。停滞止损（8 轮被动工具）保持原样。②`configure_logging` 下沉到 observability.py，worker.py main() 接上——今后 GLM 轮级工具遥测可在 Cloud Logging 按 conversation_id 查询。
- 测试：新增 3 个单测（预算耗尽→收尾作答、空 content 轮→收尾作答、连收尾为空→兜底文案，FakeSession 断言收尾请求无 tools 键）；test_server 增加 worker 日志接线断言；test_feishu_agent 尾部新增 GLM live 用例（「3」+ 真实澄清问题原文，llm_preference="glm"，断言可读答复且包含信号日/L7）。
- 遗留：本条目提交后需在 Cloud Logging 验证 worker INFO 遥测生效；GLM 对超重任务（选项3类）单轮仍可能做不完全部计算——现在会诚实汇报进度与下一步，根治仍等智谱 /responses 上线后切 Codex harness（D-2026-09-20）。

## [2026-09-26 · Codex] Enforce one workbook with replaceable topic tabs

- Fixed production task `3311be2d…` (`按照以上内容，补全模型`), which completed research but failed delivery because an extra supported file made the terminal artifact scan ambiguous.
- `export_excel` now owns one canonical `a_share_research.xlsx`; distinct stable `sheet_name` values add topic tabs, while reusing a name atomically replaces stale content in that tab.
- Delivery prefers the canonical workbook and logs/ignores non-terminal CSV/PDF/DOCX/XLSX files instead of failing the completed task; legacy single-file runtimes remain supported.
- Workbook methodology and column-note indexes now retain per-topic metadata, and the viewer reads the latest exported topic while remaining compatible with legacy workbook layouts.
- Validation: targeted 33 passed / 8 skipped; full suite 951 passed / 135 skipped; exact production prompt passed against real Codex/DeepSeek with one workbook and multiple tabs in 197.88s. The known concurrent disclosure-call ordering test failed once in the first full run, passed alone, and the complete rerun was green.

## [2026-09-26 · ZCode] 测试提速与前端整体移除（闲时任务代跑）

- 做了什么：①DeepSeek planner 重试退避改为构造参数 `retry_delay_seconds`（默认生产常量 1s），单测注入 0——全量 113.5s→59.8s（949p/134s 全绿，未加 skip 未弱化断言）；②按用户拍板整体移除前端：frontend/ 26 文件、Dockerfile node 构建阶段、api.py 页面路由与 StaticFiles 挂载、Makefile npm 构建、pre-push 钩子 node_modules 软链、cloudbuild frontend-check 步骤、PI UI feedback workflow（触发源与构建对象均为前端）、repository_context 的 frontend/src 白名单，API 路由与飞书 webhook 原样保留（947p/134s 全绿，−2 个前端主题测试）。
- 关键决定：discovery rule_search 慢测试（16.7/8.3/4.9s）实测为 pandas 固定开销（~21ms/次公式评估，缩合成数据行数无效），不动统计核心（四关判据地基）、不缩数据（会弱化断言）——保留原规模，留给后续专项。
- 遗留：`/` 页面随下次自动部署消失（已获批）；`make check` 因运营方此前已禁用后端门禁测试而成为空门禁（本次仅移除 npm 构建）；残留 frontend 字样仅存在于历史记述（HANDOFF/docs/audit）与线上可观测性资源名（frontend_request_total 指标与 dashboard.json，改名属云资源变更不在本次范围）。

## [2026-09-26 · ZCode] 修复分层时序模型判断逻辑：条件日期绑定 + 倒推锚定纪律

- 生产反馈（2026-09-26 群内，GCS task 423056c1…）：用户定义 v2.3 分层模型后问「603823，在8.10这一天 属于哪一层」，agent 两处逻辑错误——①拿查询日 L1 准入条件（当日 PE 超段上限）否决整条历史链条（把准入条件当持续在册条件，与「每层条件只限该层当日」冲突）；②默默执行「下穿当日须 cond 成立」的同日自相矛盾条件（cond 含 MA5>MA20，下穿即 MA5<MA20 → L5 恒空）而不上抛。
- 修复（通用纪律，非特例）：`_developer_instructions` 新增 staged temporal models 段（glm_agent import 同一函数自动生效）——①每层准入条件只在该层自身事件日测试，查询日未过早期层准入不得推翻既有链（除非模型明示每日重筛）；②判断某日所属层级＝倒推锚定：定位该日前最近一次转移、只在该转移自己的日期用当日 as-of 数据验证；③条款同日矛盾/互斥读法必须编号选项+推荐默认上抛，字面读法下恒空的层必须显式标记；④长链进 sandbox 逐日重放。措辞领域无关（测试负向断言锁住无 A 股词汇），符合 D-2026-09-20 提示词解耦决策。
- 回归：test_feishu_agent.py 新增 live 用例（v2.3 总本逐字作对话历史 + 生产原句 prompt），断言失败签名（唯一阻断点/即被剔除）消失、必须倒推锚定或澄清菜单、执行时必须暴露同日矛盾；真 DeepSeek/Tushare 5 分 52 秒跑通。test_codex_agent.py 锁纪律锚点文本。
- 遗留：自发现同类错误的「探针框架/影子巡检」与一键反馈闭环本次未做（用户明确本次仅修此问题）；答案证据化+机械校验是后续更强的形态。

## [2026-09-23 · Codex] Freeze and replay flexible natural-language strategies

- Replaced lossy fixed-type suggestion buttons with compilation through the validated general `QueryPlan`; saved strategies now retain the exact source text, interpretation, immutable plan, anchor date, and SHA-256 fingerprint.
- Daily execution and date-range trials replay the confirmed plan with date rebinding only, use the exchange calendar, preserve audit columns, and never ask the model to reinterpret the saved rule.
- Added `试算规则 [name] YYYY-MM-DD 至 YYYY-MM-DD` and `删除规则 <name or id>` while retaining per-row delete actions; ambiguous duplicate names fail visibly instead of guessing.
- Added both reported prompts to the live regression catalog and local regression coverage. Targeted tests: 62 passed; frontend build passed. Full suite: 947 passed / 133 skipped with one pre-existing nondeterministic concurrent-call ordering assertion; both affected tests pass alone.
- The real paid DeepSeek/Tushare replay was not run because external transmission was not authorized in this session; no cloud deployment was triggered manually.

## [2026-09-23 · ZCode] 飞书「研究任务操作失败」事件修复：dispatch 有界重试 + prompt 长度契约统一

- 线上报错事件 b3570598cabe31ad51c7357f9590ff02：`CloudRunJobDispatcher.dispatch` 对 run.googleapis.com 的单次 POST 被 `RemoteDisconnected` 打断（keep-alive 连接被回收），无重试直接把用户请求打失败；用户 3 分钟后重发自愈（task c214bb59…，原话「请将 1、2、3 建议依次都执行」）。30 天内同族失败 3 起，另两起：09-20 超长 prompt 撞 `FeishuTaskRecord.prompt` 1000 上限（任务已派发后才炸，事件-任务关联记录丢失）；09-14 research_chat `calendar` 遮蔽（main 上已被他人修复为 `trading_calendar`，无需再动）。
- 修复①：dispatch 加有界重试——`DISPATCH_MAX_ATTEMPTS=4`、0.5s 指数退避，仅对 `requests.ConnectionError/Timeout` 与 408/429/5xx 重试；4xx 立即报错。注释明示残余风险：极小概率「请求已落地但响应丢失」时重试会产生重复 execution，任务存储语义下表现为重复回复而非数据损坏。
- 修复②：prompt 长度契约统一为 `MAX_ANALYSIS_PROMPT_LENGTH`(4000)——入口 `process()` claim 后先友好拦截超限（回复实际字数与上限，不再进入提交流程）；`FeishuTaskRecord.prompt`、`FeishuConversationTurn.prompt`、`AnalysisConversationTurn.prompt`、agent 侧两处硬编码 4000 全部对齐同一常量，消除「1001-4000 字问题受理后中途炸」的窗口。interpretation 1000 上限未动（模型侧按设计输出紧凑摘要，30 天无相关事故）。
- 验证：新增 4 条 dispatch 重试测试（瞬时恢复/5xx 恢复/耗尽报错/4xx 不重试）+ 2 条长度契约测试（3500 字全链路受理并作为下轮上下文回放、4001 字友好拦截）；全量 942 passed / 131 skipped；`make pre-push` 过。未跑 live 用例：本次改动是传输层加固与模型上限对齐，报告的 13 字原话不触发任何被修路径，live 重放无回归价值（记录此判断）。
- 推送 origin/main（-rv 工作树），部署走 Cloud Scheduler 自动 reconcile。

## [2026-09-22 · ZCode] 修复引用回复分裂会话：菜单序号跟进丢失上下文

- 群内事故：机器人让用户「回复序号 2」，用户引用回复机器人消息后答「没有历史上下文」。根因：conversation_id 拼入 `thread_id or root_id or "root"`，普通群的引用回复带 root_id 落入新会话桶，历史全丢（feishu.py 解析层，首版遗留）。
- 修复（b2ac1e03）：会话身份 = `tenant:chat:topic:sender`，topic 仅取话题群平台级 thread_id；root_id（引用拓扑）不再参与。话题群隔离保留；同群同用户的普通群/单聊回复与直发统一桶，显式隔离交给命名会话。
- 测试：引用回复身份连续性（双向）+ 话题群仍分区 + 端到端「菜单答复→引用回复 2」保留历史（旧代码实测失败）；live 用例「研究对象为A股所有股票→2」真 DeepSeek+沙箱 22s 通过。全量 936 passed；pre-push 门禁过，已推 main。
- 遗留：生产复验等 Cloud Scheduler 自动 reconcile 后用户群内复测；旧桶历史（含 root_id 键）一次性丢弃，未迁移。
