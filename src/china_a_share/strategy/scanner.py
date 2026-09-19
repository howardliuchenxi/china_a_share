import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from .engine import RuleEngine
from .models import StrategyConfig, StrategyScanResult
from .persistence import StrategyStore
from .data_loader import QFQDataLoader
from china_a_share.feishu import FeishuMessageSender

logger = logging.getLogger(__name__)

class StrategyScanner:
    def __init__(
        self,
        store: StrategyStore,
        data_loader: QFQDataLoader,
        engine: RuleEngine,
        sender: FeishuMessageSender
    ):
        self.store = store
        self.data_loader = data_loader
        self.engine = engine
        self.sender = sender

    def run_daily_scan(self, target_date: str) -> None:
        """
        Execute all enabled strategies against the target date.
        Must strictly deduplicate notifications.
        """
        strategies = [s for s in self.store.list_strategies() if s.enabled]
        if not strategies:
            logger.info("No enabled strategies found for daily scan.")
            return

        # Estimate the window needed. A robust implementation would find the max window across all rules.
        # For our preset, max window is 60. We fetch 120 days to ensure enough trading days.
        start_date = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=120)).strftime("%Y%m%d")
        
        try:
            df = self.data_loader.get_adjusted_history(start_date, target_date)
        except Exception as e:
            logger.error(f"Failed to fetch data for scan: {e}")
            return
            
        if df.empty:
            logger.warning(f"No data available for scan up to {target_date}.")
            return
            
        # Verify if target_date is a valid trading day with data
        if target_date not in df["trade_date"].values:
             logger.warning(f"Target date {target_date} is not in the fetched trading days. Skipping scan.")
             return

        for strategy in strategies:
            try:
                result = self.engine.evaluate(strategy, df, target_date)
                self._dispatch_formal_notification(result, strategy)
            except Exception as e:
                logger.error(f"Scan failed for strategy {strategy.name}: {e}")

    def run_manual_preview(self, strategy: StrategyConfig, target_date: str) -> Optional[StrategyScanResult]:
        """
        Execute one strategy manually. Does NOT deduplicate or record state.
        Always sends the notification card.
        """
        start_date = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=120)).strftime("%Y%m%d")
        try:
            df = self.data_loader.get_adjusted_history(start_date, target_date)
            if df.empty or target_date not in df["trade_date"].values:
                raise ValueError("Data not ready or non-trading day.")
                
            result = self.engine.evaluate(strategy, df, target_date)
            self._send_feishu_card(result, strategy.notify_target, is_preview=True)
            return result
        except Exception as e:
            logger.error(f"Manual preview failed: {e}")
            self._send_error_card(strategy.name, strategy.notify_target, str(e))
            return None

    def _dispatch_formal_notification(self, result: StrategyScanResult, strategy: StrategyConfig) -> None:
        """
        Send notification. Ensure each strategy gets exactly one card, and deduplicate hits.
        """
        # Deduplicate hits
        new_hits = []
        for hit in result.hits:
            if self.store.check_and_mark_executed(strategy.id, hit.stock_code, result.signal_date):
                new_hits.append(hit)
                
        # Update result with only new hits
        result.hits = new_hits
        
        self._send_feishu_card(result, strategy.notify_target, is_preview=False)
        
    def _send_error_card(self, strategy_name: str, target_id: str, error_msg: str) -> None:
        card = {
             "config": {"wide_screen_mode": True},
             "header": {
                 "title": {"tag": "plain_text", "content": f"策略执行失败：{strategy_name}"},
                 "template": "red"
             },
             "elements": [
                 {"tag": "markdown", "content": f"**错误原因：**\n{error_msg}"}
             ]
         }
        try:
             self.sender.send_interactive_card(target_id, card)
        except Exception as e:
             logger.error(f"Failed to send error card: {e}")

    def _send_feishu_card(self, result: StrategyScanResult, target_id: str, is_preview: bool) -> None:
        title_prefix = "[预览] " if is_preview else ""
        header_color = "green" if result.direction == "buy" else "red"
        
        elements = [
            {
                "tag": "markdown",
                "content": f"**扫描日期**：{result.signal_date} | **扫描范围**：{result.scanned_count} 只股票\n**命中数量**：{len(result.hits)}"
            }
        ]
        
        if not result.hits:
             elements.append({"tag": "markdown", "content": "> 本次扫描未命中任何标的。"})
        else:
            for hit in result.hits[:10]: # Limit max displayed to prevent card overflow
                elements.append({"tag": "hr"})
                elements.append({
                    "tag": "markdown",
                    "content": f"**{hit.stock_name} ({hit.stock_code})**\n- **收盘价 (前复权)**: {hit.price:.2f}\n- **命中原因**: {hit.hit_reason}"
                })
            if len(result.hits) > 10:
                 elements.append({"tag": "markdown", "content": f"*(还有 {len(result.hits) - 10} 只标的未在卡片中展示)*"})
                 
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "策略筛选结果，不构成投资建议。"}]})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{title_prefix}策略：{result.strategy_name}｜建议：{result.direction.value.upper()}"
                },
                "template": header_color
            },
            "elements": elements
        }
        
        try:
            self.sender.send_interactive_card(target_id, card)
        except Exception as e:
            logger.error(f"Failed to send strategy card to {target_id}: {e}")
