"""
Outlook 邮箱服务主类（简化版）
单一 IMAP_NEW Provider + 邮件缓存 + IMAP IDLE 支持
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List

from ..base import BaseEmailService, EmailServiceError, EmailServiceType
from ...config.constants import EmailServiceType as ServiceType
from ...config.settings import get_settings
from .account import OutlookAccount
from .base import EmailMessage
from .email_parser import get_email_parser
from .health_checker import HealthChecker
from .providers.base import ProviderConfig
from .providers.imap_new import IMAPNewProvider


logger = logging.getLogger(__name__)

# 验证码搜索的文件夹列表（同时搜索收件箱和垃圾箱）
_OUTLOOK_SEARCH_FOLDERS = ["INBOX", "Junk Email"]


def _get_code_settings() -> dict:
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


class _EmailCache:
    """轻量级邮件内存缓存（TTL=60s，减少重复 IMAP 请求）"""

    TTL = 60

    def __init__(self):
        self._cache: Dict[str, tuple] = {}  # email -> (timestamp, List[EmailMessage])
        self._lock = threading.Lock()

    def get(self, email: str) -> Optional[List[EmailMessage]]:
        with self._lock:
            entry = self._cache.get(email)
            if entry and time.time() - entry[0] < self.TTL:
                return entry[1]
        return None

    def set(self, email: str, messages: List[EmailMessage]):
        with self._lock:
            self._cache[email] = (time.time(), messages)

    def invalidate(self, email: str):
        with self._lock:
            self._cache.pop(email, None)


class OutlookService(BaseEmailService):
    """
    Outlook 邮箱服务
    使用单一 IMAP_NEW Provider，支持连接池复用和 IMAP IDLE
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(ServiceType.OUTLOOK, name)

        default_config = {
            "accounts": [],
            "health_failure_threshold": 5,
            "health_disable_duration": 60,
            "timeout": 30,
            "proxy_url": None,
        }
        self.config = {**default_config, **(config or {})}

        self.provider_config = ProviderConfig(
            timeout=self.config.get("timeout", 30),
            proxy_url=self.config.get("proxy_url"),
            service_id=self.config.get("service_id"),
            health_failure_threshold=self.config.get("health_failure_threshold", 3),
            health_disable_duration=self.config.get("health_disable_duration", 300),
        )

        # 获取默认 client_id
        try:
            _default_client_id = get_settings().outlook_default_client_id
        except Exception:
            _default_client_id = "24d9a0ed-8787-4584-883c-2fd79308940a"

        # 解析账户
        self.accounts: List[OutlookAccount] = []
        self._current_account_index = 0
        self._account_lock = threading.Lock()

        if "email" in self.config and "password" in self.config:
            account = OutlookAccount.from_config(self.config)
            if not account.client_id and _default_client_id:
                account.client_id = _default_client_id
            if account.validate():
                if not account.has_oauth():
                    logger.warning(
                        f"[{account.email}] 跳过：IMAP_NEW 仅支持 OAuth2，"
                        f"请配置 client_id 和 refresh_token"
                    )
                else:
                    self.accounts.append(account)
        else:
            for ac in self.config.get("accounts", []):
                account = OutlookAccount.from_config(ac)
                if not account.client_id and _default_client_id:
                    account.client_id = _default_client_id
                if account.validate():
                    if not account.has_oauth():
                        logger.warning(
                            f"[{account.email}] 跳过：IMAP_NEW 仅支持 OAuth2，"
                            f"请配置 client_id 和 refresh_token"
                        )
                    else:
                        self.accounts.append(account)

        if not self.accounts:
            logger.warning("未配置有效的 Outlook 账户（需要 client_id + refresh_token）")

        # 健康检查器
        self.health_checker = HealthChecker(
            failure_threshold=self.provider_config.health_failure_threshold,
            disable_duration=self.provider_config.health_disable_duration,
        )

        # 邮件解析器
        self.email_parser = get_email_parser()

        # Provider 实例缓存: email -> IMAPNewProvider
        self._providers: Dict[str, IMAPNewProvider] = {}
        self._provider_lock = threading.Lock()

        # IMAP 并发限制（最多 5 个并发）
        self._imap_semaphore = threading.Semaphore(5)

        # 邮件缓存
        self._email_cache = _EmailCache()

        # 验证码去重
        self._used_codes: Dict[str, set] = {}

    def _get_provider(self, account: OutlookAccount) -> IMAPNewProvider:
        key = account.email.lower()
        with self._provider_lock:
            if key not in self._providers:
                self._providers[key] = IMAPNewProvider(account, self.provider_config)
            return self._providers[key]

    def _fetch_emails(
        self,
        account: OutlookAccount,
        count: int = 15,
        only_unseen: bool = True,
        since_minutes: Optional[int] = None,
        use_cache: bool = False,
        folders: Optional[List[str]] = None,
    ) -> List[EmailMessage]:
        """通过 IMAP_NEW Provider 获取邮件，可选使用内存缓存"""
        if use_cache:
            cached = self._email_cache.get(account.email)
            if cached is not None:
                return cached

        if not self.health_checker.is_available():
            logger.debug(f"[{account.email}] IMAP_NEW 不可用，跳过")
            return []

        try:
            provider = self._get_provider(account)
            with self._imap_semaphore:
                with provider:
                    emails = provider.get_recent_emails(
                        count, only_unseen, since_minutes=since_minutes, folders=folders
                    )

            if emails:
                self.health_checker.record_success()
                if use_cache:
                    self._email_cache.set(account.email, emails)
            return emails

        except Exception as e:
            err = str(e)
            self.health_checker.record_failure(err)
            logger.warning(f"[{account.email}] 获取邮件失败: {e}")
            return []

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """轮询选择可用的 Outlook 账户"""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有可用的 Outlook 账户"))
            raise EmailServiceError("没有可用的 Outlook 账户")

        with self._account_lock:
            account = self.accounts[self._current_account_index]
            self._current_account_index = (self._current_account_index + 1) % len(self.accounts)

        logger.info(f"选择 Outlook 账户: {account.email}")
        self.update_status(True)
        return {
            "email": account.email,
            "service_id": account.email,
            "account": {"email": account.email, "has_oauth": account.has_oauth()},
        }

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = None,
        pattern: str = None,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """从 Outlook 邮箱获取验证码"""
        account = next(
            (a for a in self.accounts if a.email.lower() == email.lower()), None
        )
        if not account:
            self.update_status(False, EmailServiceError(f"未找到邮箱账户: {email}"))
            return None

        code_settings = _get_code_settings()
        actual_timeout = timeout or code_settings["timeout"]
        poll_interval = code_settings["poll_interval"]

        logger.info(f"[{email}] 开始获取验证码，超时 {actual_timeout}s")

        if email not in self._used_codes:
            self._used_codes[email] = set()
        used_codes = self._used_codes[email]

        min_timestamp = (otp_sent_at - 60) if otp_sent_at else 0

        use_idle = True
        try:
            use_idle = get_settings().outlook_use_idle
        except Exception:
            pass

        if use_idle:
            code = self._wait_with_idle(
                account, email, actual_timeout, min_timestamp, used_codes, otp_sent_at
            )
        else:
            code = self._wait_with_poll(
                account, email, actual_timeout, poll_interval, min_timestamp, used_codes, otp_sent_at
            )

        if code:
            used_codes.add(code)
            self.update_status(True)
            return code
        return None

    def _wait_with_poll(
        self,
        account: OutlookAccount,
        email: str,
        timeout: int,
        poll_interval: int,
        min_timestamp: float,
        used_codes: set,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """轮询方式等待验证码"""
        start_time = time.time()
        poll_count = 0

        while time.time() - start_time < timeout:
            poll_count += 1
            # 每次动态计算 since_minutes，确保时间窗口随轮询推进而更新
            if otp_sent_at:
                elapsed_since_send = int((time.time() - otp_sent_at) / 60) + 2
                since_minutes: Optional[int] = min(elapsed_since_send, 180)
                only_unseen = False
            else:
                since_minutes = None
                only_unseen = poll_count <= 3
            try:
                emails = self._fetch_emails(
                    account, count=15, only_unseen=only_unseen,
                    since_minutes=since_minutes,
                    folders=_OUTLOOK_SEARCH_FOLDERS,
                )
                if emails:
                    code = self.email_parser.find_verification_code_in_emails(
                        emails,
                        target_email=email,
                        min_timestamp=min_timestamp,
                        used_codes=used_codes,
                    )
                    if code:
                        elapsed = int(time.time() - start_time)
                        logger.info(
                            f"[{email}] 找到验证码: {code}，耗时 {elapsed}s，轮询 {poll_count} 次"
                        )
                        return code
            except Exception as e:
                logger.warning(f"[{email}] 轮询出错: {e}")

            time.sleep(poll_interval)

        logger.warning(f"[{email}] 验证码超时 ({timeout}s)，共轮询 {poll_count} 次")
        return None

    def _wait_with_idle(
        self,
        account: OutlookAccount,
        email: str,
        timeout: int,
        min_timestamp: float,
        used_codes: set,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """IMAP IDLE 方式等待验证码，失败时自动降级为轮询"""
        if not self.health_checker.is_available():
            logger.warning(f"[{email}] IMAP_NEW 不可用，降级为轮询")
            return self._wait_with_poll(
                account, email, timeout, 3, min_timestamp, used_codes, otp_sent_at
            )

        # 计算 since_minutes：从发送时间前2分钟开始，最多180分钟
        since_minutes: Optional[int] = None
        if otp_sent_at:
            elapsed_since_send = int((time.time() - otp_sent_at) / 60) + 2
            since_minutes = min(elapsed_since_send, 180)

        start_time = time.time()
        try:
            provider = self._get_provider(account)
            with self._imap_semaphore:
                with provider:
                    # 先做一次即时检查
                    emails = provider.get_recent_emails(
                        15, only_unseen=(since_minutes is None), since_minutes=since_minutes,
                        folders=_OUTLOOK_SEARCH_FOLDERS,
                    )
                    code = self.email_parser.find_verification_code_in_emails(
                        emails,
                        target_email=email,
                        min_timestamp=min_timestamp,
                        used_codes=used_codes,
                    )
                    if code:
                        elapsed = int(time.time() - start_time)
                        logger.info(f"[{email}] 找到验证码: {code}，耗时 {elapsed}s（即时检查）")
                        return code

                    # IDLE 等待循环
                    while time.time() - start_time < timeout:
                        remaining = int(timeout - (time.time() - start_time))
                        if remaining <= 0:
                            break
                        arrived = provider.wait_for_new_email_idle(timeout=min(remaining, 25))
                        # 无效化缓存，强制重新拉取
                        self._email_cache.invalidate(email)
                        # IDLE 触发后用 since_minutes 搜索，覆盖已读邮件
                        fetch_since = since_minutes
                        if fetch_since is None:
                            # 没有 otp_sent_at 时，用距当前时间2分钟内的邮件
                            fetch_since = 2
                        emails = provider.get_recent_emails(
                            15, only_unseen=False, since_minutes=fetch_since,
                            folders=_OUTLOOK_SEARCH_FOLDERS,
                        )
                        code = self.email_parser.find_verification_code_in_emails(
                            emails,
                            target_email=email,
                            min_timestamp=min_timestamp,
                            used_codes=used_codes,
                        )
                        if code:
                            elapsed = int(time.time() - start_time)
                            logger.info(
                                f"[{email}] 找到验证码: {code}，耗时 {elapsed}s"
                                f"（IDLE {'推送' if arrived else '超时检查'}）"
                            )
                            return code

        except Exception as e:
            logger.warning(f"[{email}] IDLE 失败，降级为轮询: {e}")
            elapsed = int(time.time() - start_time)
            remaining = max(0, timeout - elapsed)
            if remaining > 0:
                code_settings = _get_code_settings()
                return self._wait_with_poll(
                    account, email, remaining,
                    code_settings["poll_interval"], min_timestamp, used_codes, otp_sent_at
                )

        logger.warning(f"[{email}] IDLE 等待验证码超时 ({timeout}s)")
        return None

    def check_health(self) -> bool:
        """检查 Outlook 服务是否可用"""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有配置的账户"))
            return False

        try:
            provider = self._get_provider(self.accounts[0])
            if provider.test_connection():
                self.update_status(True)
                return True
        except Exception as e:
            logger.warning(f"Outlook 健康检查失败: {e}")

        self.update_status(False, EmailServiceError("健康检查失败"))
        return False

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return [
            {
                "email": a.email,
                "id": a.email,
                "has_oauth": a.has_oauth(),
                "type": "outlook",
            }
            for a in self.accounts
        ]

    def delete_email(self, email_id: str) -> bool:
        logger.warning(f"Outlook 服务不支持删除账户: {email_id}")
        return False

    def get_account_stats(self) -> Dict[str, Any]:
        total = len(self.accounts)
        oauth_count = sum(1 for a in self.accounts if a.has_oauth())
        return {
            "total_accounts": total,
            "oauth_accounts": oauth_count,
            "password_accounts": total - oauth_count,
            "accounts": [a.to_dict() for a in self.accounts],
            "health_status": self.health_checker.get_status(),
        }

    def add_account(self, account_config: Dict[str, Any]) -> bool:
        try:
            account = OutlookAccount.from_config(account_config)
            if not account.validate():
                return False
            self.accounts.append(account)
            logger.info(f"添加 Outlook 账户: {account.email}")
            return True
        except Exception as e:
            logger.error(f"添加 Outlook 账户失败: {e}")
            return False

    def remove_account(self, email: str) -> bool:
        for i, a in enumerate(self.accounts):
            if a.email.lower() == email.lower():
                self.accounts.pop(i)
                logger.info(f"移除 Outlook 账户: {email}")
                return True
        return False

    def reset_health(self):
        self.health_checker.reset()
        logger.info("已重置 IMAP_NEW 健康状态")
