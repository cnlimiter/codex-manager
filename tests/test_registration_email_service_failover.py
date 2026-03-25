from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace

import src.services.base as base_module
from src.core.register import (
    ERROR_OTP_TIMEOUT_SECONDARY,
    PhaseResult,
    RegistrationResult,
)
from src.database.models import Base, EmailService, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.services import EmailServiceType
from src.services.base import BaseEmailService, EmailProviderBackoffState
from src.web.routes import registration as registration_routes


class DummyTaskManager:
    def __init__(self):
        self.status_updates = []
        self.logs = {}

    def is_cancelled(self, task_uuid):
        return False

    def update_status(self, task_uuid, status, email=None, error=None, **kwargs):
        self.status_updates.append((task_uuid, status, email, error, kwargs))

    def create_log_callback(self, task_uuid, prefix="", batch_id=""):
        def callback(message):
            self.logs.setdefault(task_uuid, []).append(message)
        return callback


class BackoffAwareEmailService(BaseEmailService):
    def __init__(self, service_type, config=None, name=None):
        super().__init__(service_type=service_type, name=name)
        self.config = config or {}

    def create_email(self, config=None):
        return {"email": "tester@example.com", "service_id": "svc-1"}

    def get_verification_code(self, **kwargs):
        return None

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


def test_registration_task_fails_over_after_rate_limit(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_failover.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    task_uuid = "task-rate-limit-failover"
    with manager.session_scope() as session:
        session.add(RegistrationTask(task_uuid=task_uuid, status="pending"))
        session.add_all([
            EmailService(
                service_type="duck_mail",
                name="duck-primary",
                config={
                    "base_url": "https://mail-1.example.test",
                    "default_domain": "mail.example.test",
                },
                enabled=True,
                priority=0,
            ),
            EmailService(
                service_type="duck_mail",
                name="duck-secondary",
                config={
                    "base_url": "https://mail-2.example.test",
                    "default_domain": "mail.example.test",
                },
                enabled=True,
                priority=1,
            ),
        ])

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class DummySettings:
        pass

    attempts = []

    class FakeRegistrationEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, status_callback=None, task_uuid=None):
            self.email_service = email_service
            self.phase_history = []

        def run(self):
            attempts.append(self.email_service.name)
            if self.email_service.name == "duck-primary":
                self.phase_history = [
                    PhaseResult(
                        phase="email_prepare",
                        success=False,
                        error_message="创建邮箱失败",
                        error_code="EMAIL_PROVIDER_RATE_LIMITED",
                        retryable=True,
                        next_action="switch_provider",
                        provider_backoff=EmailProviderBackoffState(
                            failures=1,
                            delay_seconds=30,
                            opened_until=1030.0,
                            retry_after=7,
                            last_error="请求失败: 429",
                        ),
                    )
                ]
                return RegistrationResult(
                    success=False,
                    error_message="创建邮箱失败: 请求失败: 429",
                    logs=[],
                )
            self.phase_history = [
                PhaseResult(
                    phase="email_prepare",
                    success=True,
                    provider_backoff=EmailProviderBackoffState(),
                )
            ]
            return RegistrationResult(
                success=True,
                email="tester@example.com",
                password="Pass12345",
                account_id="acct-1",
                workspace_id="ws-1",
                access_token="access-token",
                refresh_token="refresh-token",
                id_token="id-token",
                logs=[],
            )

        def save_to_database(self, result):
            return True

        def close(self):
            return None

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "task_manager", DummyTaskManager())
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeRegistrationEngine)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: SimpleNamespace(
            service_type=service_type,
            name=name or service_type.value,
            config=config,
        ),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: None)
    registration_routes.email_service_circuit_breakers.clear()

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type=EmailServiceType.DUCK_MAIL.value,
        proxy=None,
        email_service_config=None,
    )

    with manager.session_scope() as session:
        task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == task_uuid).first()
        services = session.query(EmailService).order_by(EmailService.priority.asc()).all()
        task_status = task.status
        task_email_service_id = task.email_service_id
        primary_service_id = services[0].id
        secondary_service_id = services[1].id

    assert attempts == ["duck-primary", "duck-secondary"]
    assert task_status == "completed"
    assert task_email_service_id == secondary_service_id
    assert registration_routes.email_service_circuit_breakers[primary_service_id].failures == 1
    assert registration_routes.email_service_circuit_breakers[primary_service_id].delay_seconds == 30


def test_registration_task_enters_deep_cooldown_after_three_otp_timeouts(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_otp_timeout_backoff.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    task_uuids = [
        "task-otp-timeout-1",
        "task-otp-timeout-2",
        "task-otp-timeout-3",
    ]
    with manager.session_scope() as session:
        session.add_all([RegistrationTask(task_uuid=task_uuid, status="pending") for task_uuid in task_uuids])
        session.add(
            EmailService(
                service_type="duck_mail",
                name="duck-primary",
                config={
                    "base_url": "https://mail-1.example.test",
                    "default_domain": "mail.example.test",
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class DummySettings:
        pass

    current_time = {"value": 1000.0}

    class FakeRegistrationEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, status_callback=None, task_uuid=None):
            self.email_service = email_service
            self.phase_history = []

        def run(self):
            self.phase_history = [
                PhaseResult(
                    phase="email_prepare",
                    success=True,
                    provider_backoff=EmailProviderBackoffState(),
                )
            ]
            return RegistrationResult(
                success=False,
                error_message="等待验证码超时",
                error_code=ERROR_OTP_TIMEOUT_SECONDARY,
                logs=[],
            )

        def save_to_database(self, result):
            return True

        def close(self):
            return None

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "task_manager", DummyTaskManager())
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeRegistrationEngine)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: BackoffAwareEmailService(
            service_type=service_type,
            config=config,
            name=name,
        ),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: None)
    monkeypatch.setattr(base_module.time, "time", lambda: current_time["value"])
    registration_routes.email_service_circuit_breakers.clear()

    with manager.session_scope() as session:
        service_id = session.query(EmailService.id).filter(EmailService.name == "duck-primary").scalar()

    expected_delays = [30, 60, 3600]
    for attempt_index, task_uuid in enumerate(task_uuids, start=1):
        registration_routes._run_sync_registration_task(
            task_uuid=task_uuid,
            email_service_type=EmailServiceType.DUCK_MAIL.value,
            proxy=None,
            email_service_config=None,
        )

        with manager.session_scope() as session:
            task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == task_uuid).first()
            assert task.status == "failed"
            assert task.error_message == "等待验证码超时"

        state = registration_routes.email_service_circuit_breakers[service_id]
        assert state.failures == attempt_index
        assert state.delay_seconds == expected_delays[attempt_index - 1]
        assert state.opened_until == current_time["value"] + expected_delays[attempt_index - 1]

        if attempt_index < len(task_uuids):
            current_time["value"] = state.opened_until + 1

    final_state = registration_routes.email_service_circuit_breakers[service_id]
    assert final_state.delay_seconds == 3600
    assert final_state.failures == 3


def test_registration_task_success_clears_email_service_backoff(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_success_clears_backoff.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    task_uuid = "task-success-clears-backoff"
    with manager.session_scope() as session:
        session.add(RegistrationTask(task_uuid=task_uuid, status="pending"))
        session.add(
            EmailService(
                service_type="duck_mail",
                name="duck-primary",
                config={
                    "base_url": "https://mail-1.example.test",
                    "default_domain": "mail.example.test",
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class DummySettings:
        pass

    class FakeRegistrationEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, status_callback=None, task_uuid=None):
            self.email_service = email_service
            self.phase_history = [
                PhaseResult(
                    phase="email_prepare",
                    success=True,
                    provider_backoff=EmailProviderBackoffState(),
                )
            ]

        def run(self):
            return RegistrationResult(
                success=True,
                email="tester@example.com",
                password="Pass12345",
                account_id="acct-1",
                workspace_id="ws-1",
                access_token="access-token",
                refresh_token="refresh-token",
                id_token="id-token",
                logs=[],
            )

        def save_to_database(self, result):
            return True

        def close(self):
            return None

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "task_manager", DummyTaskManager())
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeRegistrationEngine)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: BackoffAwareEmailService(
            service_type=service_type,
            config=config,
            name=name,
        ),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: None)
    registration_routes.email_service_circuit_breakers.clear()

    with manager.session_scope() as session:
        service_id = session.query(EmailService.id).filter(EmailService.name == "duck-primary").scalar()

    registration_routes.email_service_circuit_breakers[service_id] = EmailProviderBackoffState(
        failures=2,
        delay_seconds=60,
        opened_until=9999.0,
        last_error="等待验证码超时",
    )

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type=EmailServiceType.DUCK_MAIL.value,
        proxy=None,
        email_service_config=None,
    )

    assert service_id not in registration_routes.email_service_circuit_breakers


def test_registration_task_backoff_failures_do_not_get_lost_under_concurrency(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_backoff_concurrency.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    task_uuids = ["task-backoff-1", "task-backoff-2"]
    with manager.session_scope() as session:
        for task_uuid in task_uuids:
            session.add(RegistrationTask(task_uuid=task_uuid, status="pending"))
        session.add(
            EmailService(
                service_type="duck_mail",
                name="duck-primary",
                config={
                    "base_url": "https://mail-1.example.test",
                    "default_domain": "mail.example.test",
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class DummySettings:
        pass

    start_lock = threading.Lock()
    started = {"count": 0}
    peer_started = threading.Event()

    class FakeRegistrationEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, status_callback=None, task_uuid=None):
            self.email_service = email_service
            self.phase_history = []

        def run(self):
            with start_lock:
                started["count"] += 1
                if started["count"] == len(task_uuids):
                    peer_started.set()
            peer_started.wait(timeout=0.1)

            current_state = self.email_service.provider_backoff_state
            next_failures = current_state.failures + 1
            delay_seconds = 30 if next_failures == 1 else 60
            self.phase_history = [
                PhaseResult(
                    phase="email_prepare",
                    success=False,
                    error_message="创建邮箱失败",
                    error_code="EMAIL_PROVIDER_RATE_LIMITED",
                    retryable=True,
                    next_action="switch_provider",
                    provider_backoff=EmailProviderBackoffState(
                        failures=next_failures,
                        delay_seconds=delay_seconds,
                        opened_until=1000.0 + delay_seconds,
                        last_error="请求失败: 429",
                    ),
                )
            ]
            return RegistrationResult(
                success=False,
                error_message="创建邮箱失败: 请求失败: 429",
                logs=[],
            )

        def save_to_database(self, result):
            return True

        def close(self):
            return None

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "task_manager", DummyTaskManager())
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeRegistrationEngine)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: BackoffAwareEmailService(
            service_type=service_type,
            config=config,
            name=name,
        ),
    )
    registration_routes.email_service_circuit_breakers.clear()

    with manager.session_scope() as session:
        service_id = session.query(EmailService.id).filter(EmailService.name == "duck-primary").scalar()

    threads = [
        threading.Thread(
            target=registration_routes._run_sync_registration_task,
            kwargs={
                "task_uuid": task_uuid,
                "email_service_type": EmailServiceType.DUCK_MAIL.value,
                "proxy": None,
                "email_service_config": None,
            },
        )
        for task_uuid in task_uuids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    state = registration_routes.email_service_circuit_breakers[service_id]
    assert state.failures == 2
    assert state.delay_seconds == 60
