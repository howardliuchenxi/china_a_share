from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from china_a_share.core.contracts import AnalysisTaskStatus
from china_a_share.feishu_agent import FeishuAgentRequest, FeishuAgentTask
from china_a_share.tasks import MemoryAnalysisTaskStore
from china_a_share import worker


def test_worker_records_feishu_initialization_failure(monkeypatch):
    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = FeishuAgentTask(
        task_id="agent-task",
        status=AnalysisTaskStatus.QUEUED,
        request=FeishuAgentRequest(
            prompt="Rank recent returns.",
            conversation_id="conversation",
            source_message_id="message",
        ),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    class FakeSettings:
        @staticmethod
        def from_env():
            return SimpleNamespace(
                tushare_cache_bucket="bucket",
                feishu_app_id="",
                feishu_app_secret="",
            )

    monkeypatch.setenv("ANALYSIS_TASK_ID", task.task_id)
    monkeypatch.setattr(worker, "Settings", FakeSettings)
    monkeypatch.setattr(
        worker,
        "CloudStorageAnalysisTaskStore",
        lambda _bucket_name: store,
    )
    monkeypatch.setattr(
        worker,
        "create_feishu_agent_runtime",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("missing worker config")),
    )
    with pytest.raises(RuntimeError, match="missing worker config"):
        worker.main()

    failed = store.get(task.task_id)
    assert failed.status == AnalysisTaskStatus.FAILED
    assert failed.stage == "failed"
    assert failed.error.message == "missing worker config"
