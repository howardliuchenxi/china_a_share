from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SignalDirection(str, Enum):
    """Recommendation attached to a strategy signal."""

    BUY = "buy"
    SELL = "sell"


class DraftState(str, Enum):
    """Guided creation states for strategy configuration drafts."""

    AWAITING_NAME = "awaiting_name"
    AWAITING_DIRECTION = "awaiting_direction"
    AWAITING_RULES = "awaiting_rules"
    READY = "ready"


class BaseRule(BaseModel):
    """Immutable base contract for one supported strategy condition."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DrawdownRule(BaseRule):
    """Require the current close to be below the rolling high by a threshold."""

    type: Literal["drawdown"] = "drawdown"
    window: int = Field(gt=0, description="Trading-day rolling window length.")
    threshold: float = Field(
        gt=0.0,
        lt=1.0,
        description="Exclusive minimum drawdown expressed as a decimal fraction.",
    )


class CumulativeReturnRule(BaseRule):
    """Require the T-minus-N to current-close return to stay within bounds."""

    type: Literal["cumulative_return"] = "cumulative_return"
    window: int = Field(gt=0, description="Number of trading-day intervals in the return.")
    min_return: float = Field(description="Inclusive minimum return as a decimal fraction.")
    max_return: float = Field(description="Inclusive maximum return as a decimal fraction.")

    @model_validator(mode="after")
    def validate_bounds(self) -> "CumulativeReturnRule":
        """Reject an inverted inclusive return interval."""
        if self.min_return > self.max_return:
            raise ValueError("min_return must not exceed max_return")
        return self


class FirstBullishMARule(BaseRule):
    """Require the first day of a rising fast-over-slow moving-average state."""

    type: Literal["first_bullish_ma"] = "first_bullish_ma"
    fast_window: int = Field(gt=0, description="Fast moving-average window length.")
    slow_window: int = Field(gt=0, description="Slow moving-average window length.")

    @model_validator(mode="after")
    def validate_windows(self) -> "FirstBullishMARule":
        """Keep the fast average strictly shorter than the slow average."""
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be less than slow_window")
        return self


class LimitUpRule(BaseRule):
    """Require at least one close-at-limit-up day within the trailing window.

    The limit ratio follows the current exchange regime for the stock's board
    (10% main board, 20% ChiNext / STAR, 30% BSE) and the limit price is the
    previous nominal close multiplied by that ratio and rounded half-up to the
    0.01 tick, exactly as the exchanges compute it. Judgement uses raw
    (unadjusted) closes, so ex-dividend distortions cannot fake a limit-up.
    ST stocks carry a 5% limit that cannot be derived from price data alone;
    their limit-up days are not flagged by this rule.
    """

    type: Literal["limit_up"] = "limit_up"
    window: int = Field(
        default=1,
        ge=1,
        description="Trading-day window scanned for a close-at-limit-up day, inclusive of the current day.",
    )


Rule = Annotated[
    Union[
        DrawdownRule,
        CumulativeReturnRule,
        FirstBullishMARule,
        LimitUpRule,
    ],
    Field(discriminator="type"),
]


class StrategyConfig(BaseModel):
    """Validated rule set that produces one recommendation direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Stable strategy identifier used for persistence.")
    name: str = Field(min_length=1, description="Human-readable strategy name.")
    direction: SignalDirection = Field(description="Recommendation emitted when every rule matches.")
    rules: list[Rule] = Field(min_length=1, description="Conditions combined with logical AND.")

    # Stable business fields required downstream
    creator_open_id: str = Field(min_length=1, description="Feishu open ID of the strategy creator.")
    enabled: bool = Field(description="Flag indicating if the strategy is enabled.")
    notification_chat_id: str = Field(min_length=1, description="Feishu chat ID where notifications are sent.")
    created_at: datetime = Field(description="Timestamp when the strategy was created.")
    updated_at: datetime = Field(description="Timestamp when the strategy was last updated.")

    @model_validator(mode="after")
    def validate_timestamps(self) -> "StrategyConfig":
        """Verify that timestamps are timezone-aware and chronological."""
        if self.created_at.tzinfo is None or self.created_at.tzinfo.utcoffset(self.created_at) is None:
            raise ValueError("created_at must be timezone-aware")
        if self.updated_at.tzinfo is None or self.updated_at.tzinfo.utcoffset(self.updated_at) is None:
            raise ValueError("updated_at must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        return self

    @property
    def required_history_rows(self) -> int:
        """Return the exact trading-row requirement for evaluating the latest day."""
        requirements: list[int] = []
        for rule in self.rules:
            if isinstance(rule, DrawdownRule):
                requirements.append(rule.window)
            elif isinstance(rule, CumulativeReturnRule):
                requirements.append(rule.window + 1)
            elif isinstance(rule, FirstBullishMARule):
                # The prior bullish state compares its averages with the day before it.
                requirements.append(rule.slow_window + 2)
            elif isinstance(rule, LimitUpRule):
                requirements.append(rule.window + 1)
        return max(requirements)


class StrategyDraft(BaseModel):
    """Guided editing session for a strategy configuration, allowing incomplete values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: str = Field(min_length=1, description="Stable draft identifier.")
    owner_open_id: str = Field(min_length=1, description="Feishu open ID of the draft owner.")
    chat_id: str = Field(min_length=1, description="Feishu chat ID where editing takes place.")
    state: DraftState = Field(description="Explicit editing state or step.")

    name: Optional[str] = Field(default=None, description="Optional name under construction.")
    direction: Optional[SignalDirection] = Field(default=None, description="Optional recommendation direction.")
    rules: list[Rule] = Field(default_factory=list, description="Collected strategy rules so far.")

    created_at: datetime = Field(description="Timestamp when editing started.")
    updated_at: datetime = Field(description="Timestamp of the last edit.")

    @model_validator(mode="after")
    def validate_timestamps(self) -> "StrategyDraft":
        """Verify that timestamps are timezone-aware and chronological."""
        if self.created_at.tzinfo is None or self.created_at.tzinfo.utcoffset(self.created_at) is None:
            raise ValueError("created_at must be timezone-aware")
        if self.updated_at.tzinfo is None or self.updated_at.tzinfo.utcoffset(self.updated_at) is None:
            raise ValueError("updated_at must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        return self
