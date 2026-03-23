"""
登录流程引擎
复用 RegistrationEngine 主流程，并补充 LoginEngine 入口所需的重试与清理语义。
"""

import time
from typing import Optional

from .register import RegistrationEngine, RegistrationResult, WORKSPACE_PROBE_BACKOFF_DELAYS


class LoginEngine(RegistrationEngine):
    """登录引擎。"""

    def _get_workspace_id(self, log_missing: bool = True) -> Optional[str]:
        """为 LoginEngine 入口保留 Workspace 探测 backoff 语义。"""
        max_attempts = len(WORKSPACE_PROBE_BACKOFF_DELAYS) + 1

        for attempt in range(1, max_attempts + 1):
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
            if auth_cookie:
                workspace_id, source = self._decode_workspace_id_from_auth_cookie(auth_cookie)
            else:
                workspace_id, source = None, "auth_cookie_missing"
            if workspace_id:
                self._log(f"Workspace ID 解析成功: {workspace_id} (source={source})")
                return workspace_id

            if attempt == max_attempts:
                if log_missing:
                    if source == "auth_cookie_missing":
                        self._log("未能获取到授权 Cookie", "error")
                    else:
                        self._log(f"授权 Cookie 中未解析到 workspace: {source}", "warning")
                break

            wait_seconds = WORKSPACE_PROBE_BACKOFF_DELAYS[attempt - 1]
            self._log(
                f"Workspace 尚未就绪，等待 {wait_seconds:.1f} 秒后重试 "
                f"(第 {attempt}/{max_attempts} 次，source={source})",
                "warning",
            )
            time.sleep(wait_seconds)

        return None

    def run(self) -> RegistrationResult:
        """执行登录引擎流程，并显式保留清理钩子。"""
        try:
            return super().run()
        finally:
            self.close()
