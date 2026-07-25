"""Application configuration."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Union

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime credentials loaded from environment variables."""

    tushare_token: str
    deepseek_api_key: str = ""
    tushare_cache_bucket: str = ""
    # GLM is optional for text-only requests and required for screenshots.
    zai_api_key: str = ""
    google_cloud_project: str = ""
    cloud_run_region: str = "asia-east2"
    analysis_job_name: str = "china-a-share-analysis-worker"

    @classmethod
    def from_env(cls, env_file: Union[str, Path] = ".env") -> "Settings":
        load_dotenv(dotenv_path=env_file, override=False)
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if not token or token == "your_tushare_token_here":
            raise ConfigurationError(
                "TUSHARE_TOKEN is missing. Copy .env.example to .env and add "
                "a valid Tushare token."
            )
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not deepseek_api_key or deepseek_api_key == "your_deepseek_api_key_here":
            raise ConfigurationError(
                "DEEPSEEK_API_KEY is missing. Add a valid DeepSeek API key to .env."
            )
        tushare_cache_bucket = os.getenv("TUSHARE_CACHE_BUCKET", "").strip()
        zai_api_key = os.getenv("ZAI_API_KEY", "").strip()
        if zai_api_key == "your_zai_api_key_here":
            zai_api_key = ""
        google_cloud_project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        cloud_run_region = os.getenv("CLOUD_RUN_REGION", "asia-east2").strip()
        analysis_job_name = os.getenv(
            "ANALYSIS_JOB_NAME",
            "china-a-share-analysis-worker",
        ).strip()
        return cls(
            tushare_token=token,
            deepseek_api_key=deepseek_api_key,
            tushare_cache_bucket=tushare_cache_bucket,
            zai_api_key=zai_api_key,
            google_cloud_project=google_cloud_project,
            cloud_run_region=cloud_run_region,
            analysis_job_name=analysis_job_name,
        )
