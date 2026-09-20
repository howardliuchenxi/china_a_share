"""Execute configured strategies and deliver one Feishu card per strategy."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
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
from china_a_share.discovery.strategy_store import StrategyStore
from china_a_share.observability import log_event


STRATEGY_SCAN_API_ROUTE = "/api/analysis/tasks/strategy:daily-scan"
FEISHU_AT_ALL_PERMISSION_ERROR_CODE = "230006"
SHANGHAI_TIME_ZONE = ZoneInfo("Asia/Shanghai")
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
    ) -> None:
        self._loader = loader
        self._engine = engine
        self._store = store
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIME_ZONE))

    def run_daily_scan(self, request_id: str) -> None:
        """Run all enabled strategies and expose partial failures to the scheduler."""
        strategies = self._store.list_enabled_strategies()
        if not strategies:
            return
        requested_date = self._scan_date()
        frame = self._load_shared_history(strategies, requested_date, request_id)
        if frame.empty or str(frame["trade_date"].max()) != requested_date:
            log_event(
                logger,
                logging.INFO,
                "strategy_scan_non_trading_day",
                request_id=request_id,
                requested_date=requested_date,
                latest_trade_date=(str(frame["trade_date"].max()) if not frame.empty else ""),
            )
            return
        self._run_strategies(
            strategies,
            frame,
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
        requested_date = self._scan_date()
        frame = self._load_shared_history(strategies, requested_date, request_id)
        if frame.empty:
            raise RuntimeError("Strategy preview returned no market data.")
        self._run_strategies(
            strategies,
            frame,
            signal_date=str(frame["trade_date"].max()),
            request_id=request_id,
            preview=True,
        )

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
) -> dict[str, Any]:
    """Build one self-contained result card for exactly one strategy."""
    direction = "买入" if strategy.direction == SignalDirection.BUY else "卖出"
    if stock_codes:
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
