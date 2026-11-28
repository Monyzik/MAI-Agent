import asyncio
import logging
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app import mcp_tools
from app.hermes_client import HermesClientManager, HermesUnavailableError
from app.safe_logging import sanitize_fields
from app.telegram_service import TelegramService


def test_allowlist_drops_sensitive_and_unknown_fields() -> None:
    cleaned = sanitize_fields(
        {
            "event": "test_event",
            "channel": "telegram",
            "outcome": "success",
            "text": "Иванов Иван Иванович паспорт 4510 123456",
            "full_name": "Петров Пётр",
            "date_of_birth": "2007-05-12",
            "candidate_id": "SYN-1001",
            "sql": "SELECT * FROM applicants",
            "data": "base64imagedata",
            "chat_id": "1497275653",
            "session_id": "bot123456:AAFakeSecretTokenValue",
        }
    )
    assert cleaned["event"] == "test_event"
    assert cleaned["session_id"] == "bot<REDACTED>"
    for forbidden in ("text", "full_name", "date_of_birth", "candidate_id", "sql", "data", "chat_id"):
        assert forbidden not in cleaned


def test_telegram_logs_success_rejection_and_error_without_dialog_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    service = TelegramService()
    secret_token = "999888777:AASecretTokenLeakCheck123"
    secret_dialog = "Секретный текст анкеты абитуриента Смирнов Алексей 2008-01-15"

    # 1. Success path
    with (
        patch.object(service, "_ensure_session", AsyncMock(return_value=("sess-1", "hermes-1"))),
        patch("app.telegram_service.hermes_manager.prompt", AsyncMock(return_value=("Ответ агента", "hermes-1"))),
        patch.object(service, "_send", AsyncMock()),
    ):
        asyncio.run(
            service._handle_update(
                secret_token,
                {"update_id": 101, "message": {"chat": {"id": 777001}, "text": secret_dialog}},
            )
        )

    # 2. Rejected path (unsupported document)
    with patch.object(service, "_send", AsyncMock()):
        asyncio.run(
            service._handle_update(
                secret_token,
                {
                    "update_id": 102,
                    "message": {
                        "chat": {"id": 777001},
                        "document": {"file_name": "anketa_secret.pdf"},
                    },
                },
            )
        )

    # 3. Error path (Hermes unavailable)
    with (
        patch.object(service, "_ensure_session", AsyncMock(return_value=("sess-1", "hermes-1"))),
        patch(
            "app.telegram_service.hermes_manager.prompt",
            AsyncMock(side_effect=HermesUnavailableError("ACP down")),
        ),
        patch.object(service, "_send", AsyncMock()),
    ):
        asyncio.run(
            service._handle_update(
                secret_token,
                {"update_id": 103, "message": {"chat": {"id": 777001}, "text": secret_dialog}},
            )
        )

    logs = caplog.text
    assert "outcome=success" in logs
    assert "outcome=rejected" in logs
    assert "reason_code=document_unsupported" in logs
    assert "outcome=error" in logs
    assert "reason_code=hermes_unavailable" in logs

    assert secret_dialog not in logs
    assert "Смирнов" not in logs
    assert "anketa_secret.pdf" not in logs
    assert secret_token not in logs
    assert "777001" not in logs
def test_mcp_tools_logs_success_rejection_and_error_without_applicant_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    secret_name = "Козлов Дмитрий Сергеевич"
    secret_dob = date(2007, 9, 10)
    secret_sql = "SELECT * FROM applicants WHERE full_name = 'Козлов Дмитрий Сергеевич'"

    class DummyContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    search_fn = getattr(mcp_tools.search_synthetic_ege_results, "fn", mcp_tools.search_synthetic_ege_results)
    sql_fn = getattr(mcp_tools.sql_query, "fn", mcp_tools.sql_query)

    # 1. Success path
    with (
        patch.object(mcp_tools, "FisSessionLocal", return_value=DummyContext()),
        patch.object(
            mcp_tools,
            "search_synthetic_candidates",
            return_value={
                "match_count": 1,
                "ambiguous": False,
                "matches": [
                    {
                        "candidate_id": "SYN-9001",
                        "full_name": secret_name,
                        "date_of_birth": secret_dob,
                        "results": [{"subject": "Математика", "year": 2026, "score": 94}],
                    }
                ],
            },
        ),
    ):
        search_fn(secret_name, secret_dob)

    # 2. Rejected path (multi-statement SQL)
    with pytest.raises(ValueError):
        sql_fn(f"{secret_sql}; DROP TABLE applicants")

    # 3. Error path (DB execution failure)
    with (
        patch.object(mcp_tools, "OwnSessionLocal", return_value=DummyContext()),
        patch.object(mcp_tools, "run_sql", side_effect=RuntimeError("db failure")),
        pytest.raises(RuntimeError),
    ):
        sql_fn(secret_sql)

    logs = caplog.text
    assert "tool_name=search_synthetic_ege_results" in logs
    assert "outcome=success" in logs
    assert "match_count=1" in logs
    assert "outcome=rejected" in logs
    assert "reason_code=multiple_statements" in logs
    assert "outcome=error" in logs

    assert secret_name not in logs
    assert "2007-09-10" not in logs
    assert "SYN-9001" not in logs
    assert "DROP TABLE" not in logs
    assert "applicants" not in logs


def test_acp_client_logs_rejection_without_payload(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    manager = HermesClientManager()
    with (
        patch("app.hermes_client.get_settings") as mock_settings,
        pytest.raises(HermesUnavailableError),
    ):
        mock_settings.return_value.acp_transport = "http"
        mock_settings.return_value.hermes_max_processes = 4
        asyncio.run(manager.start_session("sess-acp-1"))

    assert "channel=acp" in caplog.text
    assert "outcome=rejected" in caplog.text
    assert "reason_code=unsupported_transport" in caplog.text
