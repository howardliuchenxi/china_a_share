import pytest
import pandas as pd
from datetime import datetime, timezone
from china_a_share.strategy.models import (
    StrategyConfig, RuleCondition, Operator, StrategyDirection, DataAdjustment, StrategyScanResult
)
from china_a_share.strategy.data_loader import QFQDataLoader
from china_a_share.strategy.engine import RuleEngine
from china_a_share.strategy.scanner import StrategyScanner
from china_a_share.core.ports import MarketDataProvider
from typing import Dict, Any, Sequence

class MockProvider(MarketDataProvider):
    def __init__(self, daily_df, adj_df):
        self.daily_df = daily_df
        self.adj_df = adj_df
    @property
    def name(self): return "mock"
    def search_operations(self, prompt): return []
    def supports(self, op): return True
    def validate_query(self, op, params, fields): pass
    
    def query(self, operation: str, params: Dict[str, Any], fields: Sequence[str], *, api_route: str, request_id: str, query_id: str) -> pd.DataFrame:
        if operation == "daily":
            return self.daily_df.copy()
        if operation == "adj_factor":
            return self.adj_df.copy()
        return pd.DataFrame()

class MockStrategyStore:
    def __init__(self):
        self.executed = set()
        self.strategies = []
    def check_and_mark_executed(self, sid, code, date):
        key = f"{sid}_{code}_{date}"
        if key in self.executed:
            return False
        self.executed.add(key)
        return True
    def list_strategies(self):
        return self.strategies

class MockSender:
    def __init__(self):
        self.cards = []
    def send_interactive_card(self, target, card):
        self.cards.append((target, card))

def test_qfq_data_loader_handles_split():
    # Test a 10-for-10 split (adj_factor doubles)
    # Day 1: price 20, adj 1
    # Day 2 (split): price 10, adj 2
    # Latest adj is 2. Day 1 qfq price should be 20 * (1 / 2) = 10.
    daily_df = pd.DataFrame({
        "ts_code": ["000001.SZ", "000001.SZ"],
        "trade_date": ["20230101", "20230102"],
        "open": [20.0, 10.0], "high": [20.0, 10.0], "low": [20.0, 10.0], "close": [20.0, 10.0],
        "vol": [100, 200], "amount": [1000, 2000]
    })
    adj_df = pd.DataFrame({
        "ts_code": ["000001.SZ", "000001.SZ"],
        "trade_date": ["20230101", "20230102"],
        "adj_factor": [1.0, 2.0]
    })
    provider = MockProvider(daily_df, adj_df)
    loader = QFQDataLoader(provider)
    res = loader.get_adjusted_history("20230101", "20230102")
    
    assert len(res) == 2
    # Check Day 1 close_qfq
    assert res.iloc[0]["close_qfq"] == 10.0
    # Check Day 2 close_qfq
    assert res.iloc[1]["close_qfq"] == 10.0
    
def test_scanner_deduplication_and_isolation():
    # Setup data
    daily_df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 60,
        "trade_date": [(datetime(2023, 1, 1) + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(60)],
        "open": [10.0]*60, "high": [10.0]*60, "low": [10.0]*60, "close": [10.0]*60,
        "vol": [100]*60, "amount": [1000]*60
    })
    adj_df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 60,
        "trade_date": daily_df["trade_date"].copy(),
        "adj_factor": [1.0]*60
    })
    provider = MockProvider(daily_df, adj_df)
    loader = QFQDataLoader(provider)
    engine = RuleEngine(loader)
    store = MockStrategyStore()
    sender = MockSender()
    scanner = StrategyScanner(store, loader, engine, sender)
    
    # Create a dummy strategy that always passes (cumulative return between -1 and 1)
    strat = StrategyConfig(
        id="s1", name="test", creator_id="u1", direction=StrategyDirection.BUY, notify_target="group1",
        conditions=[RuleCondition(metric="cumulative_return", operator=Operator.BETWEEN, parameters={"window": 1, "min": -1.0, "max": 1.0})]
    )
    store.strategies.append(strat)
    
    target_date = daily_df["trade_date"].iloc[-1]
    
    # 1. Run Daily Scan (should hit and notify)
    scanner.run_daily_scan(target_date)
    assert len(sender.cards) == 1
    # Check title does not have [预览]
    assert "[预览]" not in sender.cards[0][1]["header"]["title"]["content"]
    assert "命中数量**：1" in sender.cards[0][1]["elements"][0]["content"]
    
    # 2. Run Daily Scan again (deduplication should kick in, 0 new hits, should send 0-hit card)
    sender.cards.clear()
    scanner.run_daily_scan(target_date)
    assert len(sender.cards) == 1
    assert "本次扫描无新增可通知信号" in sender.cards[0][1]["elements"][1]["content"]
    
    # 3. Run Manual Preview (should ignore deduplication, send hit card with [预览])
    sender.cards.clear()
    scanner.run_manual_preview(strat, target_date)
    assert len(sender.cards) == 1
    assert "[预览]" in sender.cards[0][1]["header"]["title"]["content"]
    assert "命中数量**：1" in sender.cards[0][1]["elements"][0]["content"]

def test_send_card_at_all_fallback():
    # Setup scanner
    store = MockStrategyStore()
    loader = None
    engine = None
    
    class ThrowingSender:
        def __init__(self):
            self.cards = []
            self.first_throw = True
        def send_interactive_card(self, target, card):
            if self.first_throw:
                self.first_throw = False
                raise RuntimeError("230006: bot lack @all permission")
            self.cards.append((target, card))
            
    sender = ThrowingSender()
    scanner = StrategyScanner(store, loader, engine, sender)
    
    res = StrategyScanResult(
        strategy_id="s1", strategy_name="test", direction=StrategyDirection.BUY, 
        signal_date="20230101", scanned_count=1, hits=[]
    )
    scanner._send_feishu_card(res, "target1", False, 0)
    
    # Should catch the error, strip the <at id="all"></at>, and retry
    assert len(sender.cards) == 1
    assert "<at id=\"all\"></at>" not in sender.cards[0][1]["elements"][0]["content"]
