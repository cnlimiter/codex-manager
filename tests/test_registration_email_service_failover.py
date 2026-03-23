from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.core.register import RegistrationResult
from src.database.models import Base, EmailService, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.services import EmailServiceType
from src.services.base import RateLimitedEmailServiceError
from src.web.routes import registration as registration_routes


class DummyTaskManager:
    def __init__(self):
        self.status_updates = []
        self.logs = {}

    def is_cancelled(self, task_uuid):
        return False

    def update_status(self, task_uuid, status, email=None, error=None):
        self.status_updates.append((task_uuid, status, email, error))

    def create_log_callback(self, task_uuid, prefix="", batch_id=""):
        def callback(message):
            self.logs.setdefault(task_uuid, []).append(message)
        return callback


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
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.last_email_service_error = None

        def run(self):
            attempts.append(self.email_service.name)
            if self.email_service.name == "duck-primary":
                self.last_email_service_error = RateLimitedEmailServiceError("请求失败: 429", retry_after=7)
                return RegistrationResult(
                    success=False,
                    error_message="创建邮箱失败: 请求失败: 429",
                    logs=[],
                )
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
    assert registration_routes.email_service_circuit_breakers[primary_service_id] > 0
