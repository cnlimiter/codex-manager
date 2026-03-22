import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from src.config.constants import EmailServiceType
from src.core.openai.oauth import OAuthStart
from src.core.register import RegistrationEngine, SignupFormResult


class DummyEmailService:
    service_type = EmailServiceType.TEMPMAIL
    config = {}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", url="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = url
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeSession:
    def __init__(self):
        self.cookies = {"oai-login-csrf_dev_123": "csrf-123"}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, url=url)

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(
            status_code=200,
            payload={"page": {"type": "email_otp_verification"}},
            url="https://auth.openai.com/u/email-otp-challenge",
        )

def _prepare_engine(monkeypatch) -> RegistrationEngine:
    engine = RegistrationEngine(DummyEmailService(), callback_logger=lambda _msg: None)

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))

    def create_email():
        engine.email = "tester@example.com"
        engine.email_info = {"service_id": "mail-1"}
        return True

    monkeypatch.setattr(engine, "_create_email", create_email)

    def init_session():
        engine.session = SimpleNamespace(
            cookies={"__Secure-next-auth.session-token": "session-token"}
        )
        return True

    monkeypatch.setattr(engine, "_init_session", init_session)

    def start_oauth():
        engine.oauth_start = OAuthStart(
            auth_url="https://auth.openai.com/oauth/authorize?client_id=test",
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
        return True

    monkeypatch.setattr(engine, "_start_oauth", start_oauth)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-1")
    monkeypatch.setattr(engine, "_check_sentinel", lambda _did: "sen-1")
    monkeypatch.setattr(
        engine,
        "_submit_signup_form",
        lambda *_args, **_kwargs: pytest.fail("主流程不应再直接调用旧的 _submit_signup_form"),
    )
    monkeypatch.setattr(engine, "_select_workspace", lambda _workspace_id: "https://continue.test")
    monkeypatch.setattr(
        engine,
        "_follow_redirects",
        lambda _url: "http://localhost:1455/auth/callback?code=code-1&state=state-1",
    )
    monkeypatch.setattr(
        engine,
        "_handle_oauth_callback",
        lambda _callback_url: {
            "account_id": "acc_123",
            "access_token": "access_123",
            "refresh_token": "refresh_123",
            "id_token": "id_123",
        },
    )
    return engine


def test_new_account_reenters_oauth_login_after_create_account(monkeypatch):
    engine = _prepare_engine(monkeypatch)
    auth_hints = []
    login_password_calls = []
    registration_send_calls = []
    passwordless_send_calls = []
    create_account_calls = []
    validated_codes = []
    workspace_values = iter([None, "ws_123"])
    init_session_calls = []
    start_oauth_calls = []
    code_iter = iter(["111111", "222222"])

    def submit_auth_form(_did, _sen_token, screen_hint):
        auth_hints.append(screen_hint)
        if screen_hint == "signup":
            return SignupFormResult(
                success=True,
                page_type="password",
                is_existing_account=False,
                response_data={},
            )
        if screen_hint == "login":
            return SignupFormResult(
                success=True,
                page_type="login_password",
                is_existing_account=False,
                response_data={},
            )
        raise AssertionError(f"未预期的 screen_hint: {screen_hint}")

    monkeypatch.setattr(engine, "_submit_auth_form", submit_auth_form, raising=False)
    monkeypatch.setattr(engine, "_register_password", lambda: (True, "pw-123456"))
    monkeypatch.setattr(
        engine,
        "_submit_login_password",
        lambda *_args, **_kwargs: login_password_calls.append(True) or SignupFormResult(success=False),
        raising=False,
    )

    def send_verification_code(*_args, **_kwargs):
        registration_send_calls.append(True)
        return True

    monkeypatch.setattr(engine, "_send_verification_code", send_verification_code)

    def send_passwordless_otp():
        passwordless_send_calls.append(True)
        return True

    monkeypatch.setattr(engine, "_send_passwordless_otp", send_passwordless_otp, raising=False)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: next(code_iter))

    def validate_verification_code(code):
        validated_codes.append(code)
        return True

    monkeypatch.setattr(engine, "_validate_verification_code", validate_verification_code)

    def create_user_account():
        create_account_calls.append(True)
        return True

    monkeypatch.setattr(engine, "_create_user_account", create_user_account)

    def get_workspace_id():
        return next(workspace_values)

    monkeypatch.setattr(engine, "_get_workspace_id", get_workspace_id)

    def init_session():
        init_session_calls.append(True)
        engine.session = SimpleNamespace(cookies={"__Secure-next-auth.session-token": "session-token"})
        return True

    monkeypatch.setattr(engine, "_init_session", init_session)

    def start_oauth():
        start_oauth_calls.append(True)
        engine.oauth_start = OAuthStart(
            auth_url=f"https://auth.openai.com/oauth/authorize?client_id=test&n={len(start_oauth_calls)}",
            state=f"state-{len(start_oauth_calls)}",
            code_verifier=f"verifier-{len(start_oauth_calls)}",
            redirect_uri="http://localhost:1455/auth/callback",
        )
        return True

    monkeypatch.setattr(engine, "_start_oauth", start_oauth)

    result = engine.run()

    assert result.success is True
    assert auth_hints == ["signup", "login"]
    assert len(init_session_calls) == 2
    assert len(start_oauth_calls) == 2
    assert len(registration_send_calls) == 1
    assert len(login_password_calls) == 0
    assert len(passwordless_send_calls) == 1
    assert validated_codes == ["111111", "222222"]
    assert len(create_account_calls) == 1
    assert result.source == "register"
    assert result.password == "pw-123456"
    assert result.workspace_id == "ws_123"


def test_new_account_keeps_original_workspace_flow_when_cookie_still_contains_workspace(monkeypatch):
    engine = _prepare_engine(monkeypatch)
    auth_hints = []
    passwordless_send_calls = []
    validated_codes = []
    init_session_calls = []
    start_oauth_calls = []

    def submit_auth_form(_did, _sen_token, screen_hint):
        auth_hints.append(screen_hint)
        return SignupFormResult(
            success=True,
            page_type="password",
            is_existing_account=False,
            response_data={},
        )

    monkeypatch.setattr(engine, "_submit_auth_form", submit_auth_form, raising=False)
    monkeypatch.setattr(engine, "_register_password", lambda: (True, "pw-123456"))
    monkeypatch.setattr(engine, "_send_verification_code", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "111111")

    def validate_verification_code(code):
        validated_codes.append(code)
        return True

    monkeypatch.setattr(engine, "_validate_verification_code", validate_verification_code)
    monkeypatch.setattr(engine, "_create_user_account", lambda: True)
    monkeypatch.setattr(engine, "_get_workspace_id", lambda: "ws_123")
    monkeypatch.setattr(
        engine,
        "_send_passwordless_otp",
        lambda: passwordless_send_calls.append(True) or True,
        raising=False,
    )
    monkeypatch.setattr(
        engine,
        "_submit_login_password",
        lambda *_args, **_kwargs: pytest.fail("workspace 已存在时不应再进入密码登录分支"),
        raising=False,
    )

    def init_session():
        init_session_calls.append(True)
        engine.session = SimpleNamespace(cookies={"__Secure-next-auth.session-token": "session-token"})
        return True

    monkeypatch.setattr(engine, "_init_session", init_session)

    def start_oauth():
        start_oauth_calls.append(True)
        engine.oauth_start = OAuthStart(
            auth_url="https://auth.openai.com/oauth/authorize?client_id=test",
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
        return True

    monkeypatch.setattr(engine, "_start_oauth", start_oauth)

    result = engine.run()

    assert result.success is True
    assert auth_hints == ["signup"]
    assert validated_codes == ["111111"]
    assert len(init_session_calls) == 1
    assert len(start_oauth_calls) == 1
    assert len(passwordless_send_calls) == 0
    assert result.workspace_id == "ws_123"
    assert result.source == "register"


def test_existing_account_keeps_single_otp_login_flow(monkeypatch):
    engine = _prepare_engine(monkeypatch)
    auth_hints = []
    validated_codes = []

    def submit_auth_form(_did, _sen_token, screen_hint):
        auth_hints.append(screen_hint)
        return SignupFormResult(
            success=True,
            page_type="email_otp_verification",
            is_existing_account=True,
            response_data={},
        )

    monkeypatch.setattr(engine, "_submit_auth_form", submit_auth_form, raising=False)
    monkeypatch.setattr(
        engine,
        "_register_password",
        lambda: pytest.fail("已注册账号不应再设置密码"),
    )
    monkeypatch.setattr(
        engine,
        "_send_verification_code",
        lambda: pytest.fail("已注册账号首次登录应复用自动发送的 OTP"),
    )
    monkeypatch.setattr(engine, "_get_verification_code", lambda: "111111")

    def validate_verification_code(code):
        validated_codes.append(code)
        return True

    monkeypatch.setattr(engine, "_validate_verification_code", validate_verification_code)
    monkeypatch.setattr(
        engine,
        "_create_user_account",
        lambda: pytest.fail("已注册账号不应创建新账户"),
    )
    monkeypatch.setattr(engine, "_get_workspace_id", lambda: "ws_existing")

    result = engine.run()

    assert result.success is True
    assert auth_hints == ["signup"]
    assert validated_codes == ["111111"]
    assert result.source == "login"
    assert result.password == ""
    assert result.workspace_id == "ws_existing"


def test_select_workspace_prefers_redirect_location():
    engine = RegistrationEngine(DummyEmailService(), callback_logger=lambda _msg: None)
    engine.session = FakeSession()

    def post(url, **kwargs):
        return FakeResponse(
            status_code=302,
            url=url,
            headers={"Location": "/oauth/continue?foo=bar"},
        )

    engine.session.post = post

    continue_url = engine._select_workspace("ws_123")

    assert continue_url == "https://auth.openai.com/oauth/continue?foo=bar"


def test_send_passwordless_otp_posts_without_body():
    engine = RegistrationEngine(DummyEmailService(), callback_logger=lambda _msg: None)
    engine.session = FakeSession()
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.openai.com/oauth/authorize?client_id=test",
        state="state-abc",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )

    result = engine._send_passwordless_otp()

    assert result is True
    post_call = engine.session.post_calls[0]
    assert post_call["url"] == "https://auth.openai.com/api/accounts/passwordless/send-otp"
    assert "data" not in post_call["kwargs"] or post_call["kwargs"]["data"] in (None, "")
    assert post_call["kwargs"]["headers"]["referer"] == "https://auth.openai.com/oauth/authorize?client_id=test"
