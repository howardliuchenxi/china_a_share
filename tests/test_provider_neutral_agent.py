from pathlib import Path

from china_a_share.codex_agent import CODEX_TURN_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[1]


def test_feishu_agent_runtime_contains_no_model_vendor_customization():
    paths = [
        ROOT / "src/china_a_share/feishu_agent.py",
        ROOT / "src/china_a_share/model_client.py",
        ROOT / "src/china_a_share/research_sandbox.py",
    ]

    for path in paths:
        assert "deepseek" not in path.read_text(encoding="utf-8").casefold()


def test_research_sandbox_deployment_is_private_and_secretless():
    configuration = (ROOT / "cloudbuild.reconcile.yaml").read_text(
        encoding="utf-8"
    )
    sandbox_block = configuration.split("- id: deploy-research-sandbox", 1)[1]
    sandbox_block = sandbox_block.split("- id: deploy-service", 1)[0]

    assert "--no-allow-unauthenticated" in sandbox_block
    assert "--clear-env-vars" in sandbox_block
    assert "--clear-secrets" in sandbox_block
    assert "--set-secrets" not in sandbox_block
    assert "${_SANDBOX_SERVICE_ACCOUNT}" in sandbox_block
    assert "serviceAccount:${_RUNTIME_SERVICE_ACCOUNT}" in sandbox_block


def test_worker_deployment_uses_generic_model_configuration():
    configuration = (ROOT / "cloudbuild.reconcile.yaml").read_text(
        encoding="utf-8"
    )
    worker_block = configuration.split("- id: deploy-worker", 1)[1]
    worker_block = worker_block.split("- id: verify-deployment", 1)[0]

    assert "LLM_BASE_URL=${_LLM_BASE_URL}" in worker_block
    assert "LLM_MODEL=${_LLM_MODEL}" in worker_block
    assert "LLM_API_KEY=deepseek-api-key:latest" in worker_block
    assert "RESEARCH_SANDBOX_URL=" in worker_block
    assert "PUBLIC_APP_URL=${_SERVICE_URL}" in worker_block
    assert "MASSIVE_API_KEY=massive-api-key:latest" in worker_block
    assert "FINNHUB_API_KEY=finnhub-api-key:latest" in worker_block


def test_worker_timeout_exceeds_the_six_hour_codex_turn():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    configuration = (ROOT / "cloudbuild.reconcile.yaml").read_text(
        encoding="utf-8"
    )
    worker_block = configuration.split("- id: deploy-worker", 1)[1]
    worker_block = worker_block.split("- id: verify-deployment", 1)[0]

    assert CODEX_TURN_TIMEOUT_SECONDS == 6 * 60 * 60
    assert "ANALYSIS_JOB_TIMEOUT := 7h" in makefile
    assert "--task-timeout 7h" in worker_block
