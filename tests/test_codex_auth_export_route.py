import asyncio
import io
import json
import zipfile
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import src.web.routes.accounts as accounts_routes
from src.database import crud
from src.database.session import DatabaseSessionManager
from src.web.routes.accounts import BatchExportRequest


async def _read_streaming_response_body(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        else:
            chunks.append(chunk.encode("utf-8"))
    return b"".join(chunks)


def _build_fake_get_db(manager):
    @contextmanager
    def fake_get_db():
        with manager.session_scope() as session:
            yield session

    return fake_get_db


def test_export_codex_auth_single_account_uses_auth_json_filename(tmp_path, monkeypatch):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/single.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="single@example.com",
            email_service="tempmail",
            access_token="access-token",
            refresh_token="refresh-token",
            id_token="id-token",
            account_id="acct-1",
            extra_data={"codex_auth": {"generated": True}},
        )
        account_id = account.id

    monkeypatch.setattr(accounts_routes, "get_db", _build_fake_get_db(manager))

    response = asyncio.run(
        accounts_routes.export_accounts_codex_auth(
            BatchExportRequest(ids=[account_id]),
        )
    )
    body = asyncio.run(_read_streaming_response_body(response))

    assert response.headers["content-disposition"] == "attachment; filename=auth.json"
    assert json.loads(body.decode("utf-8")) == {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id-token",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "account_id": "acct-1",
        },
        "last_refresh": "",
    }


def test_export_codex_auth_multiple_accounts_zip_each_auth_json_under_email_directory(tmp_path, monkeypatch):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/multi.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        first = crud.create_account(
            session,
            email="first@example.com",
            email_service="tempmail",
            access_token="first-access",
            refresh_token="first-refresh",
            id_token="first-id",
            account_id="acct-first",
            extra_data={"codex_auth": {"generated": True}},
        )
        second = crud.create_account(
            session,
            email="second@example.com",
            email_service="tempmail",
            access_token="second-access",
            refresh_token="second-refresh",
            id_token="second-id",
            account_id="acct-second",
            extra_data={"codex_auth": {"generated": True}},
        )
        account_ids = [first.id, second.id]

    monkeypatch.setattr(accounts_routes, "get_db", _build_fake_get_db(manager))

    response = asyncio.run(
        accounts_routes.export_accounts_codex_auth(
            BatchExportRequest(ids=account_ids),
        )
    )
    body = asyncio.run(_read_streaming_response_body(response))

    with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
        assert sorted(zf.namelist()) == [
            "first@example.com/auth.json",
            "second@example.com/auth.json",
        ]

        first_auth = json.loads(zf.read("first@example.com/auth.json").decode("utf-8"))
        second_auth = json.loads(zf.read("second@example.com/auth.json").decode("utf-8"))

    assert first_auth["tokens"]["access_token"] == "first-access"
    assert second_auth["tokens"]["access_token"] == "second-access"
    assert response.headers["content-disposition"].startswith("attachment; filename=codex_auth_")


def test_export_codex_auth_requires_manual_generation_first(tmp_path, monkeypatch):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/missing-marker.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="plain@example.com",
            email_service="tempmail",
            access_token="plain-access",
            refresh_token="plain-refresh",
            id_token="plain-id",
            account_id="acct-plain",
        )
        account_id = account.id

    monkeypatch.setattr(accounts_routes, "get_db", _build_fake_get_db(manager))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            accounts_routes.export_accounts_codex_auth(
                BatchExportRequest(ids=[account_id]),
            )
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "以下账号尚未生成 Codex Auth，请先在账号管理中点击「Codex Auth 登录」后再导出：plain@example.com"
    )


def test_persist_codex_auth_result_marks_account_generated(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/persist-marker.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="marked@example.com",
            email_service="tempmail",
            access_token="old-access",
            refresh_token="old-refresh",
            id_token="old-id",
            account_id="acct-old",
            extra_data={"note": "keep-me"},
        )
        account_id = account.id

    with manager.session_scope() as session:
        accounts_routes._persist_codex_auth_result(
            session,
            account_id=account_id,
            auth_json={
                "tokens": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "id_token": "new-id",
                    "account_id": "acct-new",
                }
            },
            workspace_id="ws-new",
        )

    with manager.session_scope() as session:
        account = crud.get_account_by_id(session, account_id)
        assert account is not None
        assert account.access_token == "new-access"
        assert account.refresh_token == "new-refresh"
        assert account.id_token == "new-id"
        assert account.account_id == "acct-new"
        assert account.workspace_id == "ws-new"
        assert account.extra_data["note"] == "keep-me"
        assert account.extra_data["codex_auth"]["generated"] is True
        assert account.extra_data["codex_auth"]["workspace_id"] == "ws-new"
        assert account.extra_data["codex_auth"]["generated_at"]
