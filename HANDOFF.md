# 交接账本（Cross-tool Handoff Ledger）

> 本文件是 ZCode / Codex / Claude / Gemini 等工具共用的交接账本。协议见 `AGENTS.md`
> 的「Cross-tool handoff ledger」一节：开工先读最新条目；收尾在 DECISIONS 区下方、
> 最新条目上方追加 `## [日期 · 工具] 标题`（做了什么 / 关键决定 / 下一步）；
> 条目超 8 条压缩最旧；永不写入密钥。

## DECISIONS（架构决策账）

<!-- 固定区：架构级决策（模块边界/数据模型/产品拆分/协作规则）对齐后在此登记。
     每条三行：决策 / 为什么 / 何时可推翻；新决策加本区最上方。
     工作条目加在本区下方，不再加文件最上方。 -->

- **D-2026-09-20 · 模型层与金融领域规则解耦：领域事实住在接口层，模型层只读说明**
  - 为什么：用户明确原则——接口层新增字段说明，模型层读取说明；具体金融规则（单位、字段语义、口径约束）不进任何运行时的提示词。这样换模型/加模型零迁移成本，规则只在接口层维护一处（`registry.FIELD_UNIT_NOTES` + 目录 prose + `_result_payload` 的 `field_units` 透传即为该模式的首个实现）。
  - 执行含义：①新增领域事实一律进 registry（结构化表+prose，测试锁一致）或返回 payload，禁止写进 glm_agent/codex_agent 提示词（防回流测试已有）；②提示词只允许通用行为纪律（读 field_units、声明换算、sanity check、澄清歧义）；③机制可推广——字段语义/复权说明/口径警告等都走同一模式，需要时把 FIELD_UNIT_NOTES 泛化为字段元数据表即可；④**待对齐项**：`_developer_instructions` 里并行会话加的「THS taxonomy contract」段（基金流路由规则）是当前唯一在模型层的领域规则，被对方测试锁定——下次与其对齐后应下沉到接口层（moneyflow_ind_ths 的 catalog guidance 已含全部所需语义），并改写其测试。
  - 落地登记（用户要求所有工具记住）：ZCode 持久记忆 + 本 DECISIONS 区 + **~/.codex/AGENTS.md 已追加「Domain-Facts-in-Interface Layering」节**（对 china_a_share 强制生效，含待对齐项）。
  - 可推翻当：用户明确允许某类领域规则进提示词（如为效果做的定向调优）。

- **D-2026-09-20 · 研究模型统一走 Codex harness：GLM 及未来模型的目标接入方式与 DeepSeek 相同（上层 Codex 编排，模型只做可插拔 provider）**
  - 为什么：用户明确拍板。单一编排层（规划-执行-纠错/工作区/产物）能力天然对齐、维护成本最低；为每个模型养私有 harness 是重复投资。当前 `glm_agent.py` 的 chat 工具循环是协议阻断下的**过渡桥**（Codex SDK 0.154 只认 Responses 协议且已删 chat；智谱编码端点 `/responses` 2026-09-20 仍 404），不是目标态。
  - 执行含义：① 任何相关任务先探测智谱 `/responses` 是否上线（一条 curl），上线即把 GLM 切回 Codex harness 并废弃私有循环；② 新模型选型优先支持 Responses 协议的厂商，保证直接插入 `llm_preference.py` 注册表 + bootstrap 映射即用；③ 不往 GLM 私有循环持续堆编排能力，增强优先投 Codex 层。ZCode 侧已同步写入其持久记忆。
  - 可推翻当：用户明确放弃统一编排层，或 Codex SDK 被替换为其他 agent 框架。

- **D-2026-09-20 · 非琐碎改动先对齐再开工；开工后一杆到底，不打断**
  - 为什么：未经对齐的改动会累积架构不一致；中途打断破坏执行连续性。规则全文已落在各工具全局配置（~/.codex/AGENTS.md、~/.claude/CLAUDE.md、~/.gemini/GEMINI.md，ZCode 侧为其持久记忆）。
  - 可推翻当：用户明确要求恢复「直接开工」或「逐步确认」模式。

## [2026-09-30 · Codex] 复验美股规则并完成组合化与左尾治理

- 原 15 项测试与完整流水线复验通过，85.5 万事件及 gapdown2 / mom252 / high52w 锚点精确一致；新增后共 24 项测试通过。
- P1 将 4 条 N=5 事件规则实现为 5 袖套重叠组合；四条均未同时通过月度 CI 下界与 16/24 正超额月稳定性线。
- P2 明示深缺口过滤和 1%/2%/3% 聚合单票上限：过滤未改善左尾或组合超额；上限降低回撤但也削弱累计超额，五方案均未通过组合判定。
- 交付 `REPORT.md` 与两页审计工作簿（69,458 条完整持仓事件）；提交为 `502fd28d`（P1）和 `519307c5`（P2）。
- P3 A 股复验按本轮对齐结果延期，未改动既有 1,000 万行分片面板。

## [2026-09-29 · Codex] Validated six A-share strategy families

- Added an isolated, reproducible study under `studies/a_share_strategy_search/`; no production application code was changed.
- Repaired 426 endpoint-day gaps with study-local overlays, then evaluated 660 frozen candidates across train, validation, and blind periods ending 2026-09-23.
- Selected one rule per family without using blind data; none produced positive and statistically robust blind net excess return after costs.
- Delivered a two-sheet Excel report with 111,481 reconciled event rows, cost sensitivity, data-quality disclosures, and full execution dates.
- Focused tests, independent result reconciliation, workbook rendering, and saved-file inspection passed.

## [2026-09-27 · Codex] v2.24 condition effect validation

- Built an auditable L1-L7 daily state-machine study for 2025-01-01 through 2026-09-24, including a narrow counterfactual that reapplies L1/L2 to L3-L6.
- Main result: v2.24 produced 5,077 L7 events and 3,693 canonical-only events, but N10 hit rate stayed below 50%, median/trimmed/excess returns were negative, and quality-difference confidence intervals crossed zero.
- Delivered an executed notebook, an English methodology report, tests, and a two-sheet Excel workbook with 6,473 event rows and N1-N10 audit fields.
- Material limitation: current THS industry membership was applied historically because the available membership response lacks effective dates; L10 remains unevaluable until sell/holding rules are defined.

## [2026-09-20 · ZCode] GLM 循环停滞止损 + 轮级遥测（b7ba50d4）

- 用户问「60 轮能否减少/架构是否需升级」。判断：静态调小数字会误伤慢而有效的长链路，正确旋钮是**停滞检测**——`GLM_PASSIVE_TOOLS`（search/clarification）连续 `GLM_RUNTIME_STAGNATION_LIMIT=8` 轮占满即提前终止（群内人话提示：细化问题或切回 DeepSeek），60 硬顶保留兜底；同时每轮 `log_event("glm_agent_round", tools=…)` 补上轮级遥测（对照实验时失败轨迹不可见的缺口）。
- 测试：停滞第 8 轮精确止损（消耗=LIMIT）、被动轮-干活轮-被动轮混合不误伤；全量 916 passed，已推 main。
- 架构结论（回应「要不要升级」）：不需要为此动架构——空转的根因已归因为编排/模型长链路（见上条对照实验），止损只是止血；根治仍是等智谱 /responses 后 GLM 直连 Codex harness。

## [2026-09-20 · ZCode] 特殊逻辑全库审计 + 泛化两单元已推 main（15d55339 / 0011d847）

- 审计结论：真特例三处——①共享规划提示词写死三公司代码映射（test_components 原第 954 行锁定）②workflow 按 transform 名字键控的散点行为分支③codex_agent 的 THS taxonomy contract（即 D 区登记的待对齐项，本次未动）。可接受不改：fanout 校验哨兵 000001.SZ、RANKING_METRIC_PROMPT_ALIASES 别名表、示例文案。
- 单元①（15d55339）：删映射句，改通用指令（trusted_security 块权威 + 无块公司走 stock_basic 精确名匹配 + fan-out 绑定）；`_append_resolved_security_code` 泛化为「每个无歧义名称各注入一块」（包含关系取最长名、按 code 去重、同名多 code 跳过，不再抛歧义错）；`_resolve_prompt_security_code` 多块感知——先剥离 trusted 块再扫显式代码，顺带修掉旧实现把块内 ts_code 误当显式代码的隐性缺陷。新增宁德vs比亚迪多公司、最长名优先、同名歧义跳过三个用例。
- 单元②（0011d847）：period_return 三个散点行为（先过滤后变换 / 终点日期钳制 / 专用全市场读法）收敛为 `TRANSFORM_BEHAVIORS` 契约表；planner 披露附注改 `TRANSFORM_DISCLOSURES` 表驱动并加合成 transform 泛化测试；残留 `transform ==` 仅两处实现分发（cr10 构建器、period_return 实现）。
- 验证：worktree /tmp/glm-final（py3.14 venv，main 需 ≥3.10 而主检出 .venv 是 3.9）全量 914 passed / 129 skipped；exact-range 顺序偶发（dividend 变体）出现一次、单独跑通过、复跑全绿——维持待专项。主检出已删 12 个 fix_test*/test_tushare* 临时脚本，.zcodeignore 已提交入库。
- 遗留：③ THS contract 下沉需与并行会话对齐其测试改写后实施；fanout 哨兵值加注释即可（低优先）；分支未提交 diff 里 fanout 判定的 cr10 名字分支建议改查 TRANSFORM_BEHAVIORS。

## [2026-09-20 · ZCode] 对照实验定归因：60 轮耗尽=编排差距×模型长链路差距，双因素实锤

- 设计：同一工具循环（GlmFeishuAgentRuntime 参数化）指向 DeepSeek chat API 跑真实题，隔离「循环」与「模型」变量。结果矩阵：q4（Ln 调参）ds-flash×轻循环**同样 60 轮耗尽**（972s）→ 该题瓶颈是编排层；q2（A5 行业聚合，最重）ds-flash×轻循环**成功且仅 399s**（比 Codex 2308s 快 6 倍）而 glm-5.3×同循环失败 → 该题暴露 glm-5.3 长链路自主收敛的模型级差距。
- 结论：GLM「60 轮耗尽」= 编排脚手架缺失（工作区沉淀/任务分解/上下文压缩，q4 证明）+ glm-5.3 超长工具链收敛弱于 deepseek-flash（q2 证明），两因素叠加；单步素质（q1 数值全对、口径更全）GLM 不弱。Codex 在最重题上反而笨重（38 分钟 vs 轻循环 6.6 分钟）。GLM 补跑（按量端点）最终战绩 1/4。
- 附：可观测性缺口——GLM 循环未记录每轮工具名（失败时进度事件为空），下轮触碰 glm_agent 时补轮级轨迹日志（log_event 每轮 tool name/轮次）。
- 证据文件 /tmp/replay/out/（重启即失，结论已录此处）。

## [2026-09-21 · ZCode] 研报数据接入（broker_reports·东财）+ 数据时效契约 + 撤回修复

- 起因：群内问「你能查到宁德时代最近的研报观点吗？」，机器人如实答无研报源，却拿 2025-01-21 的业绩预告当「最近」替代——接口层缺口 + 时效判断缺陷（用户确认主要问题是日期快 2 年）。
- 探活结论：Tushare `report_rc` 有权限且字段全（评级/目标价区间/分季净利与 EPS），但配额 1次/分钟 + 10次/天且失败请求也计数——对群机器人不可靠 → 保持未审计原状；新建 `providers/eastmoney.py`（reportapi.eastmoney.com，免费无 key；**beginTime/endTime 必填**，无窗口默认近 3 年；单次 100 条最新优先；24h 缓存）暴露 `broker_reports`：capabilities 审计 shape（窄形状在前防遮蔽）+ 目录 guidance + FIELD_UNIT_NOTES 单位，bootstrap composite 变为 tushare+us+eastmoney 三方路由。
- 时效契约（接口层，沿用 field_units 模式）：registry 新增 `DATA_RECENCY_DATE_FIELDS` + `data_recency_note()`；`_result_payload` 对所有含披露日期列的数据集注入 `data_recency`（最新记录/as_of/age_days）；forecast/express 目录明示「事件性披露可能陈旧，问最近表现优先 fina_indicator/income 最新报告期」；两运行时提示词只加通用 Recency discipline（防回流测试锁定：无 report_rc/研报字样）。
- 230011 修复：专用异常 `FeishuSourceMessageWithdrawnError`（定义在 feishu_agent、feishu 反向导入防环——feishu 本就依赖 feishu_agent）；coordinator：planning 阶段撤回→FAILED/cancelled 不再烧模型，中途撤回→保留成果静默停投递，reply_file 撤回→跳过不误报，worker 不再崩。
- live：原话入 live_cases（broker_reports-production-624da11d，invariant planned_operation_broker_reports），真 DeepSeek + 真数据源跑通；live fixture 改为 composite。全量 928 passed；catalog 计数 115→116。已推 main（部署走 Cloud Scheduler 自动 reconcile）。
- 遗留：①线上复验研报问答（含中途撤回不再报错）；②东财为非官方端点，若失效可给 report_rc 补审计做后备（数据路径已验证）；③「券商金股」类问题可再评估 broker_recommend（month 必填，已探活 278 行可用）。

## [2026-09-20 · ZCode] 群内真实问题重放对比（进行中）：GLM 额度烧穿，Codex 4/4 完成

- 方法：从生产桶 analysis-jobs/ 提取群里 4 道真实题（含完整 12 轮会话上下文：股息率筛选 / A5 行业成交额变化率 / A 集合规则编码 / Ln 参数调优），conversation_id 换隔离值防污染生产会话，双运行时重放 + 任务 JSON 里的历史生产答案做三方对照。坑：Settings 漏设 google_cloud_project 会让 MCP 子进程秒崩（No project ID）。
- 结果：**Codex(DeepSeek) 4/4 成功**（63s / 2308s / 486s / 487s，最重的 A5 题 38 分钟也扛住）；**GLM 侧额度中途烧穿**——智谱返回「已达到 5 小时的使用上限，限额 2026-09-21 07:39:52 重置」，q2/q3 启动即撞、q1/q4 中途撞。结合上一轮 4 题合成基准：常规题 GLM 修复后可用，重负载题（A5 类）风险高 + 单题可烧大量套餐积分。
- 顺手合入（ac047931）：GLM 额度类错误翻译为群内人话提示（含恢复时间 + 建议切回 DeepSeek），909 passed。
- 补跑：明早额度恢复后用 ~/glm-replay-pending/（4 题 JSON + run_replay.py）重跑 GLM 侧出完整对照；worktree 需重建（origin/main + py3.14 venv + Settings 带 google_cloud_project）。

## [2026-09-20 · ZCode] 热修二（252595b4）：form 容器在旧飞书客户端渲染为升级占位条 → 改独立下拉

- 用户复验：1b9c39ca 后两张 form 仍不显示，截图实为灰色「请升级至最新版本客户端」占位条——**form 表单容器（input/select+提交按钮）需要较新版客户端**，与组件 tag 无关（连 main 既有「开始研究」输入框在用户客户端也从未显示过）。
- 修复：模型选择改为 **action 容器内独立 `select_static`**（v1 老组件、旧客户端可渲染、选择即回调无需提交按钮）：组件声明 `value={"action":"switch_model"}`，回调选中值在 `action.option`；`_extract_card_action_context` 增加 option 提取（8 元组），parse_card_action 优先 option、form_value 兜底。测试：独立下拉断言、option 回调、form 形态兼容、无选择报错；顺带密闭化 test_feishu 的 bootstrap 测试（monkeypatch GOOGLE_CLOUD_PROJECT=test-project + patch llm 控制器——本地 gcloud 环境波动曾致其依赖真实 project 解析）。
- 已推 main（`1b9c39ca..252595b4`），线上 APP_GIT_SHA=252595b4 已确认。待用户复验：旧客户端应直接显示下拉并选择生效；「开始研究」输入框需升级客户端才有（main 既有行为，非本次范围）。

## [2026-09-20 · ZCode] 热修（1b9c39ca）：模型下拉 tag 写错致飞书丢弃两张表单

- 线上事故：用户群内 @机器人 空消息，快捷菜单卡片缺模型下拉**且缺「开始研究」输入框**。根因：下拉组件 tag 写成 `select_menu`（不存在的 v1 组件名，正确为 `select_static`），飞书对含无效组件的卡片丢弃表单渲染——把原有 research_form 也连带弄丢。
- 修复：tag 改 `select_static`（属性结构同构：name/placeholder/options+form_submit 按钮）；测试断言 model_form 子组件只允许 {select_static, button} 锁死。教训：飞书卡片组件 tag 没有本地校验层，新增交互组件时应对照官方 v1 组件清单写测试断言。
- 已推 main 并确认线上 APP_GIT_SHA=1b9c39ca；待用户群内复验下拉与输入框恢复。

## [2026-09-20 · ZCode] 单位随数据返回（a61c2a48）：接口层透传 field_units

- 用户再进一步：模型层最好零特殊逻辑，单位应在**接口层返回时自带**，让所有模型被动可见。实现：`registry.py` 新增机器可读单一来源 `FIELD_UNIT_NOTES`（操作→字段→单位，含并行会话审计的 moneyflow_ind_ths/cnt_ths 的 net_amount/net_buy_amount 亿元语义）+ `field_unit_notes_for()`；`feishu_agent._result_payload`（所有数据集返回模型的统一出口，MCP/Codex 与 GLM 共用）按本次 columns 注入 `field_units`。GLM 提示词只剩通用纪律「读返回里的 field_units 并声明换算」，无任何字段事实。
- 测试（tests/test_catalog_units.py 与并行会话的 THS 断言取并集）：结构化表↔prose 一致性、payload 注入/省略、toolbox 端到端、提示词防回流（禁 "thousands of CNY"/"Tushare daily.amount"）。全量 907 passed。
- 注意：①rebase 撞上并行会话 601e81ae（THS 审计），测试文件冲突取并集解决；②**exact-range 家族顺序失败再次出现且纯净 origin/main 同样复现**（disclosure/dividend 变体）——疑似 601e81ae 引入的测试顺序影响，待专项；③从 /tmp worktree 推送会 tsc: command not found（账本旧坑重踩），从主检出推 sha:main 成功（`601e81ae..a61c2a48`）。

## [2026-09-20 · ZCode] 单位事实下沉共享目录（b2fdc326）：修正上一条的分层错误

- 用户指出：把「Tushare amount=千元」写进 GLM 提示词是特殊逻辑。判定正确——schema 事实属于数据目录层（两运行时共读），不属于任何运行时的提示词。
- 修正：`registry.py` 目录文档补单位说明（daily: amount 千元/vol 手；moneyflow/repurchase: amount 万元）；GLM 提示词单位条目泛化为「从操作目录文档读字段单位，报原单位+换算，量级 sanity check」。新增 tests/test_catalog_units.py 锁住两条不变量：①目录必须含单位文档；②运行时提示词不得内嵌 provider schema 事实（防回流）。
- 效果：Codex（MCP 工具箱同源目录）与 GLM 现在都能从目录读到单位；后续补其他操作的单位只改 registry 一处。全量 894 passed，已推 main（`b434b40a..b2fdc326`）。

## [2026-09-20 · ZCode] GLM vs DeepSeek(Codex) 四题基准对决 + 基准后加固（b434b40a）

- 对决设置：4 道题（茅台收盘价 / 宁德vs比亚迪日均成交额 / 全市场月日均成交额Top5+行业 / 上周涨幅王）× 两运行时（GLM 升级循环 vs 生产同款 deepseek-flash+Codex），另用直连 Tushare 取数做标准答案。
- 结果：**Q1 平手**（都对，GLM 24s 略快）；**Q2 平手偏 Codex**（数值都对、结论同，Codex 附 Excel 明细+口径更严谨）；**Q3 Codex 胜**——排名与行业两边全对，但 GLM 把成交额单位错 10 倍（Tushare amount=千元当成万元，234.7亿报成2347亿，且错误写进了口径说明）；**Q4 Codex 胜**——GLM 全市场重负载跑 1657s 后读超时失败，Codex 25s 正确识别「上周」歧义并给出带推荐项的澄清问题（标准答案 601091.SH +177.7%）。
- 加固（已推 main `e421295e..b434b40a`）：提示词加单位纪律（amount=千元/vol=手、报数先报原单位与换算、量级 sanity check）与歧义处理（周末问「上周」应澄清而非默选）；超时 300s→600s 且读超时重试一次；新增重试回归测试；全量 891 passed。
- 结论：常规研究 GLM 已可用且更快，重负载/严谨性仍落后——与「统一 Codex harness」决策一致，差距收敛靠模型侧协议跟进而非堆私有循环。基准结果文件在 /tmp/bench_results（重启即失，结论已录此处）。

## [2026-09-20 · ZCode] GLM 研究循环拉齐 Codex 编排（e421295e）：思考模式 + 60 轮 + 纠错纪律 + 日期锚定

- 背景：用户问「不能和 DeepSeek 拉齐吗？上层用 codex 去规划」。重探智谱编码端点 `/responses` 仍 404（Codex SDK 0.154 只认 Responses 协议且已删 chat），直连 Codex 依旧不通；改为在自己的 chat 工具循环里复刻编排能力。
- 升级（`glm_agent.py`）：① `thinking:{"type":"enabled"}`（glm-5.3 思考模式，工具间思考）+ max_tokens 16k + timeout 300s；② 工具轮次 24→60；③ system prompt 追加 `GLM_RECOVERY_INSTRUCTIONS`（工具失败不许放弃、改参/拆解重试、指标不可用换近替代并声明、异常数字交叉验证）；④ **修复真 bug：循环未注入当前日期，模型把「最近20个交易日」算成 2025-12**——现注入 Asia/Shanghai 当前日期锚（Codex 路径靠 SDK 环境自带时钟，chat 循环此前没有）；⑤ 历史 assistant 消息剥离 `reasoning_content` 防回传出错。
- 验证：真实冒烟（三银行 20 日成交额+波动率对比）——日期区间正确（2026-08-24~09-18）；期间沙箱临时不可用，模型自主降级到工具箱确定性聚合管道完成计算并声明口径替换（纠错纪律的直接实证）；全量 890 passed；已推 main（`656c06f5..e421295e`）。
- 遗留：GLM 轻量循环与 Codex 代理仍有差距（无线程级上下文压缩/多文件工作区管理）；若智谱上线 /responses 可直接切回 Codex harness（探测命令在本次会话记录里）；/tmp/glm-port3* 待清理。

## [2026-09-20 · ZCode] 研究查看页答案改为 Markdown 渲染（** 不再原样显示）

- 需求：用户发现 /research/ 链接「研究结论」里 `**加粗**` 星号原样显示。根因：`ResearchVisualizationPage` 用 `<div>{payload.answer}</div>` 纯文本渲染，agent 答案是 Markdown（加粗/标题/列表/GFM 表格）。
- 改动（分支 `codex/viewer-answer-markdown`，worktree /tmp/china-a-share-md）：新增 `frontend/src/researchMarkdown.tsx`——确定性 Markdown 子集渲染器（bold/标题/有序无序列表/GFM 表格/换行），纯 React 节点不走 innerHTML（无注入面）；前端保持零第三方运行时依赖（package.json 仅 react）。查看器页 answer 与 methodology 两处接入；styles.css 加 .research-markdown 系列样式（选择器写得比 `.research-viewer-summary div` 的 pre-wrap 更具体）。
- 验证：e2e 用真实答案形态（加粗标题/带加粗的列表/表格）断言 strong 可见、页面无字面 `**`、markdown 表渲染——通过；全量 e2e 27 过 + 既有 2 失败不变；后端 889 passed / 129 skipped；已推 main（`d345e69d..656c06f5`），scheduler 自动部署。
- 遗留：解析器只覆盖模型实际产出的子集（无嵌套列表/链接/图片），遇到新形态按需扩展同一模块；两个 main 既有 e2e 失败仍待专项。

## [2026-09-20 · ZCode] 修复研究查看页日期被渲染成千分位数字（20,260,917）

- 需求：用户报告 /research/ 链接里日期显示为 `20,260,917`（实为 20260917）。根因：main 的 `ResearchVisualizationPage.tsx` 只识别以 date/日期/时间/报告期/收盘日/交易日 **结尾**的列，agent 生成的「最近大涨日」等列名不匹配，整数日期走了 `toLocaleString`。
- 改动（分支 `codex/viewer-date-format`，worktree /tmp/china-a-share-date-fix）：① 新增共享模块 `frontend/src/calendarDates.ts`——`isDateLikeColumn`（列名含 date/day/time/时/日/期 任一 token）+ `compactCalendarDate`（8 位有效日历日期→YYYY-MM-DD，值校验兜底，非日期值不改写）；② 研究查看器页与 App.tsx 主表格数字/字符串分支都先查日期再走数字格式化；③ `feishu_agent.py` 新增 `compact_calendar_date_text`，Excel 导出日期列写 YYYY-MM-DD。
- 验证：扩展 e2e（最近大涨日 20260917 → 显示 2026-09-17、不再出现 20,260,917）通过；全量 e2e 27 过 + **2 个 main 既有失败**（反馈弹窗双「发送」按钮 strict violation、discovery 因子清单断言过期——纯净 main 同样失败，与本次无关，待专项）；后端全量 887 passed / 129 skipped；`npm run build` 通过。
- 注意：main 的 `make check` 后端测试仍被 operator 注释（自跑全量代替）；本地 e2e 注意 5173 残留旧 dev server（已清理 china-a-share-rv 遗留进程）；worktree 里 pnpm 会因链接 node_modules 报 purge 错，用 `node …/node_modules/vite/bin/vite.js` 直接起服务绕过。

## [2026-09-20 · ZCode] 模型选择重构合入 main（e4cd5216）：注册表驱动 + 卡片单一下拉

- 用户反馈（线上截图）：①「研究模型」行看不出静态还是动态；②两个切换按钮应合并为一个下拉选择；③要为第三个模型留扩展性。
- 重构：`llm_preference.py` 引入 `ChatModelOption` 注册表（provider/label/note/required_settings_field）——下拉选项、文本命令校验、状态回复、可用性判断全部由注册表派生，`ALLOWED_PROVIDERS` 亦然；**加第三个模型＝注册表加一项 + bootstrap 加运行时映射**（有专门测试 `test_registry_extension_adds_a_third_model_to_every_surface` 证明）。卡片上两按钮替换为一个 form：`select_menu`（当前项 ✅ 前缀）+「切换模型」form_submit 按钮，走与 research_form 相同的已验证表单回调路径（`form_value["model"]`）；状态行改「**当前研究模型**」并在下拉里标当前。顺带修了 `parse_card_action` 里 `str(None)→"None"` 的既有隐患（submit_research 同款）。
- 验证：56 例目标测试 + py3.14 venv 全量 888 passed（exact-date-range 偶发本轮未复现，维持既有 flaky 定性）；已推 main（`2c215ec6..e4cd5216`），等 scheduler 自动部署后群里实测下拉。
- 遗留：bootstrap 的运行时映射仍是 deepseek/glm 二元（注册表第三项需配套映射才真正生效，已在代码注释与测试中注明）；/tmp/glm-port2* 待清理。

## [2026-09-20 · ZCode] 数字显示统一规则：默认两位小数、整数不补零

- 需求：用户反馈产出的数字小数位不统一——无特殊要求时自动保留两位小数；无数值小数部分则直接显示整数。要求"所有地方都这样"，并要求 AI 工具长期记住。
- 改动：`feishu_agent.py` 的 `_agent_system_prompt` 追加统一数字格式规则（answers/tables/reports 全部适用，用户或指标另有精度要求时除外）；新增 `test_agent_system_prompt_requires_uniform_number_precision` 回归锁住该规则必须在提示词中；偏好已写入 ZCode 持久记忆。
- 说明：答案文本由 LLM 生成，仓库无确定性数字格式化函数，提示词是此不变式的最小正确边界；本次只提交 `feishu_agent.py` + `test_feishu_agent.py`（工作区其余未提交改动与他人遗留无关，未纳入）。

## [2026-09-20 · ZCode] 群聊模型切换合入 main（2c215ec6）：快捷菜单卡片按钮 + GLM 轻量运行时

- 需求：默认仍 DeepSeek；@机器人空消息弹出的快捷菜单卡片上给出模型列表一键切换；做完合入 main。因账本裁定不整体合并旧分支，此功能**移植到基于最新 main 的新分支** `codex/glm-chat-model-switch` 实现并已推 main（`a5bb2e75..2c215ec6`，FF 合入，hook 门禁通过；生产由 reconcile scheduler 自动部署）。
- 实现：① `llm_preference.py`——偏好持久化为 bucket 内 JSON，webhook 写 / worker 读，`resolve_active_provider`＝GLM 偏好仅在 ZAI_API_KEY 存在时生效，默认永远 DeepSeek；② `glm_agent.py` 新 `GlmFeishuAgentRuntime`——OpenAI chat-completions 工具循环，复用 codex_agent 的提示词/产物/可视化与同款 ResearchToolbox+RemotePythonSandbox。**背景：Codex SDK 0.154 只支持 Responses API 且已删除 wire_api="chat"，智谱无 /responses（实测 404 + 社区 issue），GLM 无法直连 Codex 代理**，故 GLM 模式走轻量循环（与 DeepSeek 完整 Codex 代理存在能力差异，切换回复中已注明）；③ feishu.py——`切换模型 glm|deepseek`/`当前模型` 文本命令 + 快捷菜单卡片加当前模型行和「🤖 切到 GLM / DeepSeek」按钮（复用 card_action→命令翻译机制）；④ bootstrap/worker 接线 `llm_preference`。
- 验证：真实冒烟——GlmFeishuAgentRuntime 用真 GLM(glm-5.3@编码端点)+真 Tushare 自主调工具返回正确收盘价 11.70；py3.14 新 venv 全量 pytest 886 passed（main 的 make check 后端测试被 operator 注释，故自建完整验证）；修了 test_worker 对 runtime 工厂签名的 patch。
- 注意事项：① main 上 `tests/test_components.py` 的 exact-date-range 类测试存在**概率性顺序失败**（dividend/disclosure 各偶发一次，同代码可全绿，与本次改动无顺序交集）——待后续专项排查；② 期间误弹出过他人 stash「On main: preserve pre-existing unlock ranking work」到 /tmp/glm-port 已 reset 还原，**stash 条目原样保留未动**；③ 旧特性分支上的 `8b89577e`/`f30b1bb4`（LLM_PROVIDER 开关 + 文本命令）与本次 main 实现是两套，特性分支那套未部署。
- /tmp/glm-port worktree 与 /tmp/glm-port-venv（py3.14，含 openai-codex）已清理；如需复跑 main 全量测试需重建 py≥3.10 venv。

## [2026-09-20 · ZCode] 合并「策略列表」与「运行规则」入口（PR #13，已合入 main）

- 需求：用户在飞书快捷菜单卡截图指出「策略列表」和「运行规则」应合并——此前 4 个入口（快捷菜单、策略菜单、列表卡底部、保存卡）都指向同一个 `strategy_run_all` 盲跑动作。
- 改动（基于最新 main 的分支 `codex/merge-strategy-run-entry`，worktree `/tmp/china-a-share-strategy-run-merge`）：两张菜单卡只留「新建策略/策略列表」；手动运行收进策略列表（每行「运行/启停/删除」+ 底部「运行全部」）；保存卡「立即运行」只跑刚保存的那条；`StrategyScanner.run_manual_preview` 增可选 `strategy_id` 单策略预览；协调器新增 `strategy_run` 动作（校验归属/启用）。文本命令「运行规则」保留不变。
- 中途 main 进了 nl-rule-compiler，PR 冲突已解（保留编译器测试 + 新运行测试，删被替代的旧菜单断言）。
- 验证：新增 7 例回归；合并后全量后端 867 passed / 129 skipped，`make check` 通过，pre-push 钩子门禁通过；PR #13 merge commit `c67d2662`。部署由 reconcile 调度器自动接管（本条目写入时最新 revision 00264 尚为合并前版本，等下一轮 10 分钟 reconcile）。

## [2026-09-20 · ZCode] 群聊内切换模型：切换模型 glm / 切换模型 deepseek

- 需求：用户问「怎么在群里切换模型」——此前 LLM_PROVIDER 只是环境变量开关，改了要重启进程。现新增飞书命令：`切换模型 glm`、`切换模型 deepseek`、`当前模型`（查询），下一次研究任务即生效。
- 机制：新模块 `llm_preference.py`——偏好持久化为应用 bucket 里单个小 JSON（webhook 进程写、worker 进程读，跨进程必须落 GCS）；`resolve_active_provider` 规则＝持久偏好仅在对应 API key 存在时生效，否则回落 LLM_PROVIDER 默认。`create_analysis_service` / `create_feishu_agent_runtime` 增加可选 `llm_preference` 参数，worker 统一传入。切 glm 但缺 ZAI_API_KEY 会被明确拒绝；「模型轮动…」等普通研究提问不会被命令误吞（有回归测试）。
- 验证与位置：新增 tests/test_llm_preference.py 17 例；提交 `f30b1bb4` 在 `codex/technical-pattern-studies`（隔离 worktree 全量门禁 750 passed / 0 failed；推送竞态由并行会话的 `1bd288cb` 顺带带上远端）。
- 注意：与 `8b89577e`（LLM_PROVIDER 开关）一样**只在本地/本分支生效**，main 未合入（见下一条目：不要整体合并，需按行为移植到基于最新 main 的新分支）；线上要用需先移植。生产两把 key secret 均已挂载，移植后即可用。

## [2026-09-20 · ZCode] 落地方案 B：新建 keep-alive Scheduler 任务消除冷启动 ❌

- 实施：用户拍板方案 B。已创建 Cloud Scheduler 任务 `china-a-share-keep-alive`（asia-east2，`*/5 * * * *` UTC，OIDC 以 `china-a-share-scheduler@` GET `/api/health`），ENABLED；手动触发 200/~6ms，07:05 定时触发也 200/4ms。项目现共 3 个 Scheduler 任务＝免费额度上限，第 4 个将开始收费；新增开销 <$0.10/月。IAM/生命周期/Cloud Run 配置零变更。
- 记账：`docs/gcp-resources.md` 已更新（新任务表格 + Last verified 09-20 + 变更日志行）。权威版本已推 main（`2673adff`，doc-only，不触发部署），同内容 cherry-pick 到本分支（`1bd288cb`，门禁 750 passed）。
- 踩坑备忘：pre-push 钩子从「推送发起目录」符号链接 .venv 和 frontend/node_modules——从 /tmp 临时 worktree 推送会因缺 node_modules 而 `tsc: command not found` 失败；应回到主检出用 `git push origin <sha>:main` 推送。期间 main 被并行会话推进两次（nl-rule-compiler、chat 命令），推送前记得 fetch+rebase。
- 效果预期：实例常驻后，卡片回调不再撞 3s 冷启动线，❌ toast 应消失；若仍偶发，再查 >3s 日志。

## [2026-09-20 · ZCode] 线上「❌ 操作已提交」真因＝Cloud Run 冷启动；本分支修复与 main 无关

- 追查：用户问修复是否已合入 main。事实：① 修复提交 `6a426bd3` 只在 `codex/technical-pattern-studies`，未合入；② main 上策略交互层是移植后的另一套实现（`discovery/strategy_interaction.py`，走 `card.action.trigger` 事件 + BackgroundTasks + 标准 toast 信封，文案「操作已提交」写死于 api.py:339），本分支的 `strategy/` 目录从未部署——**合并本分支对线上 ❌ 无效**，且账本早已裁定不要整体合并（现领先 13 / 落后 70）。
- 线上真因（gcloud 日志实锤）：服务 min instances=0，空闲缩容到零；卡片点击回调触发冷启动，实测 3.2–6.2s 才返回 200，超飞书 3s 卡片回调期限 → 客户端弹 ❌。热路径仅 ~4ms，main 代码本身没问题。2026-09-19~20 全天 23 次 >3s 回调（多为每次空闲后首击）。动作本身都成功执行，❌ 仅是提示难看。
- 待用户拍板（涉及云资源/费用）：A) Cloud Run min-instances 0→1（常驻成本约数十美元/月）；B) Cloud Scheduler 定时轻量 ping 保活（近乎零成本，新增一个资源）；C) 接受偶发 ❌。
- 本分支遗留：`strategy/` 旧副本与 main 的 `discovery/` 实现重复，建议按快照 next plan 决定废弃或继续移植；`6a426bd3` 的信封构造器/后台化写法可作移植参考。

## [2026-09-19 · ZCode] 新增 LLM_PROVIDER=glm 可选切换（默认仍 DeepSeek）

- 需求：用户想用 GLM Coding Plan 自带额度跑本项目，且明确「要可选、不强行换」。实现为纯开关：`LLM_PROVIDER` 不设或 `=deepseek` 时行为与原先逐字节一致；`=glm` 时 planner / 飞书 agent / UI 反馈三条 LLM 路径走智谱编码套餐端点 `open.bigmodel.cn/api/coding/paas/v4`（烧套餐 5 小时/周额度），`GLM_API_URL` 可覆盖为按量端点；模型映射 glm-5.3-flash → glm-5.3（对应 deepseek-flash → v4-pro 的末位升级）。
- 实现：`planners/deepseek.py` 参数化端点/模型/provider（引擎本身 provider-neutral），新增薄封装 `planners/glm.py`；`feishu_agent.py`、`feedback.py` 构造器加可选参数；`bootstrap.py` 统一 `_glm_chat_config()` 装配；截图视觉仍走按量端点不变。新测试 tests/test_llm_provider_switch.py 11 例。
- 验证：编码端点 curl 200；GlmQueryPlanner 真跑一次产出合法 supported 计划；agent 工具循环真调 Tushare 返回正确收盘价。提交 8b89577e 已随 Codex 的 6a426bd3 一并推到 origin（隔离 worktree 全量 733 passed / 0 failed）。
- 遗留：工作区仍有早前会话的确定性规划修改（workflow.py / capabilities.py / 两个测试文件 / live_cases.json）未提交未过全门，见下一条目；策略测试历史上依赖本地 .env，已在 6a426bd3 中修复密闭性。

## [2026-09-19 · ZCode] 修复策略卡片按钮点击后 toast「❌ 操作已提交」

- 现象：用户在飞书点策略菜单卡片按钮，客户端 toast「❌ 操作已提交」。根因有二：① 回调应答格式错误——飞书要求 `{"toast":…,"card":{"type":"raw","data":…}}` 信封，我们直接把 v1 卡片 JSON 放顶层、错误路径返回 `{"content":…}`，飞书一律判失败；② 「运行规则/立即预览运行」在回调线程里同步跑全市场 120 天扫描，远超飞书 3 秒回调时限。
- 修复：`strategy/feishu_interaction.py` 统一 `_callback_response()` 信封构造器（全部 handler 走它）；预览扫描改投单线程后台 executor，回调秒回 toast+「预览已提交」卡片，结果卡片由 scanner 照旧独立推送；`feishu.py`/`api.py` 兜底应答同步改 toast 格式。
- 验证：新增信封不变量与「回调不阻塞、扫描在后台完成」回归测试（tests/test_feishu_strategy.py），`make check` 全量 739 passed / 124 skipped；提交仅含本任务 5 个文件（feishu_interaction.py、feishu.py、api.py、两个测试文件）。
- 遗留：本条目连同上一条「账本启用」及在途规划改动仍在工作区未提交；后续提交时一并带走。注意部署回调地址需在飞书开放平台已配置「消息卡片请求网址」指向 `/api/integrations/feishu/events/interactive`。

## [2026-09-19 · ZCode] 账本启用

- 背景：今日因 Codex credit 耗尽做过一次手动 Codex→ZCode 交接；为了让各工具互知对方在本仓库做过什么，自本条起启用统一账本，协议已写入 `AGENTS.md`。
- 变更：`AGENTS.md` 新增「Cross-tool handoff ledger」节；本文件转为账本格式，下方原有「Zcode Handoff」快照保留为最近一次详细状态。
- 状态要点（详见下方快照）：`codex/technical-pattern-studies` 分支领先 `main` 10 个提交、落后 61 个，**不要整体合并**，按快照把想要的行为移植到基于最新 `main` 的新分支；5 个 tracked 文件有未提交的确定性规划修改（完整 pre-push 门与 live 回归未跑）；`fix_test*.py` 等未跟踪脚本待按快照处理。
- 下一步：从下方快照的「Recommended next plan」一节开始。

---

# Zcode Handoff

Snapshot date: 2026-09-19 (America/Los_Angeles).

This document records the non-obvious state that cannot be recovered safely from
the repository structure alone. It intentionally contains no credential values.

## Executive summary

There are two independent bodies of work in this checkout:

1. Ten commits on `codex/technical-pattern-studies` add U.S. market-data routing,
   model-routing changes, and a new Feishu strategy scanner/interactive workflow.
   The strategy feature is not deployed and should not be treated as production
   ready without further correction.
2. Five tracked files contain uncommitted deterministic-planning fixes for unlock
   rankings and earnings-forecast growth rankings. These changes are a work in
   progress. Targeted unit tests pass, but the complete pre-push gate and required
   live regressions have not been run.

Do not merge this branch wholesale into `main`. At this snapshot the branch is 61
commits behind `main` and 10 commits ahead, and a read-only merge-tree inspection
shows many overlapping/conflicting paths. Port the wanted behavior onto a fresh
branch from current `main` in small, reviewed units.

## 1. Work in progress

### Uncommitted deterministic analysis work

The tracked working-tree changes are:

- `live_cases.json`
- `src/china_a_share/application/workflow.py`
- `src/china_a_share/capabilities.py`
- `tests/test_components.py`
- `tests/test_execution_answer_fields.py`

Their intended behavior is:

- Compile market-wide unlock rankings at security grain, aggregating all unlock
  tranches per security before sorting and limiting.
- Make the previously timeless live prompt explicit:
  `列出2026年解禁股数占总股本比例最高的10只股票`.
- Compile H1 earnings-forecast growth rankings deterministically, retain only the
  latest disclosure per company, rank the positive `p_change_max` bound, and join
  company names from `stock_basic`.
- Audit bounded `forecast` reads as exact `ann_date` fan-out rather than treating
  them as one provider range query.
- Avoid misclassifying audited date-fan-out queries as security-fan-out templates.
- Recognize a complete weekday membership constraint as covered by the enclosing
  native date range while rejecting sparse membership.

Current diff size is 544 additions and 14 deletions across those five files.

Status: **WIP, not release-ready**.

Evidence completed on this snapshot:

- `git diff --check`: passed.
- `pytest -q tests/test_components.py tests/test_execution_answer_fields.py`:
  256 passed.

Still required before treating the work as complete:

- Reconcile the changes onto current `main`; the present branch is stale.
- Review why unlock-ranking construction exists in both `_compile_intent` and
  `_compile_known_unlock_ranking`; keep both only if the two entry paths genuinely
  require duplicate construction.
- Recheck the weekday-membership equivalence against the intended constraint
  semantics and exchange holidays.
- Run the complete `make pre-push` gate on the exact candidate commit.
- Run the exact live cases for `share_unlock_boundaries-03` and
  `earnings_guidance-02` against the configured real model and provider, as required
  by `AGENTS.md` for production-reported prompts. No current live run satisfies this.

### Duplicate safety stash

`stash@{0}` is named `On main: preserve pre-existing unlock ranking work` and was
created on 2026-08-16. It contains a 306-line subset of the current working changes:
the unlock-ranking compiler, the weekday-membership validator, related tests, and
the dated live-case prompt.

The working tree includes that subset plus later forecast-ranking and fan-out work.
Do not pop the stash into this checkout or a fresh `main`; doing so would duplicate
changes and create conflicts. Keep it only as a safety backup until the working
changes have been preserved and reviewed, then it is safe to drop deliberately.

### Committed strategy branch work

The six strategy-specific commits are `df0f7f82` through `61d1feb2`. They add:

- strategy models and Cloud Storage persistence;
- QFQ data loading and rule evaluation;
- Feishu strategy cards and draft interactions;
- a scheduled daily-scan HTTP endpoint;
- notification deduplication and retry-oriented state recording;
- a Cloud Scheduler resource entry.

Targeted tests passed on this snapshot:

- `pytest -q tests/test_strategy_engine.py tests/test_strategy_integration.py
  tests/test_strategy_api_and_states.py tests/test_feishu_strategy.py`: 14 passed.

These tests do not establish production readiness. Known correctness and security
problems are listed below.

## 2. Untracked scripts

The following files are disposable repair/smoke scripts, not product assets:

- `fix_test.py` and `fix_test2.py` mechanically repaired test arrays in
  `tests/test_strategy_engine.py`.
- `fix_test3.py` through `fix_test5.py` iteratively patched the strategy API/state
  tests.
- `fix_test6.py` through `fix_test10.py` iteratively added and repaired Feishu
  trigger, `@all` fallback, and at-least-once delivery tests. Some intermediate
  scripts contain invalid or abandoned code; the corrected test implementations
  are already present in tracked test files.
- `test_tushare_cal.py` and `test_tushare_daily.py` are ad hoc live smoke probes.
  They print provider output and are not pytest-quality tests. Their app bootstrap
  path also requires the configured GCS cache and is unsuitable as a standalone
  token check.

None contains unique behavior that must be retained. After preserving this handoff,
all 12 can be deleted rather than committed. A direct read-only Tushare probe was
run separately and is recorded under External state.

## 3. Recommended next plan

1. Start from current `main`, not from a merge of this branch.
2. Preserve the five tracked WIP files as a dedicated safety commit or patch before
   changing branches. Do not include the 12 temporary scripts.
3. Port and finish the deterministic unlock/forecast work first. It is narrower,
   already has focused unit coverage, and is independent of the strategy feature.
4. Run the exact two live cases, then `make pre-push`, before pushing that unit.
5. Reassess the strategy product contract and fix the P0/P1 issues below before
   porting only the strategy-specific code onto a fresh branch from `main`.
6. Keep current `main`'s mature Feishu research agent, push-trigger deployment,
   sandbox, query-shape metadata, and seven-hour worker configuration. Do not choose
   stale branch versions during conflict resolution.
7. Reconcile `docs/gcp-resources.md` only after the strategy resource decision. The
   `main` version is much newer than this branch's version but still omits the paused
   strategy Scheduler and has a stale top-level latest-revision row.

The old `.loop/backlog.md`, `.loop/handoff.md`, and README roadmap describe July-era
work and are not the current milestone. No additional reliable oral product decision
or hidden user commitment is available in the current Codex conversation context.

Recommended branch decision: let Zcode first preserve the five-file analysis WIP,
then create fresh branches from current `main` for (a) the analysis fix and (b) any
strategy work. Do not continue development or merge from
`codex/technical-pattern-studies` directly.

## 4. Known issues and failed assumptions

### Strategy feature: P0

- The daily-scan endpoint accepts any `Authorization: Bearer ...` value after the
  administrator-token check fails. It does not validate the Google OIDC token,
  issuer, audience, or scheduler identity. Because the Cloud Run service is public,
  this endpoint is externally triggerable by an arbitrary bearer string.
- `StrategyScanner.run_daily_scan` catches a market-data loading exception and
  returns normally. The API consequently returns success and Cloud Scheduler cannot
  retry that failure, despite commit messages claiming exact failure propagation.
- QFQ adjustment factors use dataframe-wide `ffill()` after sorting by security and
  date. Missing values can cross a security boundary; fill must be grouped by
  `ts_code` or rejected.

### Strategy feature: P1

- Rule evaluation raises on an individual security with insufficient history. A
  newly listed security can therefore fail an entire full-market strategy instead
  of being reported as ineligible.
- The Cloud Storage deduplication path is a non-atomic exists-then-write sequence.
  Concurrent invocations can both send. A crash after send and before mark can also
  duplicate delivery. This is at-least-once behavior, not strict exactly-once
  notification.
- Interactive creation is only a partial state machine. The card asks the user to
  reply with strategy details, but normal message routing does not complete those
  draft fields; save fills a preset when data is missing.
- Result rows use the security code as `stock_name`; no `stock_basic` name join is
  implemented.
- The full-market synchronous scan has not been load-tested against the 300-second
  web request timeout. The branch commit message's performance claim is not backed
  by a recorded benchmark.
- Strategy code is absent from current `main` and from the deployed production
  revision. Existing passing unit tests exercise only the stale feature branch.

### Branch integration

- The branch diverged before substantial Feishu agent, sandbox, U.S. provider, and
  deployment work landed on `main`.
- Read-only `git merge-tree` inspection reports many changed-in-both and
  added-in-both paths, including `Makefile`, `docs/gcp-resources.md`, provider files,
  Feishu code, and tests.
- The first four branch-only commits semantically overlap later `main` work but are
  not patch-identical. Prefer current `main` behavior and port only missing strategy
  functionality.

### Earlier smoke-test failure

Running the two ad hoc Tushare scripts inside the restricted sandbox first failed
while trying to refresh Google credentials for the persistent GCS cache. This was an
environment/network limitation, not evidence of an invalid Tushare token. A direct
Tushare probe outside that cache path succeeded afterward.

## 5. External state

### Local credentials

The local `.env` contains these keys: `TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`,
`ZAI_API_KEY`, `TUSHARE_CACHE_BUCKET`, `GOOGLE_CLOUD_PROJECT`, and
`GOOGLE_CLOUD_LOCATION`. It does not contain local Feishu credentials.

The machine has an active `gcloud` user login for project `china-a-share-lab`.
No separate Codex-only application secret was found. Codex process variables contain
no extra Tushare, model-provider, Feishu, Massive, Finnhub, or GitHub secret.

### Tushare

The local token was valid at this snapshot for direct read-only calls:

- `trade_cal`: 6 rows for 2023-01-01 through 2023-01-10 open sessions;
- `daily`: 5,065 rows for 2023-01-10;
- `adj_factor`: 5,156 rows for 2023-01-10.

The exact account points, per-interface quota, rate limit, and any commercial expiry
were not exposed by the client and remain unverified. Successful calls prove current
token validity and permission for only those tested endpoints.

### Feishu

Production Cloud Run binds `FEISHU_APP_ID` and the three Feishu application secrets
from Secret Manager. Recent Cloud Run request logs show repeated 200 responses from
`/api/integrations/feishu/events` during the last 72 hours, with no matching
`feishu_research_turn_failed`, `feishu_interactive_card_failed`, or
`strategy_daily_scan_failed` event found in the same query window.

This proves that the existing production research callback is active. It does not
prove the new strategy UI works: strategy code is not deployed, no strategy callback
was production-tested, and the strategy Scheduler is paused.

### Google Cloud live state

Read-only `gcloud` inspection found:

- Project: `china-a-share-lab`, region `asia-east2`.
- Production service: `china-a-share-lab`, latest ready revision
  `china-a-share-lab-00260-c8w`, 100% traffic.
- Deployed application source: `main@f8efb2fb140abcf4c8cca7c2d2db9c8a890c45be`.
  Current `main` is one later documentation-only commit, so the deployed application
  code is current for non-documentation changes.
- Runtime: 1 vCPU, 1 GiB, concurrency 4, 300-second request timeout, CPU throttling
  disabled, zero minimum instances, and service-level maximum scale 1.
- Worker: `china-a-share-analysis-worker`, 1 vCPU, 4 GiB, one task, one retry,
  25,200-second timeout, synchronized to the same deployed source SHA.
- Private research sandbox: live as `china-a-share-research-sandbox`; it is documented
  on current `main` but absent from this stale branch's inventory.
- `china-a-share-deploy-main-push` is enabled for pushes to `main`.
- `china-a-share-reconcile-main` is paused intentionally after push-trigger
  deployment replaced polling.
- `china-a-share-strategy-daily-scan` exists with OIDC, weekday 16:30
  Asia/Shanghai scheduling, and the intended strategy endpoint, but is **PAUSED**.
- The project also contains unrelated `video-factory` resources; do not treat them as
  part of this application's inventory.

Inventory discrepancies:

- This branch's `docs/gcp-resources.md` is substantially stale and incorrectly marks
  both Scheduler jobs as enabled.
- Current `main` documents the research sandbox, seven-hour worker, push trigger,
  and paused reconciliation Scheduler, but its top-level latest revision still says
  `00257-scz`; its change log correctly records `00260-c8w`.
- Current `main` does not include the newly created, paused strategy Scheduler.

No cloud resource was mutated during this handoff audit.

## Safe cleanup after preservation

After the five-file WIP is safely committed or patched and this handoff is retained:

- delete the 12 untracked repair/smoke scripts;
- drop `stash@{0}` only after confirming the preserved WIP contains the desired
  unlock changes;
- leave both Scheduler jobs paused until the strategy endpoint is securely ported,
  deployed, and verified.
