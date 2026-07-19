import pytest

from china_a_share.config import ConfigurationError, Settings


def test_settings_reads_credentials_from_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TUSHARE_CACHE_BUCKET", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TUSHARE_TOKEN=abc123\n"
        "DEEPSEEK_API_KEY=deepseek123\n"
        "TUSHARE_CACHE_BUCKET=test-cache-bucket\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)
    assert settings.tushare_token == "abc123"
    assert settings.deepseek_api_key == "deepseek123"
    assert settings.tushare_cache_bucket == "test-cache-bucket"


def test_settings_rejects_missing_token(monkeypatch, tmp_path):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TUSHARE_CACHE_BUCKET", raising=False)

    with pytest.raises(ConfigurationError):
        Settings.from_env(tmp_path / "missing.env")
