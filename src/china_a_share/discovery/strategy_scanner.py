"""Execute configured strategies and deliver one Feishu card per strategy."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import logging
from typing import Any, Callable, Optional, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from china_a_share.discovery.qfq_loader import QFQLoader
from china_a_share.discovery.rule_engine import RuleEngine
from china_a_share.discovery.strategy_models import (
    CumulativeReturnRule,
    DrawdownRule,
    FirstBullishMARule,
    LimitUpRule,
    SignalDirection,
    StrategyConfig,
)
from china_a_share.discovery.strategy_plan import answer_result, bind_plan_date
from china_a_share.discovery.strategy_store import StrategyStore
from china_a_share.application.workflow import AnalysisService
from china_a_share.core.contracts import AnalysisRequest, QueryStatus
from china_a_share.observability import log_event


STRATEGY_SCAN_API_ROUTE = "/api/analysis/tasks/strategy:daily-scan"
STRATEGY_TRIAL_API_ROUTE = "/api/analysis/tasks/strategy:trial"
FEISHU_AT_ALL_PERMISSION_ERROR_CODE = "230006"
SHANGHAI_TIME_ZONE = ZoneInfo("Asia/Shanghai")
MAX_TRIAL_CALENDAR_DAYS = 31
MAX_CARD_RESULT_ROWS = 30
FROZEN_PLAN_EXECUTION_PROMPT = (
    "Execute the confirmed reusable strategy plan exactly as provided."
)
logger = logging.getLogger(__name__)


class StrategyCardSender(Protocol):
    """Send a new interactive card to a Feishu chat."""

    def send_chat_card(self, chat_id: str, card: dict[str, Any]) -> str:
        """Send one card and return the created message identifier."""
        ...


class StrategyScanner:
    """Evaluate enabled strategies against one shared adjusted-price snapshot."""

    def __init__(
        self,
        loader: QFQLoader,
        engine: RuleEngine,
        store: StrategyStore,
        sender: StrategyCardSender,
        *,
        clock: Callable[[], datetime] | None = None,
        analysis_service: AnalysisService | None = None,
    ) -> None:
        self._loader = loader
        self._engine = engine
        self._store = store
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIME_ZONE))
        self._analysis_service = analysis_service

    def run_daily_scan(self, request_id: str) -> None:
        """Run all enabled strategies and expose partial failures to the scheduler."""
        strategies = self._store.list_enabled_strategies()
        if not strategies:
            return
        requested_date = self._scan_date()
        legacy = [strategy for strategy in strategies if strategy.compiled_rule is None]
        compiled = [strategy for strategy in strategies if strategy.compiled_rule is not None]
        if legacy:
            frame = self._load_shared_history(legacy, requested_date, request_id)
            if not frame.empty and str(frame["trade_date"].max()) == requested_date:
                self._run_strategies(
                    legacy,
                    frame,
                    signal_date=requested_date,
                    request_id=request_id,
                    preview=False,
                )
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "strategy_scan_non_trading_day",
                    request_id=request_id,
                    requested_date=requested_date,
                    latest_trade_date=(str(frame["trade_date"].max()) if not frame.empty else ""),
                )
        if compiled:
            requested = datetime.strptime(requested_date, "%Y%m%d")
            if requested.date() in self._trial_dates(
                requested,
                requested,
                request_id=request_id,
                api_route=STRATEGY_SCAN_API_ROUTE,
            ):
                self._run_compiled_strategies(
                    compiled,
                    signal_date=requested_date,
                    request_id=request_id,
                    preview=False,
                )

    def run_manual_preview(
        self, owner_open_id: str, request_id: str, strategy_id: Optional[str] = None
    ) -> None:
        """Run one owner's enabled strategies without reading or writing dedup markers.

        With strategy_id, restrict the run to that single strategy; a missing or
        disabled id simply runs nothing, letting the interaction layer decide
        what notice to show.
        """
        strategies = self._store.list_strategies(owner_open_id, enabled=True)
        if strategy_id is not None:
            strategies = [s for s in strategies if s.id == strategy_id]
        if not strategies:
            return
        compiled = [strategy for strategy in strategies if strategy.compiled_rule is not None]
        legacy = [strategy for strategy in strategies if strategy.compiled_rule is None]
        requested_date = self._scan_date()
        if legacy:
            frame = self._load_shared_history(legacy, requested_date, request_id)
            if frame.empty:
                raise RuntimeError("Strategy preview returned no market data.")
            self._run_strategies(
                legacy,
                frame,
                signal_date=str(frame["trade_date"].max()),
                request_id=request_id,
                preview=True,
            )
        if compiled:
            self._run_compiled_strategies(
                compiled,
                signal_date=requested_date,
                request_id=request_id,
                preview=True,
            )

    def run_trial(
        self,
        strategy: StrategyConfig,
        start_date: str,
        end_date: str,
        request_id: str,
    ) -> None:
        """Replay one frozen strategy over a bounded historical date range."""
        if strategy.compiled_rule is None:
            raise ValueError("Date-range trials require a compiled natural-language rule.")
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        if end < start:
            raise ValueError("Trial end date must not precede start date.")
        inclusive_days = (end - start).days + 1
        if inclusive_days > MAX_TRIAL_CALENDAR_DAYS:
            raise ValueError(
                f"Trial range cannot exceed {MAX_TRIAL_CALENDAR_DAYS} calendar days."
            )
        rows: list[dict[str, Any]] = []
        daily_counts: list[tuple[str, int]] = []
        for trading_date in self._trial_dates(
            start,
            end,
            request_id=request_id,
            api_route=STRATEGY_TRIAL_API_ROUTE,
        ):
            target_date = trading_date.strftime("%Y%m%d")
            result_rows = self._execute_compiled_plan(
                strategy,
                target_date,
                f"{request_id}-{target_date}",
                api_route=STRATEGY_TRIAL_API_ROUTE,
            )
            daily_counts.append((target_date, len(result_rows)))
            rows.extend(
                {"trial_date": target_date, **row}
                for row in result_rows
            )
        self._sender.send_chat_card(
            strategy.notification_chat_id,
            build_strategy_trial_result_card(
                strategy,
                start_date,
                end_date,
                rows,
                daily_counts,
            ),
        )

    def _trial_dates(
        self,
        start: datetime,
        end: datetime,
        *,
        request_id: str,
        api_route: str,
    ) -> list[date]:
        """Return exchange trading dates, with a weekday fallback for test doubles."""
        if self._analysis_service is not None:
            resolver = getattr(self._analysis_service, "trading_dates", None)
            if callable(resolver):
                return resolver(
                    start.date(),
                    end.date(),
                    request_id=request_id,
                    api_route=api_route,
                )
        return [
            (start + timedelta(days=offset)).date()
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        ]

    def _scan_date(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Strategy scanner clock must return a timezone-aware datetime.")
        return now.astimezone(SHANGHAI_TIME_ZONE).strftime("%Y%m%d")

    def _load_shared_history(
        self,
        strategies: list[StrategyConfig],
        end_date: str,
        request_id: str,
    ) -> pd.DataFrame:
        required_rows = max(strategy.required_history_rows for strategy in strategies)
        calendar_days = max(required_rows * 2, required_rows + 30)
        start = datetime.strptime(end_date, "%Y%m%d") - timedelta(days=calendar_days)
        return self._loader.load_qfq(
            start.strftime("%Y%m%d"),
            end_date,
            ts_codes=None,
            api_route=STRATEGY_SCAN_API_ROUTE,
            request_id=request_id,
        )

    def _run_strategies(
        self,
        strategies: list[StrategyConfig],
        frame: pd.DataFrame,
        *,
        signal_date: str,
        request_id: str,
        preview: bool,
    ) -> None:
        failures: list[str] = []
        for strategy in strategies:
            try:
                self._run_strategy(strategy, frame, signal_date, preview=preview)
            except Exception:
                failures.append(strategy.id)
                log_event(
                    logger,
                    logging.ERROR,
                    "strategy_scan_strategy_failed",
                    request_id=request_id,
                    strategy_id=strategy.id,
                    chat_id=strategy.notification_chat_id,
                    preview=preview,
                    exc_info=True,
                )
        if failures:
            raise RuntimeError(
                "Strategy scan failed for: " + ", ".join(sorted(failures))
            )

    def _run_strategy(
        self,
        strategy: StrategyConfig,
        frame: pd.DataFrame,
        signal_date: str,
        *,
        preview: bool,
    ) -> None:
        evaluated = self._engine.evaluate(frame, strategy)
        raw_matches = evaluated.loc[
            (evaluated["trade_date"].astype(str) == signal_date)
            & evaluated["signal"]
        ]
        deliverable = raw_matches
        if not preview and not raw_matches.empty:
            deliverable = raw_matches.loc[
                [
                    not self._store.is_notification_sent(
                        strategy.id,
                        str(row.ts_code),
                        signal_date,
                        strategy.direction,
                    )
                    for row in raw_matches.itertuples()
                ]
            ]

        card = build_strategy_result_card(
            strategy,
            signal_date,
            deliverable["ts_code"].astype(str).tolist(),
            raw_match_count=len(raw_matches),
            preview=preview,
        )
        self._send_with_at_all_fallback(strategy.notification_chat_id, card)

        # Mark only after the card succeeds. A crash between send and mark can
        # duplicate a retry, but it cannot permanently lose a notification.
        if not preview:
            for stock_code in deliverable["ts_code"].astype(str):
                self._store.mark_notification_sent(
                    strategy.id,
                    stock_code,
                    signal_date,
                    strategy.direction,
                )

    def _run_compiled_strategies(
        self,
        strategies: list[StrategyConfig],
        *,
        signal_date: str,
        request_id: str,
        preview: bool,
    ) -> None:
        failures: list[str] = []
        for strategy in strategies:
            try:
                rows = self._execute_compiled_plan(
                    strategy,
                    signal_date,
                    f"{request_id}-{strategy.id}",
                    api_route=(STRATEGY_TRIAL_API_ROUTE if preview else STRATEGY_SCAN_API_ROUTE),
                )
                deliverable = rows
                if not preview:
                    deliverable = [
                        row
                        for row in rows
                        if not self._store.is_notification_sent(
                            strategy.id,
                            self._row_identity(row),
                            signal_date,
                            strategy.direction,
                        )
                    ]
                card = build_strategy_result_card(
                    strategy,
                    signal_date,
                    [],
                    raw_match_count=len(rows),
                    preview=preview,
                    result_rows=deliverable,
                )
                self._send_with_at_all_fallback(strategy.notification_chat_id, card)
                if not preview:
                    for row in deliverable:
                        self._store.mark_notification_sent(
                            strategy.id,
                            self._row_identity(row),
                            signal_date,
                            strategy.direction,
                        )
            except Exception:
                failures.append(strategy.id)
                log_event(
                    logger,
                    logging.ERROR,
                    "compiled_strategy_scan_failed",
                    request_id=request_id,
                    strategy_id=strategy.id,
                    preview=preview,
                    exc_info=True,
                )
        if failures:
            raise RuntimeError(
                "Compiled strategy scan failed for: " + ", ".join(sorted(failures))
            )

    def _execute_compiled_plan(
        self,
        strategy: StrategyConfig,
        target_date: str,
        request_id: str,
        *,
        api_route: str,
    ) -> list[dict[str, Any]]:
        if self._analysis_service is None or strategy.compiled_rule is None:
            raise RuntimeError("Compiled strategy execution is not configured.")
        rebound_plan = bind_plan_date(strategy.compiled_rule, target_date)
        response = self._analysis_service.analyze(
            request_id,
            AnalysisRequest(
                # The source wording may contain the original observation date.
                # Execution is authorized by the frozen validated plan; using a
                # date-neutral prompt prevents the validation layer from mistaking
                # an explicit historical date for the newly bound trial cutoff.
                prompt=FROZEN_PLAN_EXECUTION_PROMPT,
                confirmed_plan=rebound_plan,
            ),
            api_route=api_route,
            progress_callback=lambda _completed, _total: None,
        )
        result = answer_result(response)
        if response.status == "error" or result is None:
            detail = (
                response.error.message
                if response.error is not None
                else "The frozen plan did not produce its declared answer result."
            )
            raise RuntimeError(detail)
        if result.status != QueryStatus.SUCCESS:
            detail = result.error.message if result.error is not None else "Query failed."
            raise RuntimeError(detail)
        return [dict(row) for row in result.rows]

    @staticmethod
    def _row_identity(row: dict[str, Any]) -> str:
        ts_code = str(row.get("ts_code", "")).strip()
        if ts_code:
            return ts_code
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return f"row-{sha256(encoded).hexdigest()[:20]}"

    def _send_with_at_all_fallback(
        self, chat_id: str, card: dict[str, Any]
    ) -> None:
        try:
            self._sender.send_chat_card(chat_id, card)
        except Exception as exc:
            if FEISHU_AT_ALL_PERMISSION_ERROR_CODE not in str(exc):
                raise
            log_event(
                logger,
                logging.ERROR,
                "feishu_at_all_failed",
                chat_id=chat_id,
                error=str(exc)[:300],
            )
            fallback = deepcopy(card)
            content = fallback["elements"][0]["text"]["content"]
            fallback["elements"][0]["text"]["content"] = content.replace(
                '<at id="all"></at>',
                "⚠️ 机器人无 @all 权限，全员提醒失败。",
                1,
            )
            self._sender.send_chat_card(chat_id, fallback)


def build_strategy_result_card(
    strategy: StrategyConfig,
    signal_date: str,
    stock_codes: list[str],
    *,
    raw_match_count: int,
    preview: bool,
    result_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build one self-contained result card for exactly one strategy."""
    direction = "买入" if strategy.direction == SignalDirection.BUY else "卖出"
    if result_rows:
        result = "\n".join(
            f"- {_format_result_row(row)}"
            for row in result_rows[:MAX_CARD_RESULT_ROWS]
        )
        if len(result_rows) > MAX_CARD_RESULT_ROWS:
            result += f"\n- ... 另有 {len(result_rows) - MAX_CARD_RESULT_ROWS} 条"
    elif stock_codes:
        result = "\n".join(f"- {stock_code}" for stock_code in stock_codes)
    elif raw_match_count:
        result = "> 本次扫描无新增可通知信号。"
    else:
        result = "> 本次扫描未命中任何标的。"
    title_prefix = "[预览] " if preview else ""
    content = (
        '<at id="all"></at>\n'
        f"**策略：** {strategy.name}\n"
        f"**建议：** {direction}\n"
        f"**信号日期：** {signal_date}\n"
        f"**规则：**\n{format_strategy_rules(strategy)}\n"
        f"**结果：**\n{result}"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if strategy.direction == SignalDirection.BUY else "red",
            "title": {
                "tag": "plain_text",
                "content": f"{title_prefix}A股策略扫描结果",
            },
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "仅供研究参考，不构成任何投资建议。",
                    }
                ],
            },
        ],
    }


def format_strategy_rules(strategy: StrategyConfig) -> str:
    """Render one human-readable bullet list for a strategy's rules."""
    if strategy.compiled_rule is not None:
        return (
            f"- 原始规则：{strategy.compiled_rule.source_text}\n"
            f"- 执行解释：{strategy.compiled_rule.interpretation}\n"
            f"- 计划指纹：{strategy.compiled_rule.plan_fingerprint[:12]}"
        )
    lines: list[str] = []
    for rule in strategy.rules:
        if isinstance(rule, DrawdownRule):
            lines.append(
                f"- {rule.window} 个交易日内由最高价至当日收盘回撤 "
                f"> {rule.threshold:.1%}"
            )
        elif isinstance(rule, CumulativeReturnRule):
            lines.append(
                f"- 近 {rule.window} 个交易日累计涨跌幅在 "
                f"[{rule.min_return:.1%}, {rule.max_return:.1%}]"
            )
        elif isinstance(rule, FirstBullishMARule):
            lines.append(
                f"- MA{rule.fast_window} 与 MA{rule.slow_window} 首次形成多头向上"
            )
        elif isinstance(rule, LimitUpRule):
            if rule.window == 1:
                lines.append("- 当日收盘涨停（按板块涨跌幅限制精确判定）")
            else:
                lines.append(
                    f"- 近 {rule.window} 个交易日内出现过收盘涨停（含当日）"
                )
    return "\n".join(lines)


def _format_result_row(row: dict[str, Any]) -> str:
    """Render one bounded auditable result row for a Feishu card."""
    return "，".join(f"{key}={value}" for key, value in row.items())


def build_strategy_trial_result_card(
    strategy: StrategyConfig,
    start_date: str,
    end_date: str,
    rows: list[dict[str, Any]],
    daily_counts: list[tuple[str, int]],
) -> dict[str, Any]:
    """Build an auditable no-lookahead replay card for one frozen plan."""
    compiled_rule = strategy.compiled_rule
    if compiled_rule is None:
        raise ValueError("trial cards require a compiled strategy rule")
    active_days = [(date, count) for date, count in daily_counts if count]
    funnel = "、".join(f"{date}:{count}" for date, count in active_days) or "无命中日期"
    if rows:
        result = "\n".join(
            f"- {_format_result_row(row)}" for row in rows[:MAX_CARD_RESULT_ROWS]
        )
        if len(rows) > MAX_CARD_RESULT_ROWS:
            result += f"\n- ... 另有 {len(rows) - MAX_CARD_RESULT_ROWS} 条"
    else:
        result = "> 区间内没有命中记录。"
    content = (
        f"**策略：** {strategy.name}\n"
        f"**试算区间：** {start_date} 至 {end_date}\n"
        "**防未来数据：** 每个交易日均以该日作为独立观察截止日执行冻结计划。\n"
        f"**计划指纹：** {compiled_rule.plan_fingerprint[:12]}\n"
        f"**命中漏斗（日期:行数）：** {funnel}\n"
        f"**总命中行数：** {len(rows)}\n"
        f"**结果与审计字段：**\n{result}"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "规则区间试算结果"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "仅供研究与规则核验，不构成任何投资建议。",
                    }
                ],
            },
        ],
    }
