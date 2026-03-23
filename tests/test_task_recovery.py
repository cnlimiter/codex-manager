from contextlib import contextmanager
import asyncio

from fastapi import WebSocketDisconnect

from src.database import crud
from src.database.models import Base, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web.routes import websocket as websocket_routes
from src.web.task_manager import TaskManager


def test_fail_incomplete_registration_tasks_marks_pending_and_running_failed(tmp_path):
    db_path = tmp_path / "recovery.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add_all([
            RegistrationTask(task_uuid="task-pending", status="pending"),
            RegistrationTask(task_uuid="task-running", status="running", logs="[01:00:00] still running"),
            RegistrationTask(task_uuid="task-done", status="completed"),
        ])

    with manager.session_scope() as session:
        cleaned = crud.fail_incomplete_registration_tasks(
            session,
            "服务启动时检测到未完成的历史任务，已标记失败，请重新发起。"
        )

    assert cleaned == ["task-pending", "task-running"]

    with manager.session_scope() as session:
        pending_task = crud.get_registration_task_by_uuid(session, "task-pending")
        running_task = crud.get_registration_task_by_uuid(session, "task-running")
        done_task = crud.get_registration_task_by_uuid(session, "task-done")

        assert pending_task.status == "failed"
        assert running_task.status == "failed"
        assert pending_task.error_message == "服务启动时检测到未完成的历史任务，已标记失败，请重新发起。"
        assert running_task.completed_at is not None
        assert "[系统] 服务启动时检测到未完成的历史任务，已标记失败，请重新发起。" in running_task.logs
        assert done_task.status == "completed"


def test_restore_task_snapshot_loads_status_and_logs_from_database(monkeypatch, tmp_path):
    db_path = tmp_path / "websocket.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            RegistrationTask(
                task_uuid="task-websocket",
                status="failed",
                logs="[01:00:00] step 1\n[01:00:01] step 2",
                result={"email": "tester@example.com"},
                error_message="boom"
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(websocket_routes, "get_db", fake_get_db)

    status, logs = websocket_routes._restore_task_snapshot("task-websocket")

    assert status == {
        "status": "failed",
        "email": "tester@example.com",
        "error": "boom",
    }
    assert logs == ["[01:00:00] step 1", "[01:00:01] step 2"]


def test_sync_task_state_prefers_longer_persisted_log_history():
    manager = TaskManager()
    task_uuid = "task-sync"

    manager.sync_task_state(task_uuid, status={"status": "running"}, logs=["a", "b"])
    manager.sync_task_state(task_uuid, logs=["a"])

    assert manager.get_status(task_uuid) == {"status": "running"}
    assert manager.get_logs(task_uuid) == ["a", "b"]


def test_register_websocket_returns_snapshot_and_keeps_live_cursor():
    manager = TaskManager()
    task_uuid = "task-live"
    websocket = object()

    manager.sync_task_state(task_uuid, status={"status": "running"}, logs=["log-1", "log-2"])

    history_logs = manager.register_websocket(task_uuid, websocket)

    assert history_logs == ["log-1", "log-2"]
    assert manager.get_unsent_logs(task_uuid, websocket) == []

    manager.add_log(task_uuid, "log-3")

    assert manager.get_unsent_logs(task_uuid, websocket) == ["log-3"]


class _FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.messages.append(payload)

    async def receive_json(self):
        raise WebSocketDisconnect()


def test_batch_websocket_replays_history_logs_from_registration_snapshot(monkeypatch):
    manager = TaskManager()
    batch_id = "batch-history"
    websocket = _FakeWebSocket()

    manager.init_batch(batch_id, total=2)
    manager.add_batch_log(batch_id, "[01:00:00] first")
    manager.add_batch_log(batch_id, "[01:00:01] second")

    monkeypatch.setattr(websocket_routes, "task_manager", manager)

    asyncio.run(websocket_routes.batch_websocket(websocket, batch_id))

    assert websocket.accepted is True
    assert websocket.messages[0]["type"] == "status"
    assert [msg["message"] for msg in websocket.messages[1:]] == [
        "[01:00:00] first",
        "[01:00:01] second",
    ]
