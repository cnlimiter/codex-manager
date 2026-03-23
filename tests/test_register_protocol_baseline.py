import json
from types import SimpleNamespace

import src.core.register as register_module
from src.config.constants import OPENAI_PAGE_TYPES
from src.core.register import RegistrationEngine
from src.services import EmailServiceType


class DummySettings:
    openai_client_id = "client-id"
    openai_auth_url = "https://auth.example.test"
    openai_token_url = "https://token.example.test"
    openai_redirect_uri = "https://callback.example.test"
    openai_scope = "openid profile email"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({
            "url": url,
            **kwargs,
        })
        return self.response


def _build_engine(monkeypatch):
    monkeypatch.setattr(register_module, "get_settings", lambda: DummySettings())
    email_service = SimpleNamespace(service_type=EmailServiceType.DUCK_MAIL)
    return RegistrationEngine(email_service=email_service)


def test_submit_signup_form_uses_stable_protocol_body(monkeypatch):
    engine = _build_engine(monkeypatch)
    session = FakeSession(FakeResponse(
        status_code=200,
        payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}},
    ))
    engine.session = session
    engine.email = "tester@example.com"

    result = engine._submit_signup_form("did-1", None)

    assert result.success is True
    assert result.is_existing_account is False
    assert (
        session.calls[0]["data"]
        == '{"username":{"value":"tester@example.com","kind":"email"},"screen_hint":"signup"}'
    )


def test_register_password_uses_stable_protocol_body(monkeypatch):
    engine = _build_engine(monkeypatch)
    session = FakeSession(FakeResponse(status_code=200))
    engine.session = session
    engine.email = "tester@example.com"
    monkeypatch.setattr(engine, "_generate_password", lambda length=0: "Pass12345")

    success, password = engine._register_password()

    assert success is True
    assert password == "Pass12345"
    assert session.calls[0]["data"] == json.dumps(
        {
            "password": "Pass12345",
            "username": "tester@example.com",
        }
    )
