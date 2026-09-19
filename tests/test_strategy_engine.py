import pytest
import pandas as pd
from datetime import datetime, timezone
from china_a_share.strategy.models import StrategyConfig, RuleCondition, Operator, StrategyDirection, DataAdjustment
from china_a_share.strategy.engine import RuleEngine

class MockDataLoader:
    def get_adjusted_history(self, start_date: str, end_date: str) -> pd.DataFrame:
        pass

def test_drawdown_rule_passes():
    engine = RuleEngine(MockDataLoader())
    
    # Create mock data: max is 100, current is 65 -> drawdown is 35%
    data = {
        "ts_code": ["000001.SZ"] * 60,
        "trade_date": [(datetime(2023, 1, 1).strftime("%Y%m%d"))] * 60, # Simplified
        "high_qfq": [100.0] + [90.0] * 58 + [70.0],
        "close_qfq": [90.0] * 59 + [65.0]
    }
    # Fix dates to make them unique and sorted
    data["trade_date"] = [(datetime(2023, 1, 1) + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(60)]
    
    df = pd.DataFrame(data)
    
    condition = RuleCondition(
        metric="drawdown", 
        operator=Operator.GT, 
        parameters={"window": 60, "threshold": 0.30}
    )
    
    target_date = data["trade_date"][-1]
    passed, reason, ind = engine._evaluate_condition(condition, df, target_date)
    
    assert bool(passed) is True
    assert ind["drawdown"] == 0.35
    
def test_drawdown_rule_fails_boundary():
    engine = RuleEngine(MockDataLoader())
    
    # max is 100, current is 70 -> drawdown is 30%. Rule is GT (> 30%), so it should fail.
    data = {
        "ts_code": ["000001.SZ"] * 60,
        "high_qfq": [100.0] + [90.0] * 58 + [70.0],
        "close_qfq": [90.0] * 59 + [70.0]
    }
    data["trade_date"] = [(datetime(2023, 1, 1) + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(60)]
    df = pd.DataFrame(data)
    
    condition = RuleCondition(metric="drawdown", operator=Operator.GT, parameters={"window": 60, "threshold": 0.30})
    target_date = data["trade_date"][-1]
    
    passed, reason, ind = engine._evaluate_condition(condition, df, target_date)
    assert bool(passed) is False
    assert ind["drawdown"] == 0.30
    
def test_cumulative_return_passes_boundary():
    engine = RuleEngine(MockDataLoader())
    
    # 11 days of data. We need to compare day 11 to day 1.
    # Start price (index 0) = 100. Current close (index 10) = 110. Return = 10%.
    data = {
        "ts_code": ["000001.SZ"] * 11,
        "close_qfq": [100.0] + [105.0] * 9 + [110.0]
    }
    data["trade_date"] = [(datetime(2023, 1, 1) + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(11)]
    df = pd.DataFrame(data)
    
    condition = RuleCondition(metric="cumulative_return", operator=Operator.BETWEEN, parameters={"window": 10, "min": -0.10, "max": 0.10})
    target_date = data["trade_date"][-1]
    
    passed, reason, ind = engine._evaluate_condition(condition, df, target_date)
    assert bool(passed) is True
    assert ind["10d_ret"] == 0.10
    
def test_ma_cross_first_time():
    engine = RuleEngine(MockDataLoader())
    
    # Needs at least 11 days.
    # Let's craft MA5 and MA10 such that today they cross, but yesterday they didn't.
    data = {
        "ts_code": ["000001.SZ"] * 15,
        # Close prices to manipulate MAs
        "close_qfq": [10.0] * 13 + [10.0, 20.0]
    }
    data["trade_date"] = [(datetime(2023, 1, 1) + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(15)]
    df = pd.DataFrame(data)
    
    condition = RuleCondition(metric="ma_cross", operator=Operator.MA_CROSS_UP_FIRST, parameters={"fast": 5, "slow": 10})
    target_date = data["trade_date"][-1]
    
    passed, reason, ind = engine._evaluate_condition(condition, df, target_date)
    assert bool(passed) is True
