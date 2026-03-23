"""
Outlook 邮箱服务基础定义
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List


class ProviderType(str, Enum):
    """Outlook 提供者类型（仅 IMAP_NEW）"""
    IMAP_NEW = "imap_new"


class TokenEndpoint(str, Enum):
    """Token 端点"""
    CONSUMERS = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"


class ProviderStatus(str, Enum):
    """提供者状态"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DISABLED = "disabled"


@dataclass
class EmailMessage:
    """邮件消息数据类"""
    id: str
    subject: str
    sender: str
    recipients: List[str] = field(default_factory=list)
    body: str = ""
    body_preview: str = ""
    received_at: Optional[datetime] = None
    received_timestamp: int = 0
    is_read: bool = False
    has_attachments: bool = False
    raw_data: Optional[bytes] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "sender": self.sender,
            "recipients": self.recipients,
            "body": self.body,
            "body_preview": self.body_preview,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "received_timestamp": self.received_timestamp,
            "is_read": self.is_read,
            "has_attachments": self.has_attachments,
        }


@dataclass
class TokenInfo:
    """Token 信息数据类"""
    access_token: str
    expires_at: float
    token_type: str = "Bearer"
    scope: str = ""
    refresh_token: Optional[str] = None

    def is_expired(self, buffer_seconds: int = 120) -> bool:
        import time
        return time.time() >= (self.expires_at - buffer_seconds)

    @classmethod
    def from_response(cls, data: Dict[str, Any], scope: str = "") -> "TokenInfo":
        import time
        return cls(
            access_token=data.get("access_token", ""),
            expires_at=time.time() + data.get("expires_in", 3600),
            token_type=data.get("token_type", "Bearer"),
            scope=scope or data.get("scope", ""),
            refresh_token=data.get("refresh_token"),
        )


@dataclass
class ProviderHealth:
    """提供者健康状态"""
    provider_type: ProviderType
    status: ProviderStatus = ProviderStatus.HEALTHY
    failure_count: int = 0
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    last_error: str = ""
    disabled_until: Optional[datetime] = None

    def record_success(self):
        self.status = ProviderStatus.HEALTHY
        self.failure_count = 0
        self.last_success = datetime.now()
        self.disabled_until = None

    def record_failure(self, error: str):
        self.failure_count += 1
        self.last_failure = datetime.now()
        self.last_error = error

    def should_disable(self, threshold: int = 3) -> bool:
        return self.failure_count >= threshold

    def is_disabled(self) -> bool:
        if self.disabled_until and datetime.now() < self.disabled_until:
            return True
        return False

    def disable(self, duration_seconds: int = 300):
        from datetime import timedelta
        self.status = ProviderStatus.DISABLED
        self.disabled_until = datetime.now() + timedelta(seconds=duration_seconds)

    def enable(self):
        self.status = ProviderStatus.HEALTHY
        self.disabled_until = None
        self.failure_count = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_type": self.provider_type.value,
            "status": self.status.value,
            "failure_count": self.failure_count,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "last_error": self.last_error,
            "disabled_until": self.disabled_until.isoformat() if self.disabled_until else None,
        }
