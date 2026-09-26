# 交接账本（Cross-tool Handoff Ledger）

> 本文件是 ZCode / Codex / Claude / Gemini 等工具共用的交接账本。协议见 `AGENTS.md`
> 的「Cross-tool handoff ledger」一节：开工先读最新条目；收尾在 DECISIONS 区下方、
> 最新条目上方追加 `## [日期 · 工具] 标题`（做了什么 / 关键决定 / 下一步）；
> 条目超 8 条压缩最旧；永不写入密钥。

## DECISIONS（架构决策账）

<!-- 固定区：架构级决策（模块边界/数据模型/产品拆分/协作规则）对齐后在此登记。
     注：2026-09-22 前 main 无本文件；历史决策条目暂存于分叉分支
     codex/technical-pattern-studies 的 HANDOFF.md，随小单元移植逐步回填。 -->

## [2026-09-25 · Codex] Add idempotent Feishu execution and research evidence manifests

- Added atomic Cloud Storage generation-guarded task leases, legacy running-task recovery, and duplicate-worker suppression without changing normal progress or terminal message content.
- Added a durable terminal delivery outbox; successful research and all delivery intents are stored together, failed text or workbook delivery receives one bounded retry without rerunning research, and viewer bearer tokens are never persisted.
- Added task result fingerprints plus an executor-owned `ResearchManifest` covering dataset sources, validated parameters, dates, units, formulas, missing counts/rates, completeness evidence, code revision, and answer/artifact hashes.
- Existing task JSON remains readable through defaults. Targeted reliability/unit checks passed (57 passed, 6 skipped); the complete suite reached 954 passed / 133 skipped with one pre-existing concurrent-call ordering assertion, which passed on isolated rerun.
- No paid live provider/model case was needed because the change is persistence and delivery infrastructure rather than a production-reported research prompt; no cloud resources or deployment were changed.

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
