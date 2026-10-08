"""Runtime configuration contract tests (text2sql_V1.md section 7)."""

from __future__ import annotations

import pytest

from text2sql.config import ConfigurationError, Settings


def test_defaults_match_document_contract():
    settings = Settings(db_password="secret")
    assert settings.sql_dialect == "mysql"
    assert settings.sql_max_rows == 100
    assert settings.sql_timeout_seconds == 30
    assert settings.sql_max_retries == 2
    assert settings.catalog_version == "v1"


def test_settings_loaded_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DB_HOST=db.internal",
                "DB_PORT=3307",
                "DB_NAME=demo",
                "DB_USER=reader",
                "DB_PASSWORD=p@ss word",
                "SQL_MAX_ROWS=50",
                "SQL_MAX_RETRIES=1",
                "SQL_TIMEOUT_SECONDS=5",
                "CATALOG_VERSION=v2",
                "LLM_MODEL=deepseek-chat",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings.load(env_file)
    assert settings.db_host == "db.internal"
    assert settings.db_port == 3307
    assert settings.sql_max_rows == 50
    assert settings.sql_max_retries == 1
    assert settings.catalog_version == "v2"


def test_connection_url_shape_and_masking():
    settings = Settings(db_user="text2sql_readonly", db_password="p@ss word")
    url = settings.sqlalchemy_url()
    assert url.startswith("mysql+pymysql://text2sql_readonly:")
    assert url.endswith("/ecommerce_text2sql?charset=utf8mb4")
    assert "p%40ss+word" in url, "密码必须被 URL 编码"
    masked = settings.sqlalchemy_url(mask_password=True)
    assert "p%40ss+word" not in masked and "***" in masked


def test_secrets_are_redacted_from_messages():
    settings = Settings(db_password="topsecret", llm_api_key="sk-abc123")
    text = settings.redact(
        "connect failed: mysql+pymysql://user:topsecret@host/db api_key=sk-abc123"
    )
    assert "topsecret" not in text
    assert "sk-abc123" not in text
    assert "***" in text


def test_describe_never_leaks_secrets():
    settings = Settings(db_password="topsecret", llm_api_key="sk-abc123")
    described = str(settings.describe())
    assert "topsecret" not in described
    assert "sk-abc123" not in described


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"sql_dialect": "postgres"}, "SQL_DIALECT"),
        ({"sql_max_rows": 0}, "SQL_MAX_ROWS"),
        ({"sql_timeout_seconds": 0}, "SQL_TIMEOUT_SECONDS"),
        ({"sql_max_retries": -1}, "SQL_MAX_RETRIES"),
        ({"sql_limit_policy": "whatever"}, "SQL_LIMIT_POLICY"),
        ({"catalog_source": "vector"}, "CATALOG_SOURCE"),
    ],
)
def test_invalid_configuration_is_rejected(overrides, message):
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(**overrides).validate()
    assert message in str(excinfo.value)
