from types import SimpleNamespace

import src.core.codex_auth as codex_auth_module
from src.core.codex_auth import CodexAuthEngine
from src.core.register import PhaseResult, RegistrationEngine
from src.services import EmailServiceType


class DummySettings:
    openai_client_id = "client-id"
    openai_auth_url = "https://auth.example.test/oauth/authorize"
    openai_token_url = "https://auth.example.test/oauth/token"


class FakeEmailService:
    service_type = EmailServiceType.TEMPMAIL


class FakeResponse:
    def __init__(self, *, status_code=200, url="", text=""):
        self.status_code = status_code
        self.url = url
        self.text = text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return self.response


def _build_engine(monkeypatch):
    monkeypatch.setattr(codex_auth_module, "get_settings", lambda: DummySettings())
    return CodexAuthEngine(
        email="tester@example.com",
        password="Pass12345",
        email_service=FakeEmailService(),
        email_service_id="svc-1",
    )


def test_codex_auth_run_reuses_working_login_flow_without_manual_otp_send(monkeypatch):
    engine = _build_engine(monkeypatch)

    def fake_start_oauth(self):
        self.oauth_start = SimpleNamespace(
            auth_url="https://auth.example.test/oauth/authorize",
            state="state-1",
            code_verifier="verifier-1",
        )
        return True

    monkeypatch.setattr(RegistrationEngine, "_init_session", lambda self: True)
    monkeypatch.setattr(RegistrationEngine, "_start_oauth", fake_start_oauth)
    monkeypatch.setattr(RegistrationEngine, "_get_device_id", lambda self: "did-1")
    monkeypatch.setattr(engine, "_try_reenter_login_flow", lambda: True)
    monkeypatch.setattr(
        engine,
        "_send_verification_code",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected manual otp send")),
        raising=False,
    )

    seen = {}
    monkeypatch.setattr(codex_auth_module.time, "time", lambda: 1_700_000_000.0)

    def fake_submit_login_password_step():
        seen["anchor_before_password"] = engine._otp_sent_at
        return True

    def fake_phase_otp_secondary(context, started_at=None):
        seen["anchor_before_wait"] = context.otp_sent_at
        seen["otp_wait_started_at"] = started_at
        return "654321", PhaseResult(phase="otp_secondary", success=True)

    monkeypatch.setattr(engine, "_submit_login_password_step", fake_submit_login_password_step)
    monkeypatch.setattr(engine, "_phase_otp_secondary", fake_phase_otp_secondary)
    monkeypatch.setattr(
        engine,
        "_validate_verification_code_and_get_continue_url",
        lambda code: (True, "https://auth.example.test/consent"),
    )
    monkeypatch.setattr(engine, "_resolve_workspace_id", lambda consent_url: "ws-1")
    monkeypatch.setattr(
        RegistrationEngine,
        "_select_workspace",
        lambda self, workspace_id: "https://auth.example.test/continue",
    )
    monkeypatch.setattr(
        RegistrationEngine,
        "_follow_redirects",
        lambda self, continue_url: "http://localhost:1455/auth/callback?code=code-1&state=state-1",
    )
    monkeypatch.setattr(
        RegistrationEngine,
        "_handle_oauth_callback",
        lambda self, callback_url: {
            "id_token": "id-token",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "account_id": "acct-1",
        },
    )

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-1"
    assert result.auth_json["auth_mode"] == "chatgpt"
    assert result.auth_json["OPENAI_API_KEY"] is None
    assert result.auth_json["tokens"] == {
        "id_token": "id-token",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "account_id": "acct-1",
    }
    assert result.auth_json["last_refresh"]
    assert seen["anchor_before_password"] == 1_700_000_000.0
    assert seen["anchor_before_wait"] == 1_700_000_000.0
    assert seen["otp_wait_started_at"] == 1_700_000_000.0


def test_resolve_workspace_id_falls_back_to_cookie_path_when_consent_page_has_no_workspace(monkeypatch):
    engine = _build_engine(monkeypatch)
    consent_url = "https://auth.example.test/consent"
    engine.oauth_start = SimpleNamespace(auth_url="https://auth.example.test/oauth/authorize")
    engine.session = FakeSession(
        FakeResponse(
            status_code=200,
            url=consent_url,
            text="<html><body>consent</body></html>",
        )
    )

    monkeypatch.setattr(
        engine,
        "_extract_workspace_id_from_response",
        lambda response=None, html=None, url=None: None,
    )
    monkeypatch.setattr(RegistrationEngine, "_get_workspace_id", lambda self: "ws-cookie")

    workspace_id = engine._resolve_workspace_id(consent_url)

    assert workspace_id == "ws-cookie"
    assert engine.session.calls == [{"url": consent_url, "kwargs": {"timeout": 20}}]
