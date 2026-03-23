"""
Outlook 提供者抽象基类
"""

import abc
import logging
from dataclasses import dataclass
from typing import List, Optional

from ..base import ProviderType, EmailMessage, ProviderHealth, ProviderStatus
from ..account import OutlookAccount


logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """提供者配置"""
    timeout: int = 30
    proxy_url: Optional[str] = None
    service_id: Optional[int] = None
    health_failure_threshold: int = 3
    health_disable_duration: int = 300


class OutlookProvider(abc.ABC):
    """Outlook 提供者抽象基类"""

    def __init__(
        self,
        account: OutlookAccount,
        config: Optional[ProviderConfig] = None,
    ):
        self.account = account
        self.config = config or ProviderConfig()
        self._health = ProviderHealth(provider_type=ProviderType.IMAP_NEW)
        self._connected = False
        self._last_error: Optional[str] = None

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.IMAP_NEW

    @property
    def health(self) -> ProviderHealth:
        return self._health

    @property
    def is_healthy(self) -> bool:
        return (
            self._health.status == ProviderStatus.HEALTHY
            and not self._health.is_disabled()
        )

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abc.abstractmethod
    def connect(self) -> bool:
        pass

    @abc.abstractmethod
    def disconnect(self):
        pass

    @abc.abstractmethod
    def get_recent_emails(
        self,
        count: int = 20,
        only_unseen: bool = True,
    ) -> List[EmailMessage]:
        pass

    @abc.abstractmethod
    def test_connection(self) -> bool:
        pass

    def wait_for_new_email_idle(self, timeout: int = 25) -> bool:
        """IMAP IDLE（默认不支持，子类可覆盖）"""
        return False

    def record_success(self):
        self._health.record_success()
        self._last_error = None

    def record_failure(self, error: str):
        self._health.record_failure(error)
        self._last_error = error
        if self._health.should_disable(self.config.health_failure_threshold):
            self._health.disable(self.config.health_disable_duration)
            logger.warning(
                f"[{self.account.email}] IMAP_NEW 已禁用 "
                f"{self.config.health_disable_duration}s，原因: {error}"
            )

    def check_health(self) -> bool:
        if self._health.is_disabled():
            return False
        return self._health.status in (ProviderStatus.HEALTHY, ProviderStatus.DEGRADED)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.account.email})"

    def __repr__(self) -> str:
        return self.__str__()
