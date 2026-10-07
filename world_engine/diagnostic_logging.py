"""本机运行诊断日志的凭据遮蔽工具。"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable

_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|passwd|secret)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;&}]+)"
)


def redact_sensitive_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Yuki 在写本机诊断日志前遮蔽已知密钥和常见凭据字段。"""
    text = str(value)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "[已遮蔽]")
    text = _BEARER.sub(r"\1[已遮蔽]", text)
    return _ASSIGNMENT.sub(r"\1[已遮蔽]", text)


def diagnostic_json(value: object, secrets: Iterable[str] = ()) -> str:
    """Yuki 用 UTF-8 友好的 JSON 记录诊断数据，再统一遮蔽凭据。"""
    return redact_sensitive_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), secrets,
    )


def configure_runtime_logging(debug_model_io: bool) -> None:
    """Yuki 把应用日志写到后台进程的标准错误，供启动器实时跟随。"""
    if sys.platform == "win32":
        # Yuki 让重定向到文件的 stdout/stderr 明确使用 UTF-8，避免日志查看器读出乱码。
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="backslashreplace")
    logger = logging.getLogger("virtual-world")
    # Yuki 让各子模块的日志继承统一级别，确保调试提示词不会被父级过滤。
    logger.setLevel(logging.DEBUG if debug_model_io else logging.INFO)
    handler = next(
        (item for item in logger.handlers if getattr(item, "_virtual_world_runtime", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._virtual_world_runtime = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False
