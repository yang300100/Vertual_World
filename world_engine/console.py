from __future__ import annotations

import sys


def configure_console_encoding() -> None:
    """让Windows终端稳定输出中文，同时保留不可编码字符的容错。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

