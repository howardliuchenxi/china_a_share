"""Private HTTP boundary for restricted research calculations."""

import logging

from fastapi import FastAPI, HTTPException

from china_a_share.research_sandbox import (
    RestrictedDataFrameRunner,
    SandboxRequest,
    SandboxResponse,
    SandboxValidationError,
)


logger = logging.getLogger(__name__)
app = FastAPI(title="A-Share Research Sandbox", docs_url=None, redoc_url=None)
runner = RestrictedDataFrameRunner()


@app.get("/health")
def health() -> dict[str, str]:
    """Return the private sandbox readiness state."""
    return {"status": "ok"}


@app.post("/v1/execute", response_model=SandboxResponse)
def execute(request: SandboxRequest) -> SandboxResponse:
    """Execute one validated calculation without application credentials."""
    try:
        return runner.run(request)
    except SandboxValidationError as exc:
        logger.warning("research_sandbox_request_rejected error=%s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("research_sandbox_execution_failed")
        raise HTTPException(status_code=500, detail="Sandbox execution failed.") from exc
