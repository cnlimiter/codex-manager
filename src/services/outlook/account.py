"""
Outlook 账户数据类
"""

from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class OutlookAccount:
    """Outlook 账户信息"""
    email: str
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "OutlookAccount":
        return cls(
            email=config.get("email", ""),
            password=config.get("password", ""),
            client_id=config.get("client_id", ""),
            refresh_token=config.get("refresh_token", "")
        )

    def has_oauth(self) -> bool:
        return bool(self.client_id and self.refresh_token)

    def validate(self) -> bool:
        return bool(self.email and self.password) or self.has_oauth()

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        result = {
            "email": self.email,
            "has_oauth": self.has_oauth(),
        }
        if include_sensitive:
            result.update({
                "password": self.password,
                "client_id": self.client_id,
                "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            })
        return result

    def __str__(self) -> str:
        return f"OutlookAccount({self.email})"
