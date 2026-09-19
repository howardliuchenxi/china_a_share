import json
import logging
from typing import Dict, List, Optional
from datetime import datetime, timezone
from google.cloud import storage

from .models import StrategyConfig, StrategyDraft
from china_a_share.cache import CloudStorageDataCacheStore

logger = logging.getLogger(__name__)

class StrategyStore:
    def __init__(self, bucket_name: str, storage_client: Optional[storage.Client] = None):
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)
        self._prefix_config = "strategy/config/"
        self._prefix_draft = "strategy/draft/"
        self._prefix_history = "strategy/history/"
        
    def _object_name(self, prefix: str, obj_id: str) -> str:
        return f"{prefix}{obj_id}.json"

    def put_strategy(self, strategy: StrategyConfig) -> None:
        strategy.validate_strategy()
        blob = self._bucket.blob(self._object_name(self._prefix_config, strategy.id))
        blob.upload_from_string(strategy.model_dump_json())

    def get_strategy(self, strategy_id: str) -> Optional[StrategyConfig]:
        blob = self._bucket.blob(self._object_name(self._prefix_config, strategy_id))
        if not blob.exists():
            return None
        return StrategyConfig.model_validate_json(blob.download_as_text())

    def list_strategies(self) -> List[StrategyConfig]:
        strategies = []
        blobs = self._bucket.list_blobs(prefix=self._prefix_config)
        for blob in blobs:
            try:
                strategies.append(StrategyConfig.model_validate_json(blob.download_as_text()))
            except Exception as e:
                logger.error(f"Failed to load strategy {blob.name}: {e}")
        return strategies

    def put_draft(self, draft: StrategyDraft) -> None:
        blob = self._bucket.blob(self._object_name(self._prefix_draft, draft.id))
        blob.upload_from_string(draft.model_dump_json())

    def get_draft(self, draft_id: str) -> Optional[StrategyDraft]:
        blob = self._bucket.blob(self._object_name(self._prefix_draft, draft_id))
        if not blob.exists():
            return None
        return StrategyDraft.model_validate_json(blob.download_as_text())
        
    def list_drafts(self, creator_id: str) -> List[StrategyDraft]:
        drafts = []
        blobs = self._bucket.list_blobs(prefix=self._prefix_draft)
        for blob in blobs:
            try:
                draft = StrategyDraft.model_validate_json(blob.download_as_text())
                if draft.creator_id == creator_id:
                    drafts.append(draft)
            except Exception as e:
                logger.error(f"Failed to load draft {blob.name}: {e}")
        return drafts
        
    def delete_draft(self, draft_id: str) -> None:
        blob = self._bucket.blob(self._object_name(self._prefix_draft, draft_id))
        if blob.exists():
            blob.delete()

    def check_and_mark_executed(self, strategy_id: str, stock_code: str, signal_date: str) -> bool:
        """
        Atomically check and mark execution to prevent duplicate notifications.
        Returns True if this is the first execution, False if already executed.
        """
        record_id = f"{strategy_id}_{stock_code}_{signal_date}"
        blob = self._bucket.blob(self._object_name(self._prefix_history, record_id))
        
        # In GCS, we can use if_generation_match=0 to ensure we only write if it doesn't exist
        try:
            record = {"executed_at": datetime.now(timezone.utc).isoformat()}
            blob.upload_from_string(json.dumps(record), if_generation_match=0)
            return True
        except Exception as e:
            # PreconditionFailed indicates the file already exists
            return False
