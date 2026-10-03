"""Offline unit tests for the VIX group-alert capability.

Every fake quotes the real CNBC payload shape captured on 2026-10-03; the
invariants asserted here are the capability contract (thresholds live only in
group configuration, delivery is at-least-once per rule per trading day, and
history only ever extends), not any single reported example.
"""

from datetime import datetime, timezone

import pytest

from china_a_share.vix.commands import (
    handle_vix_command,
    handles_vix_command,
)
from china_a_share.vix.history import (
    MemoryVixHistoryStore,
    VixHistory,
    parse_fred_vixcls_csv,
)
from china_a_share.vix.rules import (
    MemoryVixRuleStore,
    VixRule,
    new_vix_rule,
)
from china_a_share.vix.scanner import (
    MemoryAlertClaimStore,
    VixAlertService,
    compose_alert_message,
)
from china_a_share.vix.source import (
    VixQuote,
    eastern_wall_clock,
    fetch_vix_quote,
    parse_vix_quote,
)


CNBC_PAYLOAD = """
{"FormattedQuoteResult":{"FormattedQuote":[{"symbol":"VIX","last":"15.31",
"change":"-1.08","change_pct":"-6.59%","previous_day_closing":"16.39",
"last_time":"2026-10-02","last_timedate":"10/02/26 EDT","open":"16.15",
"high":"16.24","low":"15.30","type":"INDEX","realTime":"true"}]}}
"""

FRED_PAYLOAD = """observation_date,VIXCLS
1990-01-02,17.25
1990-01-03,.
1990-01-04,16.80
2026-10-01,16.39
"""

CHAT_ID = "oc_test_chat_001"


def eastern(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute)


class FakeDispatcher:
    def __init__(self, failing=False):
        self.sent = []
        self.failing = failing

    def send_chat_text(self, chat_id, text):
        if self.failing:
            raise RuntimeError("feishu down")
        self.sent.append((chat_id, text))
        return f"msg-{len(self.sent)}"


def build_service(
    *,
    closes=None,
    rules=None,
    dispatcher=None,
    quote=None,
    claims=None,
    now=None,
):
    history_store = MemoryVixHistoryStore(closes if closes is not None else {})
    history = VixHistory(history_store, http_get=lambda url, timeout: FRED_PAYLOAD)
    rule_store = MemoryVixRuleStore()
    for chat_id, chat_rules in (rules or {CHAT_ID: []}).items():
        rule_store.save(chat_id, chat_rules)
    return VixAlertService(
        history=history,
        rules=rule_store,
        claims=claims or MemoryAlertClaimStore(),
        dispatcher=dispatcher or FakeDispatcher(),
        quote_fetcher=(lambda: quote) if quote is not None else (lambda: None),
        now_fn=lambda: now or eastern(2026, 10, 2, 18, 0),
    )


def up_rule(threshold, rule_id="r-up"):
    return VixRule(rule_id, "up", threshold, "user", "2026-10-01T00:00:00+00:00")


def down_rule(threshold, rule_id="r-down"):
    return VixRule(rule_id, "down", threshold, "user", "2026-10-01T00:00:00+00:00")


def intraday_quote(change_pct=-6.59, date_str="2026-10-02", close=15.31):
    prev = close / (1 + change_pct / 100.0)
    return VixQuote(date_str, close, round(prev, 4), change_pct, date_str)


# --- source -----------------------------------------------------------------


def test_parse_vix_quote_reads_real_payload_shape():
    quote = parse_vix_quote(CNBC_PAYLOAD)
    assert quote.date == "2026-10-02"
    assert quote.close == pytest.approx(15.31)
    assert quote.prev_close == pytest.approx(16.39)
    assert quote.change_pct == pytest.approx(-6.59)


def test_parse_vix_quote_computes_change_when_missing():
    payload = CNBC_PAYLOAD.replace('"change_pct":"-6.59%",', "")
    quote = parse_vix_quote(payload)
    assert quote.change_pct == pytest.approx((15.31 / 16.39 - 1) * 100)


def test_parse_vix_quote_rejects_missing_levels():
    broken = CNBC_PAYLOAD.replace('"last":"15.31",', "")
    with pytest.raises(ValueError):
        parse_vix_quote(broken)


def test_fetch_vix_quote_retries_transient_failures():
    attempts = []

    def flaky(url, timeout):
        attempts.append(url)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return CNBC_PAYLOAD

    assert fetch_vix_quote(flaky) is not None
    assert len(attempts) == 3


def test_fetch_vix_quote_returns_none_after_all_attempts_fail():
    def broken(url, timeout):
        raise RuntimeError("down")

    assert fetch_vix_quote(broken) is None


def test_quote_evaluability_windows():
    quote = intraday_quote()
    # During the quote's own date the snapshot is evaluable, settled after 17:00.
    assert quote.evaluable(eastern(2026, 10, 2, 12, 0))
    assert not quote.settled(eastern(2026, 10, 2, 16, 59))
    assert quote.settled(eastern(2026, 10, 2, 17, 0))
    # Any older quote date is stale and never drives alerts.
    assert not quote.evaluable(eastern(2026, 10, 3, 12, 0))


def test_eastern_wall_clock_follows_us_dst():
    utc = timezone.utc
    summer = datetime(2026, 7, 1, 16, 0, tzinfo=utc)  # 12:00 EDT
    winter = datetime(2026, 1, 15, 17, 0, tzinfo=utc)  # 12:00 EST
    assert eastern_wall_clock(summer).hour == 12
    assert eastern_wall_clock(winter).hour == 12
    assert eastern_wall_clock(summer) == eastern(2026, 7, 1, 12, 0)
    assert eastern_wall_clock(winter) == eastern(2026, 1, 15, 12, 0)


# --- history ----------------------------------------------------------------


def test_parse_fred_csv_skips_missing_markers():
    closes = parse_fred_vixcls_csv(FRED_PAYLOAD)
    assert closes == {"1990-01-02": 17.25, "1990-01-04": 16.80, "2026-10-01": 16.39}


def test_history_seeds_once_from_fred_then_extends_monotonically():
    store = MemoryVixHistoryStore()
    history = VixHistory(store, http_get=lambda url, timeout: FRED_PAYLOAD)
    history.ensure_seeded()
    first = store.get_closes()
    history.ensure_seeded()
    assert store.get_closes() == first

    assert history.record_settlement("2026-10-02", 15.31) is True
    # Re-recording the same or older date never changes the series.
    assert history.record_settlement("2026-10-02", 15.31) is False
    assert history.record_settlement("2026-10-01", 99.0) is False
    assert store.get_closes()["2026-10-02"] == 15.31


def test_history_lookup_returns_previous_trading_day():
    history = VixHistory(
        MemoryVixHistoryStore({"2026-10-01": 16.39, "2026-10-02": 15.31}),
        http_get=lambda url, timeout: FRED_PAYLOAD,
    )
    assert history.lookup("2026-10-02") == ("2026-10-01", 16.39, 15.31)
    assert history.lookup("2026-10-03") is None


# --- rules ------------------------------------------------------------------


def test_rule_threshold_bounds_are_enforced():
    for bad in (0.0, -1.0, 100.5):
        with pytest.raises(ValueError):
            new_vix_rule("up", bad, "user", "x")
    assert new_vix_rule("up", 100.0, "user", "x").threshold_pct == 100.0
    with pytest.raises(ValueError):
        new_vix_rule("sideways", 10.0, "user", "x")


def test_rule_hit_semantics_are_inclusive_of_threshold():
    assert up_rule(10.0).hits(10.0)
    assert not up_rule(10.0).hits(9.99)
    assert down_rule(5.0).hits(-5.0)
    assert not down_rule(5.0).hits(-4.99)
    assert not down_rule(5.0).hits(5.0)


def test_rule_store_roundtrip_and_listing():
    store = MemoryVixRuleStore()
    store.save(CHAT_ID, [up_rule(10.0)])
    store.save("oc_other", [down_rule(5.0)])
    assert [rule.id for rule in store.load(CHAT_ID)] == ["r-up"]
    assert sorted(store.iter_chat_ids()) == sorted([CHAT_ID, "oc_other"])


# --- scanner ----------------------------------------------------------------


def test_scan_alerts_only_chats_with_hit_rules():
    dispatcher = FakeDispatcher()
    rules = {
        "oc_a": [up_rule(10.0, "r1")],
        "oc_b": [up_rule(10.0, "r1"), up_rule(20.0, "r2")],
    }
    service = build_service(
        rules=rules,
        dispatcher=dispatcher,
        quote=intraday_quote(change_pct=-6.59),
        now=eastern(2026, 10, 2, 12, 0),
    )
    summary = service.run_hourly_scan()
    assert summary["status"] == "completed"
    assert summary["chats_scanned"] == 2
    assert summary["alerts_sent"] == 0  # a -6.59% move hits no up-threshold rule
    assert dispatcher.sent == []

    service_down = build_service(
        rules={"oc_a": [down_rule(5.0, "r1")]},
        dispatcher=dispatcher,
        quote=intraday_quote(change_pct=-6.59),
        now=eastern(2026, 10, 2, 12, 0),
    )
    summary = service_down.run_hourly_scan()
    assert summary["alerts_sent"] == 1
    chat_id, text = dispatcher.sent[0]
    assert chat_id == "oc_a"
    assert "15.31" in text
    assert "下跌 6.59%" in text
    assert "≥5.00%" in text


def test_scan_deduplicates_per_rule_per_trading_day():
    dispatcher = FakeDispatcher()
    service = build_service(
        rules={CHAT_ID: [down_rule(5.0, "r1")]},
        dispatcher=dispatcher,
        quote=intraday_quote(change_pct=-6.59),
        now=eastern(2026, 10, 2, 12, 0),
    )
    assert service.run_hourly_scan()["alerts_sent"] == 1
    assert service.run_hourly_scan()["alerts_sent"] == 0


def test_scan_retries_after_send_failure_and_keeps_others():
    failing = FakeDispatcher(failing=True)
    service = build_service(
        rules={CHAT_ID: [down_rule(5.0, "r1")]},
        dispatcher=failing,
        quote=intraday_quote(change_pct=-6.59),
        now=eastern(2026, 10, 2, 12, 0),
    )
    assert service.run_hourly_scan()["alerts_failed"] == 1

    healthy = FakeDispatcher()
    service.dispatcher = healthy
    summary = service.run_hourly_scan()
    assert summary["alerts_sent"] == 1
    assert len(healthy.sent) == 1


def test_scan_skips_stale_quote_and_records_settlement_once():
    dispatcher = FakeDispatcher()
    service = build_service(
        rules={CHAT_ID: [down_rule(5.0, "r1")]},
        dispatcher=dispatcher,
        quote=intraday_quote(date_str="2026-10-01"),
        now=eastern(2026, 10, 2, 12, 0),
    )
    assert service.run_hourly_scan()["status"] == "skipped_stale_quote"
    assert dispatcher.sent == []

    settled_service = build_service(
        rules={CHAT_ID: [down_rule(5.0, "r1")]},
        dispatcher=dispatcher,
        quote=intraday_quote(date_str="2026-10-02"),
        now=eastern(2026, 10, 2, 18, 0),
    )
    summary = settled_service.run_hourly_scan()
    assert summary["settled_recorded"] is True
    assert summary["alerts_sent"] == 1
    assert settled_service.history.lookup("2026-10-02") is not None


def test_scan_without_quote_reports_skip():
    service = build_service(quote=None)
    assert service.run_hourly_scan()["status"] == "skipped_no_quote"


def test_compose_message_formats_two_decimals_and_rules():
    message = compose_alert_message(
        intraday_quote(change_pct=11.111), [up_rule(10.0, "r1")]
    )
    assert "上涨 11.11%" in message
    assert "≥10.00%" in message
    assert "[r1]" not in message  # ids are not part of alert text


# --- lookback ---------------------------------------------------------------


def test_lookback_uses_history_then_reports_per_rule():
    service = build_service(
        closes={"2026-10-01": 16.39},
        rules={CHAT_ID: [up_rule(10.0, "r1"), down_rule(5.0, "r2")]},
        quote=intraday_quote(),
        now=eastern(2026, 10, 2, 18, 0),
    )
    reply = handle_vix_command(service, CHAT_ID, "VIX回溯 2026-10-02", "user")
    assert "收盘 15.31" in reply
    assert "16.39" in reply
    assert "✓ 命中 [r2]" in reply
    assert "✗ 未命中 [r1]" in reply


def test_lookback_reports_missing_data_and_bad_dates():
    service = build_service(rules={CHAT_ID: [up_rule(10.0)]}, quote=None)
    missing = handle_vix_command(service, CHAT_ID, "VIX回溯 2026-10-03", "user")
    assert "没有 VIX 交易数据" in missing
    bad = handle_vix_command(service, CHAT_ID, "VIX回溯 2026-13-99", "user")
    assert "日期格式无法识别" in bad


def test_lookback_accepts_compact_date_form():
    service = build_service(
        closes={"2026-10-01": 16.39, "2026-10-02": 15.31},
        rules={CHAT_ID: [down_rule(5.0, "r1")]},
    )
    reply = handle_vix_command(service, CHAT_ID, "VIX回溯 20261002", "user")
    assert "✓ 命中 [r1]" in reply


# --- command parsing and lifecycle ------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "VIX规则",
        "VIX提醒 上涨10%",
        "VIX提醒 涨10",
        "VIX提醒 下跌5%",
        "VIX提醒 跌 5.5%",
        "VIX删除规则 全部",
        "VIX删除规则 r1a2b3c4",
        "VIX回溯 2026-04-04",
        "VIX回溯 20260404",
    ],
)
def test_command_patterns_recognize_the_whole_family(prompt):
    assert handles_vix_command(prompt)


@pytest.mark.parametrize(
    "prompt",
    ["vix提醒 上涨10%", "VIX回溯", "提醒 上涨10%"],
)
def test_non_commands_are_not_routed(prompt):
    assert not handles_vix_command(prompt)


def test_add_rule_rejects_out_of_range_threshold_via_reply():
    service = build_service()
    reply = handle_vix_command(service, CHAT_ID, "VIX提醒 上涨0%", "user")
    assert "无法添加规则" in reply
    assert service.rules.load(CHAT_ID) == []


def test_add_rule_persists_and_reports_duplicates():
    service = build_service()
    first = handle_vix_command(service, CHAT_ID, "VIX提醒 上涨10%", "user-1")
    assert "已添加规则" in first
    stored = service.rules.load(CHAT_ID)
    assert len(stored) == 1
    assert stored[0].direction == "up"
    assert stored[0].threshold_pct == 10.0
    assert stored[0].created_by == "user-1"
    duplicate = handle_vix_command(service, CHAT_ID, "VIX提醒 涨10%", "user-2")
    assert "已存在" in duplicate
    assert len(service.rules.load(CHAT_ID)) == 1


def test_remove_rules_by_id_and_all():
    service = build_service(rules={CHAT_ID: [up_rule(10.0, "r1"), down_rule(5.0, "r2")]})
    missing = handle_vix_command(service, CHAT_ID, "VIX删除规则 nope", "user")
    assert "未找到编号" in missing
    removed = handle_vix_command(service, CHAT_ID, "VIX删除规则 R1", "user")
    assert "剩余 1 条" in removed
    assert [rule.id for rule in service.rules.load(CHAT_ID)] == ["r2"]
    removed_all = handle_vix_command(service, CHAT_ID, "VIX删除规则 全部", "user")
    assert "已删除全部 1 条" in removed_all
    assert service.rules.load(CHAT_ID) == []


def test_rules_listing_empty_then_populated():
    service = build_service()
    empty = handle_vix_command(service, CHAT_ID, "VIX规则", "user")
    assert "还没有" in empty and "用法" in empty
    handle_vix_command(service, CHAT_ID, "VIX提醒 下跌5%", "user")
    listed = handle_vix_command(service, CHAT_ID, "VIX规则", "user")
    assert "≥5.00%" in listed


def test_non_vix_prompts_return_none():
    service = build_service()
    assert handle_vix_command(service, CHAT_ID, "帮我看看茅台", "user") is None
