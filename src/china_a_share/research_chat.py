"""Independent conversational research tools built on the market-data port."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
import json
import re
from typing import Optional

import pandas as pd

from china_a_share.core.ports import MarketDataProvider
from china_a_share.feishu import FeishuConversationTurn


FEISHU_API_ROUTE = "/api/integrations/feishu/events"
LIMIT_UP_TOOL_NAME = "limit_up_outcome_probability"
MONTH_PATTERN = re.compile(r"(?:(20\d{2})年)?\s*(1[0-2]|[1-9])月")
YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
RANGE_PATTERN = re.compile(r"(1[0-2]|[1-9])\s*[—–~至到-]\s*(1[0-2]|[1-9])月")
CONSECUTIVE_PATTERN = re.compile(r"连续\s*([2-9])\s*(?:天|个交易日)?涨停")
OBSERVATION_PATTERN = re.compile(r"第\s*([3-9])\s*(?:天|个交易日)")


@dataclass(frozen=True)
class LimitUpStudyRequest:
    """Validated inputs for one consecutive-limit-up outcome study."""

    year: int
    start_month: int
    end_month: int
    consecutive_sessions: int = 2
    observation_offset: int = 1
    exclude_one_price: bool = True

    def __post_init__(self) -> None:
        if not 2000 <= self.year <= 2100:
            raise ValueError("Research year is outside the supported range.")
        if not 1 <= self.start_month <= self.end_month <= 12:
            raise ValueError("Research months must form one ascending calendar range.")
        if self.consecutive_sessions != 2 or self.observation_offset != 1:
            raise ValueError(
                "The first research tool supports two limit-up sessions and the next trading day."
            )

    def serialize(self) -> str:
        """Return stable context that later turns can refine without guessing."""
        return json.dumps(
            {
                "tool": LIMIT_UP_TOOL_NAME,
                "year": self.year,
                "start_month": self.start_month,
                "end_month": self.end_month,
                "consecutive_sessions": self.consecutive_sessions,
                "observation_offset": self.observation_offset,
                "exclude_one_price": self.exclude_one_price,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


class LocalResearchConversationService:
    """Compile bounded chat turns into deterministic provider-backed studies."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider

    def answer(
        self,
        request_id: str,
        prompt: str,
        conversation: list[FeishuConversationTurn],
    ) -> tuple[str, str]:
        """Execute one supported study or return a precise capability boundary."""
        study = compile_limit_up_study(prompt, conversation)
        if study is None:
            return (
                "当前飞书研究工具仅支持连续两日涨停后第三个交易日的月度统计。"
                "请包含年份和月份，例如：统计2026年1-8月二连板后第三日上涨概率。",
                json.dumps({"tool": "unsupported", "prompt": prompt}, ensure_ascii=False),
            )
        rows = run_limit_up_study(self._provider, study, request_id=request_id)
        return format_limit_up_study(rows, study, request_id), study.serialize()


def compile_limit_up_study(
    prompt: str,
    conversation: list[FeishuConversationTurn],
) -> Optional[LimitUpStudyRequest]:
    """Resolve an explicit request or a bounded refinement of the prior study."""
    normalized = prompt.replace("二连板", "连续2天涨停")
    prior = _latest_limit_up_context(conversation)
    mentions_pattern = "涨停" in normalized or "连板" in prompt
    if not mentions_pattern and prior is None:
        return None

    year_match = YEAR_PATTERN.search(normalized)
    year = int(year_match.group(1)) if year_match else (prior.year if prior else 0)
    month_range = RANGE_PATTERN.search(normalized)
    month_matches = MONTH_PATTERN.findall(normalized)
    if month_range:
        start_month, end_month = map(int, month_range.groups())
    elif month_matches:
        start_month = int(month_matches[0][1])
        end_month = start_month
    elif prior is not None:
        start_month, end_month = prior.start_month, prior.end_month
    else:
        return None

    consecutive_match = CONSECUTIVE_PATTERN.search(normalized)
    consecutive_sessions = (
        int(consecutive_match.group(1))
        if consecutive_match
        else (prior.consecutive_sessions if prior else 2)
    )
    observation_match = OBSERVATION_PATTERN.search(normalized)
    observation_day = int(observation_match.group(1)) if observation_match else 3
    exclude_one_price = prior.exclude_one_price if prior else True
    if "不剔除一字板" in normalized or "包含一字板" in normalized:
        exclude_one_price = False
    elif "剔除一字板" in normalized or "非一字板" in normalized:
        exclude_one_price = True
    try:
        return LimitUpStudyRequest(
            year=year,
            start_month=start_month,
            end_month=end_month,
            consecutive_sessions=consecutive_sessions,
            observation_offset=observation_day - consecutive_sessions,
            exclude_one_price=exclude_one_price,
        )
    except ValueError:
        return None


def _latest_limit_up_context(
    conversation: list[FeishuConversationTurn],
) -> Optional[LimitUpStudyRequest]:
    """Read the latest validated tool arguments instead of replaying prose."""
    for turn in reversed(conversation):
        try:
            payload = json.loads(turn.interpretation)
            if payload.get("tool") != LIMIT_UP_TOOL_NAME:
                continue
            return LimitUpStudyRequest(
                year=int(payload["year"]),
                start_month=int(payload["start_month"]),
                end_month=int(payload["end_month"]),
                consecutive_sessions=int(payload["consecutive_sessions"]),
                observation_offset=int(payload["observation_offset"]),
                exclude_one_price=bool(payload["exclude_one_price"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def run_limit_up_study(
    provider: MarketDataProvider,
    study: LimitUpStudyRequest,
    *,
    request_id: str,
) -> list[dict]:
    """Calculate monthly outcomes from native limit lists and daily prices."""
    first_calendar_day = date(study.year, study.start_month, 1)
    last_calendar_day = date(
        study.year,
        study.end_month,
        calendar.monthrange(study.year, study.end_month)[1],
    )
    start = first_calendar_day.strftime("%Y%m%d")
    end = last_calendar_day.strftime("%Y%m%d")
    calendar_start = (first_calendar_day - timedelta(days=20)).strftime("%Y%m%d")
    calendar_end = (last_calendar_day + timedelta(days=20)).strftime("%Y%m%d")
    calendar = provider.query(
        "trade_cal",
        {"exchange": "", "start_date": calendar_start, "end_date": calendar_end, "is_open": "1"},
        ["cal_date"],
        api_route=FEISHU_API_ROUTE,
        request_id=request_id,
        query_id="research_trade_calendar",
    )
    trade_dates = sorted(calendar["cal_date"].astype(str).tolist())
    positions = {value: index for index, value in enumerate(trade_dates)}
    limits = provider.query(
        "limit_list_d",
        {"start_date": calendar_start, "end_date": end, "limit_type": "U"},
        ["trade_date", "ts_code", "name"],
        api_route=FEISHU_API_ROUTE,
        request_id=request_id,
        query_id="research_limit_ups",
    )
    limits["trade_date"] = limits["trade_date"].astype(str)
    limit_pairs = set(zip(limits["ts_code"], limits["trade_date"]))
    events = []
    for record in limits.itertuples(index=False):
        position = positions.get(record.trade_date)
        if position is None or position < 2 or not start <= record.trade_date <= end:
            continue
        first_day = trade_dates[position - 1]
        if (record.ts_code, first_day) not in limit_pairs:
            continue
        if (record.ts_code, trade_dates[position - 2]) in limit_pairs:
            continue
        events.append(
            {
                "ts_code": record.ts_code,
                "day1": first_day,
                "day2": record.trade_date,
                "day3": trade_dates[position + 1],
                "month": record.trade_date[:6],
            }
        )
    frame = pd.DataFrame(events).drop_duplicates(["ts_code", "day2"])
    if frame.empty:
        return []
    required_dates = sorted(set(frame["day1"]) | set(frame["day2"]) | set(frame["day3"]))
    daily_frames = []
    for trade_date in required_dates:
        daily_frames.append(
            provider.query(
                "daily",
                {"trade_date": trade_date},
                ["ts_code", "trade_date", "high", "low", "close", "pre_close", "pct_chg"],
                api_route=FEISHU_API_ROUTE,
                request_id=request_id,
                query_id=f"research_daily_{trade_date}",
            )
        )
    daily = pd.concat(daily_frames, ignore_index=True)
    outcomes = daily[["ts_code", "trade_date", "close", "pre_close", "pct_chg"]]
    frame = frame.merge(outcomes, left_on=["ts_code", "day3"], right_on=["ts_code", "trade_date"], how="left")
    frame = frame[frame["close"].notna()].copy()
    frame["up"] = frame["close"] > frame["pre_close"]
    boards = daily.merge(frame[["ts_code", "day1", "day2"]], on="ts_code", how="inner")
    boards = boards[(boards["trade_date"] == boards["day1"]) | (boards["trade_date"] == boards["day2"])].copy()
    boards["one_price"] = (boards["high"] - boards["low"]).abs() < 0.001
    flags = boards.groupby(["ts_code", "day2"])["one_price"].any()
    frame["one_price"] = [bool(flags.get((row.ts_code, row.day2), False)) for row in frame.itertuples(index=False)]
    if study.exclude_one_price:
        frame = frame[~frame["one_price"]]
    results = []
    for month in range(study.start_month, study.end_month + 1):
        monthly = frame[frame["month"] == f"{study.year}{month:02d}"]
        results.append(
            {
                "month": month,
                "events": int(len(monthly)),
                "up": int(monthly["up"].sum()),
                "probability": float(monthly["up"].mean()) if len(monthly) else None,
                "mean_return": float(monthly["pct_chg"].mean()) if len(monthly) else None,
                "median_return": float(monthly["pct_chg"].median()) if len(monthly) else None,
            }
        )
    return results


def format_limit_up_study(
    rows: list[dict],
    study: LimitUpStudyRequest,
    request_id: str,
) -> str:
    """Render one compact Chinese evidence table for Feishu."""
    scope = "已剔除前两板中的一字板" if study.exclude_one_price else "包含一字板"
    lines = [f"{study.year}年{study.start_month}-{study.end_month}月二连板第三日表现（{scope}）"]
    lines.append("月份｜事件｜收涨｜概率｜平均｜中位数")
    for row in rows:
        if row["probability"] is None:
            lines.append(f"{row['month']}月｜0｜0｜无样本｜—｜—")
            continue
        lines.append(
            f"{row['month']}月｜{row['events']}｜{row['up']}｜"
            f"{row['probability']:.2%}｜{row['mean_return']:+.2f}%｜"
            f"{row['median_return']:+.2f}%"
        )
    lines.append(f"请求编号：{request_id}")
    return "\n".join(lines)
