"""
WebSocket 路由
提供实时日志推送和任务状态更新
"""

import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...database import crud
from ...database.session import get_db
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()


def _restore_task_snapshot(task_uuid: str) -> tuple[dict, list[str]]:
    """从数据库恢复任务状态和历史日志，解决服务重启后的监控空白。"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)

    if not task:
        return {}, []

    status = {"status": task.status}
    if task.result and task.result.get("email"):
        status["email"] = task.result["email"]
    if task.error_message:
        status["error"] = task.error_message

    logs = task.logs.splitlines() if task.logs else []
    task_manager.sync_task_state(task_uuid, status=status, logs=logs)
    return status, logs


@router.websocket("/ws/task/{task_uuid}")
async def task_websocket(websocket: WebSocket, task_uuid: str):
    """
    任务日志 WebSocket

    消息格式：
    - 服务端发送: {"type": "log", "task_uuid": "xxx", "message": "...", "timestamp": "..."}
    - 服务端发送: {"type": "status", "task_uuid": "xxx", "status": "running|completed|failed|cancelled", ...}
    - 客户端发送: {"type": "ping"} - 心跳
    - 客户端发送: {"type": "cancel"} - 取消任务
    """
    await websocket.accept()
    restored_status, restored_logs = _restore_task_snapshot(task_uuid)

    # 注册连接，并取得注册时刻的历史日志快照，避免与后续实时推送串扰
    history_logs = task_manager.register_websocket(task_uuid, websocket)
    logger.info(f"WebSocket 连接已建立: {task_uuid}")

    try:
        # 发送当前状态
        status = task_manager.get_status(task_uuid) or restored_status
        if status:
            await websocket.send_json({
                "type": "status",
                "task_uuid": task_uuid,
                **status
            })

        # 发送历史日志。服务重启后 _restore_task_snapshot 会先把数据库快照回填到内存。
        for log in history_logs or restored_logs:
            await websocket.send_json({
                "type": "log",
                "task_uuid": task_uuid,
                "message": log
            })

        # 保持连接，等待客户端消息
        while True:
            try:
                # 使用 wait_for 实现超时，但不是断开连接
                # 而是发送心跳检测
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=30.0  # 30秒超时
                )

                # 处理心跳
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                # 处理取消请求
                elif data.get("type") == "cancel":
                    task_manager.cancel_task(task_uuid)
                    await websocket.send_json({
                        "type": "status",
                        "task_uuid": task_uuid,
                        "status": "cancelling",
                        "message": "取消请求已提交"
                    })

            except asyncio.TimeoutError:
                # 超时，发送心跳检测
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    # 发送失败，可能是连接断开
                    logger.info(f"WebSocket 心跳检测失败: {task_uuid}")
                    break

    except WebSocketDisconnect:
        logger.info(f"WebSocket 断开: {task_uuid}")

    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")

    finally:
        task_manager.unregister_websocket(task_uuid, websocket)


@router.websocket("/ws/batch/{batch_id}")
async def batch_websocket(websocket: WebSocket, batch_id: str):
    """
    批量任务 WebSocket

    用于批量注册任务的实时状态更新

    消息格式：
    - 服务端发送: {"type": "log", "batch_id": "xxx", "message": "...", "timestamp": "..."}
    - 服务端发送: {"type": "status", "batch_id": "xxx", "status": "running|completed|cancelled", ...}
    - 客户端发送: {"type": "ping"} - 心跳
    - 客户端发送: {"type": "cancel"} - 取消批量任务
    """
    await websocket.accept()

    # 注册连接，并取得注册时刻的历史日志快照，避免漏发/重复发送
    history_logs = task_manager.register_batch_websocket(batch_id, websocket)
    logger.info(f"批量任务 WebSocket 连接已建立: {batch_id}")

    try:
        # 发送当前状态
        status = task_manager.get_batch_status(batch_id)
        if status:
            await websocket.send_json({
                "type": "status",
                "batch_id": batch_id,
                **status
            })

        for log in history_logs:
            await websocket.send_json({
                "type": "log",
                "batch_id": batch_id,
                "message": log
            })

        # 保持连接，等待客户端消息
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=30.0
                )

                # 处理心跳
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                # 处理取消请求
                elif data.get("type") == "cancel":
                    task_manager.cancel_batch(batch_id)
                    await websocket.send_json({
                        "type": "status",
                        "batch_id": batch_id,
                        "status": "cancelling",
                        "message": "取消请求已提交"
                    })

            except asyncio.TimeoutError:
                # 超时，发送心跳检测
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    logger.info(f"批量任务 WebSocket 心跳检测失败: {batch_id}")
                    break

    except WebSocketDisconnect:
        logger.info(f"批量任务 WebSocket 断开: {batch_id}")

    except Exception as e:
        logger.error(f"批量任务 WebSocket 错误: {e}")

    finally:
        task_manager.unregister_batch_websocket(batch_id, websocket)
