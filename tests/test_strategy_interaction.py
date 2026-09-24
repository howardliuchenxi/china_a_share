"""Tests for the Feishu strategy interaction layer and its bot integration."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from china_a_share.core.contracts import (
    AnalysisTask,
    AnalysisTaskStatus,
    AnalysisTaskSubmission,
    QueryPlan,
)
from china_a_share.discovery.strategy_interaction import (
    RuleSpecError,
    StrategyInteractionCoordinator,
    build_strategy_menu_card,
    parse_rule_spec,
)
from china_a_share.discovery.strategy_models import (
    CompiledStrategyRule,
    CumulativeReturnRule,
    DraftState,
    DrawdownRule,
    FirstBullishMARule,
    LimitUpRule,
    SignalDirection,
)
from china_a_share.discovery.strategy_store import MemoryStrategyStore
from china_a_share.feishu import (
    FeishuEventError,
    FeishuMessageEvent,
    FeishuResearchBot,
    MemoryConversationStore,
    build_feishu_quick_menu_card,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 9, 18, 15, 30, tzinfo=SHANGHAI)
USER_RULES_TEXT = (
    "回撤 窗口=60 阈值=30%；涨幅 窗口=10 下限=-10% 上限=10%；金叉 快线=5 慢线=10"
)


class FakeSender:
    def __init__(self):
        self.replies = []
        self.cards = []

    def reply(self, message_id, text):
        self.replies.append((message_id, text))
        return f"reply-{len(self.replies)}"

    def update(self, message_id, text):
        raise AssertionError("Unexpected message update")

    def reply_card(self, message_id, card):
        self.cards.append((message_id, card))
        return f"card-{len(self.cards)}"


class FakeTaskCoordinator:
    def __init__(self):
        self.requests = []

    def submit(self, request, *, task_id=None):
        self.requests.append(request)
        now = datetime(2026, 9, 18, tzinfo=SHANGHAI)
        task_id = task_id or f"{len(self.requests):032x}"
        self.tasks = getattr(self, "tasks", {})
        self.tasks[task_id] = AnalysisTask(
            task_id=task_id,
            status=AnalysisTaskStatus.QUEUED,
            request=request,
            created_at=now,
            updated_at=now,
        )
        return AnalysisTaskSubmission(
            task_id=task_id,
            status=AnalysisTaskStatus.QUEUED,
            status_url=f"/api/analysis/tasks/{task_id}",
        )

    def get(self, task_id):
        return None


class FakeScanner:
    def __init__(self, failing=False):
        self.calls = []
        self.failing = failing

    def run_manual_preview(self, owner_open_id, request_id, strategy_id=None):
        self.calls.append((owner_open_id, request_id, strategy_id))
        if self.failing:
            raise RuntimeError("strategy preview failed")

    def run_trial(self, strategy, start_date, end_date, request_id):
        self.calls.append(
            ("trial", strategy.id, start_date, end_date, request_id)
        )
        if self.failing:
            raise RuntimeError("strategy trial failed")


class FakeCompiler:
    def __init__(self, result=None, raising=False):
        self.result = result
        self.raising = raising
        self.calls = []
        self.conversations = []

    def compile(self, text, conversation=None):
        self.calls.append(text)
        self.conversations.append(conversation or [])
        if self.raising:
            raise RuntimeError("compiler exploded")
        return self.result


def build_interaction(scanner=None, compiler=None):
    store = MemoryStrategyStore()
    coordinator = StrategyInteractionCoordinator(
        store,
        scanner,
        clock=lambda: FIXED_NOW,
        compiler=compiler,
    )
    return coordinator, store


def compiled_rule(source_text="多阶段自然语言规则"):
    plan = QueryPlan.model_validate(
        {
            "interpretation": "Execute the complete multi-stage screening rule.",
            "queries": [
                {
                    "query_id": "result",
                    "operation": "daily",
                    "params": {"start_date": "20260901", "end_date": "20260922"},
                    "fields": ["ts_code", "trade_date", "close"],
                    "purpose": "Load the source rows.",
                }
            ],
            "answer_contract": {
                "result_query_id": "result",
                "result_kind": "table",
                "outputs": [
                    {"field": "ts_code", "description": "Security code."}
                ],
            },
        }
    )
    return CompiledStrategyRule.create(
        source_text=source_text,
        plan=plan,
        anchor_date="20260922",
    )


def seed_draft_at_rules_step(coordinator, store, owner="ou_a", name="策略甲"):
    coordinator.handle_message(owner, "oc_chat", f"新建策略 {name}")
    draft = store.list_drafts(owner)[0]
    coordinator.handle_card_action(
        owner,
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )
    return store.get_draft(draft.draft_id, owner)


def card_text(card):
    return str(card)


def button_values(card):
    values = []
    for element in card.get("elements", []):
        if element.get("tag") == "action":
            values.extend(button.get("value", {}) for button in element["actions"])
    return values


def save_strategy_via_draft(coordinator, store, owner, chat, name):
    """Walk the guided draft flow once and return the saved-confirmation card."""
    coordinator.handle_message(owner, chat, f"新建策略 {name}")
    draft = store.list_drafts(owner)[0]
    coordinator.handle_card_action(
        owner,
        chat,
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )
    coordinator.handle_message(owner, chat, f"规则 {USER_RULES_TEXT}")
    return coordinator.handle_card_action(
        owner, chat, "strategy_draft_save", {"draft_id": draft.draft_id}
    )


def test_parse_rule_spec_supports_user_preset_rules():
    rules = parse_rule_spec(USER_RULES_TEXT)
    assert [type(rule) for rule in rules] == [
        DrawdownRule,
        CumulativeReturnRule,
        FirstBullishMARule,
    ]
    assert rules[0].window == 60
    assert rules[0].threshold == pytest.approx(0.30)
    assert rules[1].window == 10
    assert rules[1].min_return == pytest.approx(-0.10)
    assert rules[1].max_return == pytest.approx(0.10)
    assert rules[2].fast_window == 5
    assert rules[2].slow_window == 10


def test_parse_rule_spec_accepts_english_aliases_and_decimals():
    rules = parse_rule_spec(
        "drawdown window=60 threshold=0.3；return window=10 min=-0.1 max=0.1；ma fast=5 slow=10"
    )
    assert rules[0].threshold == pytest.approx(0.3)
    assert rules[1].min_return == pytest.approx(-0.1)
    assert rules[2].slow_window == 10


@pytest.mark.parametrize(
    "spec, expected_hint",
    [
        ("成交量 窗口=5", "回撤"),
        ("回撤 窗口=60", "阈值"),
        ("回撤 窗口=six 阈值=30%", "数值"),
        ("回撤 窗口=60 阈值=30% 未知=1", "不支持参数"),
        ("金叉 快线=10 慢线=5", "fast_window"),
        ("涨停 阈值=10%", "涨停"),
        ("最近交易日涨停", "涨停"),
    ],
)
def test_parse_rule_spec_rejects_invalid_input_with_guidance(spec, expected_hint):
    with pytest.raises(RuleSpecError) as exc_info:
        parse_rule_spec(spec)
    assert expected_hint in str(exc_info.value)


def test_parse_rule_spec_supports_limit_up_with_optional_window():
    rules = parse_rule_spec("涨停")
    assert rules == [LimitUpRule(window=1)]
    windowed = parse_rule_spec("涨停 窗口=3")
    assert windowed == [LimitUpRule(window=3)]
    combined = parse_rule_spec("涨停；金叉 快线=5 慢线=10")
    assert combined == [LimitUpRule(window=1), FirstBullishMARule(fast_window=5, slow_window=10)]


def test_limit_up_rule_full_draft_flow():
    coordinator, store = build_interaction()
    coordinator.handle_message("ou_a", "oc_chat", "新建策略 拉取最近交易日涨停的股票")
    draft = store.list_drafts("ou_a")[0]
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )

    ready_card = coordinator.handle_message("ou_a", "oc_chat", "规则 涨停")
    assert "保存策略" in card_text(ready_card)
    draft = store.get_draft(draft.draft_id, "ou_a")
    assert draft.state == DraftState.READY
    assert draft.rules == [LimitUpRule(window=1)]

    saved_card = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )
    assert "已保存" in card_text(saved_card)
    assert store.list_strategies("ou_a")[0].rules == [LimitUpRule(window=1)]


def test_guided_draft_flow_saves_enabled_strategy():
    coordinator, store = build_interaction()
    create_card = coordinator.handle_message("ou_a", "oc_chat", "新建策略")
    assert "策略名称" in card_text(create_card)

    draft = store.list_drafts("ou_a")[0]
    assert draft.state == DraftState.AWAITING_NAME

    name_card = coordinator.handle_message("ou_a", "oc_chat", "策略名称 深度回撤首次转多")
    assert "买入" in card_text(name_card) and "卖出" in card_text(name_card)
    draft = store.get_draft(draft.draft_id, "ou_a")
    assert draft.state == DraftState.AWAITING_DIRECTION
    assert draft.name == "深度回撤首次转多"

    direction_card = coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "sell"},
    )
    assert "规则" in card_text(direction_card)
    draft = store.get_draft(draft.draft_id, "ou_a")
    assert draft.state == DraftState.AWAITING_RULES
    assert draft.direction == SignalDirection.SELL

    ready_card = coordinator.handle_message("ou_a", "oc_chat", f"规则 {USER_RULES_TEXT}")
    assert "保存策略" in card_text(ready_card)
    draft = store.get_draft(draft.draft_id, "ou_a")
    assert draft.state == DraftState.READY
    assert len(draft.rules) == 3

    saved_card = coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_save",
        {"draft_id": draft.draft_id},
    )
    assert "已保存" in card_text(saved_card)

    strategies = store.list_strategies("ou_a")
    assert len(strategies) == 1
    strategy = strategies[0]
    assert strategy.name == "深度回撤首次转多"
    assert strategy.direction == SignalDirection.SELL
    assert strategy.enabled is True
    assert strategy.notification_chat_id == "oc_chat"
    assert len(strategy.rules) == 3
    assert store.list_drafts("ou_a") == []


def test_invalid_rules_keep_draft_state_and_guide_user():
    coordinator, store = build_interaction()
    coordinator.handle_message("ou_a", "oc_chat", "新建策略 深度回撤")
    draft = store.list_drafts("ou_a")[0]
    assert draft.state == DraftState.AWAITING_DIRECTION
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )

    error_card = coordinator.handle_message("ou_a", "oc_chat", "规则 回撤 阈值=30%")
    assert "窗口" in card_text(error_card)
    draft = store.get_draft(draft.draft_id, "ou_a")
    assert draft.state == DraftState.AWAITING_RULES
    assert draft.rules == []

    ready_card = coordinator.handle_message("ou_a", "oc_chat", f"规则 {USER_RULES_TEXT}")
    assert "保存策略" in card_text(ready_card)


def test_draft_and_strategy_isolation_between_users():
    coordinator, store = build_interaction()
    coordinator.handle_message("ou_a", "oc_chat", "新建策略 甲的策略")
    draft = store.list_drafts("ou_a")[0]
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )
    coordinator.handle_message("ou_a", "oc_chat", f"规则 {USER_RULES_TEXT}")

    # User B must not reach user A's draft at any step.
    denied_card = coordinator.handle_card_action(
        "ou_b",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "sell"},
    )
    assert "无权访问" in card_text(denied_card)
    denied_save = coordinator.handle_card_action(
        "ou_b", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )
    assert "无权访问" in card_text(denied_save)

    coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )
    strategy = store.list_strategies("ou_a")[0]

    denied_toggle = coordinator.handle_card_action(
        "ou_b",
        "oc_chat",
        "strategy_toggle",
        {"strategy_id": strategy.id, "enabled": False},
    )
    assert "无权访问" in card_text(denied_toggle)
    assert store.list_strategies("ou_b") == []
    assert store.list_strategies("ou_a")[0].enabled is True


def test_handles_prompt_matches_only_strategy_commands():
    coordinator, _ = build_interaction()
    for command in (
        "新建策略",
        "策略列表",
        "运行规则",
        "删除规则 大涨回撤列表",
        "试算规则 大涨回撤列表 2026-09-01 至 2026-09-22",
        "取消草稿",
        "策略菜单",
        "策略名称 X",
        "规则 回撤 窗口=5 阈值=10%",
    ):
        assert coordinator.handles_prompt(command) is True, command
    for research_text in (
        "帮我研究一下白酒板块近期的走势",
        "新建会话",
        "查看进度",
        "执行回测：300开头、市值大于500亿",
        "什么是MACD金叉",
        "写一个策略回测报告",
    ):
        assert coordinator.handles_prompt(research_text) is False, research_text


def test_run_all_runs_owner_enabled_strategies_once():
    scanner = FakeScanner()
    coordinator, store = build_interaction(scanner)
    coordinator.handle_message("ou_a", "oc_chat", "新建策略 甲的策略")
    draft = store.list_drafts("ou_a")[0]
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )
    coordinator.handle_message("ou_a", "oc_chat", f"规则 {USER_RULES_TEXT}")
    coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )

    result = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run_all", {}, request_id="req-1"
    )
    assert result is None
    assert scanner.calls == [("ou_a", "req-1", None)]


def test_run_all_reports_when_no_enabled_strategies_or_no_scanner():
    scanner = FakeScanner()
    coordinator, _ = build_interaction(scanner)
    notice = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run_all", {}, request_id="req-2"
    )
    assert "没有启用的策略" in card_text(notice)
    assert scanner.calls == []

    no_scanner_coordinator, store = build_interaction(None)
    no_scanner_notice = no_scanner_coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run_all", {}
    )
    assert "未配置" in card_text(no_scanner_notice)


def test_toggle_and_delete_card_actions_refresh_list():
    coordinator, store = build_interaction()
    coordinator.handle_message("ou_a", "oc_chat", "新建策略 甲的策略")
    draft = store.list_drafts("ou_a")[0]
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )
    coordinator.handle_message("ou_a", "oc_chat", f"规则 {USER_RULES_TEXT}")
    coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )
    strategy = store.list_strategies("ou_a")[0]

    disabled_card = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_toggle", {"strategy_id": strategy.id, "enabled": False}
    )
    assert "停用" in card_text(disabled_card)
    assert store.list_strategies("ou_a")[0].enabled is False

    deleted_card = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_delete", {"strategy_id": strategy.id}
    )
    assert "已删除规则" in card_text(deleted_card)
    assert strategy.id in card_text(deleted_card)
    assert store.list_strategies("ou_a") == []


def test_delete_rule_command_accepts_unique_name_and_exact_identifier():
    coordinator, store = build_interaction()
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "待删除规则")
    first = store.list_strategies("ou_a")[0]

    by_name = coordinator.handle_message(
        "ou_a", "oc_chat", "删除规则 待删除规则"
    )
    assert first.id in card_text(by_name)
    assert store.list_strategies("ou_a") == []

    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "另一条规则")
    second = store.list_strategies("ou_a")[0]
    by_id = coordinator.handle_message(
        "ou_a", "oc_chat", f"删除规则 {second.id}"
    )
    assert "已删除" in card_text(by_id)
    assert store.list_strategies("ou_a") == []


def test_trial_command_runs_latest_compiled_draft_with_explicit_range():
    from china_a_share.discovery.rule_compiler import RuleCompileResult

    scanner = FakeScanner()
    compiler = FakeCompiler(
        result=RuleCompileResult(compiled_rule=compiled_rule())
    )
    coordinator, store = build_interaction(scanner, compiler)
    draft = seed_draft_at_rules_step(coordinator, store, name="大涨回撤列表")
    coordinator.handle_message("ou_a", "oc_chat", "规则 灵活的多阶段筛选")

    response = coordinator.handle_message(
        "ou_a",
        "oc_chat",
        "试算规则 2026-09-01 至 2026-09-22",
        request_id="trial-1",
    )

    assert response is None
    assert scanner.calls == [
        ("trial", draft.draft_id, "20260901", "20260922", "trial-1")
    ]


def test_trial_command_runs_saved_rule_by_name():
    from china_a_share.discovery.rule_compiler import RuleCompileResult

    scanner = FakeScanner()
    compiler = FakeCompiler(
        result=RuleCompileResult(compiled_rule=compiled_rule())
    )
    coordinator, store = build_interaction(scanner, compiler)
    draft = seed_draft_at_rules_step(coordinator, store, name="大涨回撤列表")
    coordinator.handle_message("ou_a", "oc_chat", "规则 灵活的多阶段筛选")
    coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )
    saved = store.list_strategies("ou_a")[0]

    response = coordinator.handle_message(
        "ou_a",
        "oc_chat",
        "试算规则 大涨回撤列表 2026-09-15 至 2026-09-22",
        request_id="trial-2",
    )

    assert response is None
    assert scanner.calls == [
        ("trial", saved.id, "20260915", "20260922", "trial-2")
    ]


def test_natural_language_rules_compile_to_validated_rules():
    from china_a_share.discovery.rule_compiler import RuleCompileResult

    compiler = FakeCompiler(
        result=RuleCompileResult(
            compiled_rule=compiled_rule("60天内跌掉三成后横盘，今天涨停"),
        )
    )
    coordinator, store = build_interaction(compiler=compiler)
    seed_draft_at_rules_step(coordinator, store)

    card = coordinator.handle_message(
        "ou_a", "oc_chat", "规则 60天内跌掉三成后横盘，今天涨停"
    )
    assert "保存策略" in card_text(card)
    assert compiler.calls == ["60天内跌掉三成后横盘，今天涨停"]
    draft = store.list_drafts("ou_a")[0]
    assert draft.state == DraftState.READY
    assert draft.rules == []
    assert draft.compiled_rule is not None
    assert draft.compiled_rule.source_text == "60天内跌掉三成后横盘，今天涨停"


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "找到同一个行业涨停股数量大于等于5只，或者大于10%"
            "（个股数量少于20只的行业）同时大于等于2只，间隔在10个交易日以内，"
            "取最晚一日为触发日。给我列出其中的涨停股名字、涨停日期、涨幅、市值、是否st"
        ),
        (
            "我在A中搜索，在最近10个交易日中（截止9.22）只出现过一次收盘涨幅超过5%的股票，"
            "然后在这些股票中，寻找最新的收盘价第一次回撤到大涨当日盘中最低价1.03倍以下"
            "或者大涨当日的前一日收盘价1.03倍以下的股票，然后在这些股票中找出大涨日的最高价"
            "大于当日收盘时20日线值和60日线值的股票，给出列表，大涨回撤列表"
        ),
    ],
)
def test_reported_complex_rules_are_preserved_as_one_frozen_plan(prompt):
    from china_a_share.discovery.rule_compiler import RuleCompileResult

    compiler = FakeCompiler(
        result=RuleCompileResult(compiled_rule=compiled_rule(prompt))
    )
    coordinator, store = build_interaction(compiler=compiler)
    seed_draft_at_rules_step(coordinator, store)

    card = coordinator.handle_message("ou_a", "oc_chat", f"规则 {prompt}")

    assert "保存策略" in card_text(card)
    assert "你的意思是不是下面之一" not in card_text(card)
    stored = store.list_drafts("ou_a")[0].compiled_rule
    assert stored is not None
    assert stored.source_text == prompt
    assert compiler.calls == [prompt]


def test_unclear_rules_explain_ambiguity_without_candidate_option_buttons():
    from china_a_share.discovery.rule_compiler import RuleCompileResult

    compiler = FakeCompiler(
        result=RuleCompileResult(
            clarification_options=["请明确股票集合A的构成。"],
            limitations=["当前对话没有定义集合A。"],
        )
    )
    coordinator, store = build_interaction(compiler=compiler)
    draft = seed_draft_at_rules_step(coordinator, store)

    card = coordinator.handle_message("ou_a", "oc_chat", "规则 最近交易日涨停")
    text = card_text(card)
    assert "请明确股票集合A" in text
    assert "当前对话没有定义集合A" in text
    assert "你的意思是不是下面之一" not in text
    assert not any(
        value.get("action") == "strategy_rules_candidate"
        for value in button_values(card)
    )
    assert store.get_draft(draft.draft_id, "ou_a").state == DraftState.AWAITING_RULES


def test_uncompilable_rules_report_failure_without_fixed_vocabulary():
    compiler = FakeCompiler(result=None)
    coordinator, store = build_interaction(compiler=compiler)
    seed_draft_at_rules_step(coordinator, store)

    card = coordinator.handle_message("ou_a", "oc_chat", "规则 成交量放大十倍")
    text = card_text(card)
    assert "暂时不可用" in text
    assert "支持的类型" not in text
    assert "你的意思是不是下面之一" not in text
    assert store.list_drafts("ou_a")[0].state == DraftState.AWAITING_RULES


def test_compiler_failure_falls_back_to_precise_issue_card():
    compiler = FakeCompiler(raising=True)
    coordinator, store = build_interaction(compiler=compiler)
    seed_draft_at_rules_step(coordinator, store)

    card = coordinator.handle_message("ou_a", "oc_chat", "规则 回撤 阈值=30%")
    assert "规则需要补充" in card_text(card)
    assert store.list_drafts("ou_a")[0].state == DraftState.AWAITING_RULES


def test_without_compiler_strict_dsl_errors_still_guide_user():
    coordinator, store = build_interaction()
    seed_draft_at_rules_step(coordinator, store)

    card = coordinator.handle_message("ou_a", "oc_chat", "规则 回撤 阈值=30%")
    text = card_text(card)
    assert "窗口" in text
    assert store.list_drafts("ou_a")[0].state == DraftState.AWAITING_RULES


def test_menu_cards_defer_manual_runs_to_strategy_list():
    strategy_menu = build_strategy_menu_card()
    quick_menu = build_feishu_quick_menu_card(include_strategy=True)
    for card in (strategy_menu, quick_menu):
        actions = [value["action"] for value in button_values(card)]
        assert "strategy_create_draft" in actions
        assert "strategy_list" in actions
        assert "strategy_run_all" not in actions

    plain_quick_menu = build_feishu_quick_menu_card(include_strategy=False)
    assert not any(
        "strategy" in value.get("action", "")
        for value in button_values(plain_quick_menu)
    )


def test_list_card_offers_per_strategy_run_and_run_all():
    coordinator, store = build_interaction()
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "甲策略")
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "乙策略")

    card = coordinator.handle_card_action("ou_a", "oc_chat", "strategy_list", {})
    values = button_values(card)
    strategy_ids = {strategy.id for strategy in store.list_strategies("ou_a")}
    per_row_runs = [
        value for value in values if value.get("action") == "strategy_run"
    ]
    assert {value["strategy_id"] for value in per_row_runs} == strategy_ids
    assert [value.get("action") for value in values].count("strategy_run_all") == 1


def test_saved_card_runs_only_the_just_saved_strategy():
    coordinator, store = build_interaction()
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "甲策略")
    saved_card = save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "乙策略")

    values = button_values(saved_card)
    run_values = [value for value in values if value.get("action") == "strategy_run"]
    assert len(run_values) == 1
    assert run_values[0]["strategy_id"] in {
        strategy.id for strategy in store.list_strategies("ou_a")
    }
    assert all(value.get("action") != "strategy_run_all" for value in values)


def test_run_one_runs_only_that_strategy():
    scanner = FakeScanner()
    coordinator, store = build_interaction(scanner)
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "甲策略")
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "乙策略")
    target = store.list_strategies("ou_a")[1]

    result = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run", {"strategy_id": target.id}, request_id="req-1"
    )

    assert result is None
    assert scanner.calls == [("ou_a", "req-1", target.id)]


def test_run_one_rejects_missing_disabled_or_foreign_strategy():
    scanner = FakeScanner()
    coordinator, store = build_interaction(scanner)
    save_strategy_via_draft(coordinator, store, "ou_a", "oc_chat", "甲策略")
    save_strategy_via_draft(coordinator, store, "ou_b", "oc_chat", "乙策略")
    foreign = store.list_strategies("ou_b")[0]

    missing = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run", {"strategy_id": "missing"}, request_id="r1"
    )
    assert "策略不存在或无权访问" in card_text(missing)

    foreign_notice = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run", {"strategy_id": foreign.id}, request_id="r2"
    )
    assert "策略不存在或无权访问" in card_text(foreign_notice)

    own = store.list_strategies("ou_a")[0]
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_toggle",
        {"strategy_id": own.id, "enabled": False},
        request_id="r3",
    )
    disabled_notice = coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_run", {"strategy_id": own.id}, request_id="r4"
    )
    assert "已停用" in card_text(disabled_notice)
    assert scanner.calls == []


def build_bot(interaction, allowed_open_ids=None):
    sender = FakeSender()
    task_coordinator = FakeTaskCoordinator()
    bot = FeishuResearchBot(
        task_coordinator,
        sender,
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        allowed_open_ids=allowed_open_ids,
        strategy_interaction=interaction,
    )
    return bot, sender, task_coordinator


def message_event(prompt, sender_open_id="ou_a", event_id="evt-1"):
    return FeishuMessageEvent(
        event_id=event_id,
        message_id="msg-1",
        conversation_id=f"tenant:oc_chat:root:{sender_open_id}",
        prompt=prompt,
        sender_open_id=sender_open_id,
        chat_id="oc_chat",
    )


def test_bot_routes_strategy_commands_and_leaves_research_untouched():
    coordinator, _ = build_interaction()
    bot, sender, task_coordinator = build_bot(interaction=coordinator)

    bot.process(message_event("新建策略"))
    assert len(sender.cards) == 1
    assert "策略名称" in card_text(sender.cards[0][1])
    assert task_coordinator.requests == []

    bot.process(message_event("帮我研究白酒板块", event_id="evt-2"))
    assert sender.cards == [(sender.cards[0][0], sender.cards[0][1])]
    assert len(task_coordinator.requests) == 1


def strategy_card_payload(action_name, value, *, operator="ou_a", event_id="evt-card-1"):
    return {
        "header": {
            "token": "verification-token",
            "event_type": "card.action.trigger",
            "event_id": event_id,
            "tenant_key": "tenant",
        },
        "event": {
            "operator": {"open_id": operator},
            "context": {
                "open_chat_id": "oc_chat",
                "open_message_id": "msg-card-1",
            },
            "action": {"value": {"action": action_name, **value}},
        },
    }


def test_bot_parses_and_dedupes_strategy_card_actions():
    coordinator, store = build_interaction()
    bot, sender, _ = build_bot(interaction=coordinator)

    action = bot.parse_strategy_card_action(
        strategy_card_payload("strategy_create_draft", {})
    )
    assert action is not None
    assert action.operator_open_id == "ou_a"
    assert action.chat_id == "oc_chat"

    bot.process_strategy_card_action(action)
    assert len(sender.cards) == 1
    assert store.list_drafts("ou_a")

    # The same Feishu event id must not execute twice.
    bot.process_strategy_card_action(action)
    assert len(sender.cards) == 1


def test_bot_strategy_card_actions_respect_open_id_allowlist():
    coordinator, _ = build_interaction()
    bot, _, _ = build_bot(interaction=coordinator, allowed_open_ids={"ou_a"})

    with pytest.raises(FeishuEventError):
        bot.parse_strategy_card_action(
            strategy_card_payload("strategy_create_draft", {}, operator="ou_b")
        )


def test_bot_non_strategy_card_actions_fall_back_to_legacy_path():
    coordinator, _ = build_interaction()
    bot, _, _ = build_bot(interaction=coordinator)

    action = bot.parse_strategy_card_action(
        strategy_card_payload("new_session", {})
    )
    assert action is None

    legacy = bot.parse_card_action(strategy_card_payload("new_session", {}))
    assert legacy is not None
    assert legacy.prompt == "新建会话"


def test_bot_strategy_card_failure_replies_error_text():
    scanner = FakeScanner(failing=True)
    coordinator, store = build_interaction(scanner)
    coordinator.handle_message("ou_a", "oc_chat", "新建策略 甲的策略")
    draft = store.list_drafts("ou_a")[0]
    coordinator.handle_card_action(
        "ou_a",
        "oc_chat",
        "strategy_draft_direction",
        {"draft_id": draft.draft_id, "direction": "buy"},
    )
    coordinator.handle_message("ou_a", "oc_chat", f"规则 {USER_RULES_TEXT}")
    coordinator.handle_card_action(
        "ou_a", "oc_chat", "strategy_draft_save", {"draft_id": draft.draft_id}
    )

    bot, sender, _ = build_bot(interaction=coordinator)
    action = bot.parse_strategy_card_action(
        strategy_card_payload("strategy_run_all", {}, event_id="evt-run-1")
    )
    bot.process_strategy_card_action(action)
    assert len(sender.replies) == 1
    assert "策略操作失败" in sender.replies[0][1]
