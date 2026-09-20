"""Zhipu GLM implementation of the provider-neutral query-planner port.

The default endpoint is the GLM Coding Plan host so an active subscription's
included quota is consumed instead of pay-as-you-go balance. Point
GLM_API_URL at https://open.bigmodel.cn/api/paas/v4/chat/completions to bill
account balance instead.
"""

from typing import Any, Optional

from china_a_share.planners.deepseek import DeepSeekQueryPlanner


GLM_PLANNER_NAME = "glm"
GLM_CODING_API_URL = (
    "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
)
GLM_PAY_AS_YOU_GO_API_URL = (
    "https://open.bigmodel.cn/api/paas/v4/chat/completions"
)
# Flash answers routine planning attempts cheaply; the flagship model is
# reserved for the final recovery attempts, mirroring the DeepSeek escalation.
GLM_MODEL = "glm-5.3-flash"
GLM_FALLBACK_MODEL = "glm-5.3"


class GlmQueryPlanner(DeepSeekQueryPlanner):
    """Convert natural language into a query plan using Zhipu GLM."""

    def __init__(
        self,
        api_key: str,
        session: Optional[Any] = None,
        *,
        api_url: str = GLM_CODING_API_URL,
        model: str = GLM_MODEL,
        fallback_model: str = GLM_FALLBACK_MODEL,
    ) -> None:
        """Reuse the shared planning engine with GLM endpoint defaults."""
        super().__init__(
            api_key,
            session=session,
            api_url=api_url,
            model=model,
            fallback_model=fallback_model,
            provider=GLM_PLANNER_NAME,
            label="GLM",
        )
