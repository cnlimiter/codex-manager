"""
Token 管理器（简化版）
固定使用 consumers 端点 + IMAP scope
"""

import json
import logging
import threading
import time
from typing import Dict, Optional, Any

from curl_cffi import requests as _requests

from .base import TokenInfo
from .account import OutlookAccount


logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


class TokenManager:
    """
    Token 管理器
    固定 consumers 端点，缓存 key = email
    """

    _token_cache: Dict[str, TokenInfo] = {}
    _cache_lock = threading.Lock()

    DEFAULT_TIMEOUT = 30
    REFRESH_BUFFER = 120

    def __init__(
        self,
        account: OutlookAccount,
        proxy_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        service_id: Optional[int] = None,
    ):
        self.account = account
        self.proxy_url = proxy_url
        self.timeout = timeout
        self.service_id = service_id

    def _cache_key(self) -> str:
        return self.account.email.lower()

    def get_cached_token(self) -> Optional[TokenInfo]:
        with self._cache_lock:
            token = self._token_cache.get(self._cache_key())
            if token and not token.is_expired(self.REFRESH_BUFFER):
                return token
        return None

    def set_cached_token(self, token: TokenInfo):
        with self._cache_lock:
            self._token_cache[self._cache_key()] = token

    def clear_cache(self):
        with self._cache_lock:
            self._token_cache.pop(self._cache_key(), None)

    def get_access_token(self, force_refresh: bool = False) -> Optional[str]:
        if not force_refresh:
            cached = self.get_cached_token()
            if cached:
                logger.debug(f"[{self.account.email}] 使用缓存 Token")
                return cached.access_token

        try:
            token = self._refresh_token()
            if token:
                self.set_cached_token(token)
                return token.access_token
        except Exception as e:
            logger.error(f"[{self.account.email}] 获取 Token 失败: {e}")

        return None

    def _refresh_token(self) -> Optional[TokenInfo]:
        if not self.account.client_id or not self.account.refresh_token:
            raise ValueError("缺少 client_id 或 refresh_token")

        logger.debug(f"[{self.account.email}] 正在刷新 Token...")

        data = {
            "client_id": self.account.client_id,
            "refresh_token": self.account.refresh_token,
            "grant_type": "refresh_token",
            "scope": IMAP_SCOPE,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}

        try:
            resp = _requests.post(
                TOKEN_URL,
                data=data,
                headers=headers,
                proxies=proxies,
                timeout=self.timeout,
                impersonate="chrome110",
            )

            if resp.status_code != 200:
                body = resp.text
                logger.error(f"[{self.account.email}] Token 刷新失败: HTTP {resp.status_code}")
                if "service abuse" in body.lower():
                    logger.warning(f"[{self.account.email}] 账号可能被封禁")
                elif "invalid_grant" in body.lower():
                    logger.warning(f"[{self.account.email}] Refresh Token 已失效")
                return None

            response_data = resp.json()
            token = TokenInfo.from_response(response_data, IMAP_SCOPE)
            logger.info(
                f"[{self.account.email}] Token 刷新成功，"
                f"有效期 {int(token.expires_at - time.time())} 秒"
            )

            # 若响应含新 refresh_token → 写回内存 + 持久化数据库
            new_rt = response_data.get("refresh_token", "")
            if new_rt and new_rt != self.account.refresh_token:
                self.account.refresh_token = new_rt
                if self.service_id:
                    try:
                        from ...database.session import get_session_manager
                        from ...database.crud import update_outlook_refresh_token
                        with get_session_manager().session_scope() as db:
                            update_outlook_refresh_token(
                                db, self.service_id, self.account.email, new_rt
                            )
                        logger.info(f"[{self.account.email}] refresh_token 已写回数据库")
                    except Exception as e:
                        logger.warning(f"[{self.account.email}] 写回 refresh_token 失败: {e}")

            return token

        except json.JSONDecodeError as e:
            logger.error(f"[{self.account.email}] JSON 解析错误: {e}")
            return None
        except Exception as e:
            logger.error(f"[{self.account.email}] 未知错误: {e}")
            return None

    @classmethod
    def clear_all_cache(cls):
        with cls._cache_lock:
            cls._token_cache.clear()
            logger.info("已清除所有 Token 缓存")

    @classmethod
    def get_cache_stats(cls) -> Dict[str, Any]:
        with cls._cache_lock:
            return {
                "cache_size": len(cls._token_cache),
                "entries": list(cls._token_cache.keys()),
            }
