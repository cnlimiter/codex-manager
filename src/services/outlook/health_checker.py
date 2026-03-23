"""
健康检查管理（简化版，单 Provider）
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .base import ProviderHealth, ProviderStatus, ProviderType


logger = logging.getLogger(__name__)


class HealthChecker:
    """
    单 Provider 健康检查器
    跟踪 IMAP_NEW 的健康状态
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        disable_duration: int = 300,
    ):
        self.failure_threshold = failure_threshold
        self.disable_duration = disable_duration
        self._health = ProviderHealth(provider_type=ProviderType.IMAP_NEW)
        self._lock = threading.Lock()

    def record_success(self):
        with self._lock:
            self._health.record_success()

    def record_failure(self, error: str):
        with self._lock:
            self._health.record_failure(error)
            if self._health.should_disable(self.failure_threshold):
                self._health.disable(self.disable_duration)
                logger.warning(
                    f"IMAP_NEW 已禁用 {self.disable_duration}s，原因: {error}"
                )

    def is_available(self) -> bool:
        with self._lock:
            if self._health.is_disabled():
                remaining = (
                    (self._health.disabled_until - datetime.now()).total_seconds()
                    if self._health.disabled_until
                    else 0
                )
                logger.debug(f"IMAP_NEW 已被禁用，剩余 {int(remaining)}s")
                return False
            return self._health.status != ProviderStatus.DISABLED

    def reset(self):
        with self._lock:
            self._health = ProviderHealth(provider_type=ProviderType.IMAP_NEW)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return self._health.to_dict()
