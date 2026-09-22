# 交接账本（Cross-tool Handoff Ledger）

> 本文件是 ZCode / Codex / Claude / Gemini 等工具共用的交接账本。协议见 `AGENTS.md`
> 的「Cross-tool handoff ledger」一节：开工先读最新条目；收尾在 DECISIONS 区下方、
> 最新条目上方追加 `## [日期 · 工具] 标题`（做了什么 / 关键决定 / 下一步）；
> 条目超 8 条压缩最旧；永不写入密钥。

## DECISIONS（架构决策账）

<!-- 固定区：架构级决策（模块边界/数据模型/产品拆分/协作规则）对齐后在此登记。
     注：2026-09-22 前 main 无本文件；历史决策条目暂存于分叉分支
     codex/technical-pattern-studies 的 HANDOFF.md，随小单元移植逐步回填。 -->

## [2026-09-22 · ZCode] 修复引用回复分裂会话：菜单序号跟进丢失上下文

- 群内事故：机器人让用户「回复序号 2」，用户引用回复机器人消息后答「没有历史上下文」。根因：conversation_id 拼入 `thread_id or root_id or "root"`，普通群的引用回复带 root_id 落入新会话桶，历史全丢（feishu.py 解析层，首版遗留）。
- 修复（b2ac1e03）：会话身份 = `tenant:chat:topic:sender`，topic 仅取话题群平台级 thread_id；root_id（引用拓扑）不再参与。话题群隔离保留；同群同用户的普通群/单聊回复与直发统一桶，显式隔离交给命名会话。
- 测试：引用回复身份连续性（双向）+ 话题群仍分区 + 端到端「菜单答复→引用回复 2」保留历史（旧代码实测失败）；live 用例「研究对象为A股所有股票→2」真 DeepSeek+沙箱 22s 通过。全量 936 passed；pre-push 门禁过，已推 main。
- 遗留：生产复验等 Cloud Scheduler 自动 reconcile 后用户群内复测；旧桶历史（含 root_id 键）一次性丢弃，未迁移。
