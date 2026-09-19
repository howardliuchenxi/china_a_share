from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class StrategyDirection(str, Enum):
    BUY = "buy"
    SELL = "sell"

class DataAdjustment(str, Enum):
    QFQ = "qfq"  # Forward adjusted (前复权)

class Operator(str, Enum):
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    BETWEEN = "between"
    MA_CROSS_UP_FIRST = "ma_cross_up_first"

class RuleCondition(BaseModel):
    metric: str
    operator: Operator
    parameters: Dict[str, Any] = Field(default_factory=dict)
    
    def validate_condition(self):
        if self.operator == Operator.BETWEEN:
            if "min" not in self.parameters or "max" not in self.parameters:
                raise ValueError("Operator 'between' requires 'min' and 'max' parameters.")
            if self.parameters["min"] > self.parameters["max"]:
                raise ValueError("'min' cannot be greater than 'max'.")
        elif self.metric == "drawdown":
            if self.operator != Operator.GT:
                raise ValueError("Drawdown rule requires GT (>) operator.")
            if "window" not in self.parameters or "threshold" not in self.parameters:
                raise ValueError("Drawdown requires 'window' and 'threshold'.")
        elif self.metric == "cumulative_return":
            if self.operator != Operator.BETWEEN:
                raise ValueError("Cumulative return currently expects 'between'.")
            if "window" not in self.parameters:
                raise ValueError("Cumulative return requires 'window'.")
        elif self.operator == Operator.MA_CROSS_UP_FIRST:
            if "fast" not in self.parameters or "slow" not in self.parameters:
                raise ValueError("MA cross requires 'fast' and 'slow' window parameters.")

class StrategyConfig(BaseModel):
    id: str
    name: str
    creator_id: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    direction: StrategyDirection
    data_adjustment: DataAdjustment = DataAdjustment.QFQ
    conditions: List[RuleCondition]
    notify_target: str
    
    def validate_strategy(self):
        if not self.name.strip():
            raise ValueError("Strategy name cannot be empty.")
        if not self.conditions:
            raise ValueError("Strategy must have at least one condition.")
        for cond in self.conditions:
            cond.validate_condition()

class StrategyExecutionRecord(BaseModel):
    strategy_id: str
    stock_code: str
    stock_name: str
    signal_date: str
    direction: StrategyDirection
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hit_reason: str
    price: float
    indicators: Dict[str, Any] = Field(default_factory=dict)

class StrategyScanResult(BaseModel):
    strategy_id: str
    strategy_name: str
    direction: StrategyDirection
    signal_date: str
    scanned_count: int
    hits: List[StrategyExecutionRecord]
    
class StrategyDraft(BaseModel):
    id: str
    creator_id: str
    name: Optional[str] = None
    direction: Optional[StrategyDirection] = None
    notify_target: Optional[str] = None
    conditions: List[RuleCondition] = Field(default_factory=list)
    step: str = "init"
