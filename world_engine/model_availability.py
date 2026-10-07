"""同一进程内共享模型额度状态，避免余额不足时反复打满请求队列。"""

import time
from threading import Lock


class ModelBillingError(RuntimeError):
    """模型服务端明确拒绝当前账户的付费请求。"""


_PAYMENT_MESSAGE = "DeepSeek 返回 HTTP 402：账户余额或额度不足，请检查 DeepSeek 账户后重试"
_COOLDOWN_SECONDS = 60.0
_blocked_until: dict[str, float] = {}
_lock = Lock()


def check_model_billing(base_url: str) -> None:
    with _lock:
        if time.monotonic() < _blocked_until.get(base_url, 0):
            raise ModelBillingError(_PAYMENT_MESSAGE)


def record_model_billing_failure(base_url: str) -> None:
    with _lock:
        _blocked_until[base_url] = time.monotonic() + _COOLDOWN_SECONDS
    raise ModelBillingError(_PAYMENT_MESSAGE)


def is_model_billing_error(exc: BaseException) -> bool:
    while exc is not None:
        if isinstance(exc, ModelBillingError):
            return True
        exc = exc.__cause__
    return False
