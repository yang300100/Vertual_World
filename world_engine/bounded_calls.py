"""有限并发的只读外部调用；超时后丢弃结果，不等待线程退出。"""

from concurrent.futures import Future
from threading import BoundedSemaphore, Thread

_SLOTS = BoundedSemaphore(16)


def submit_call(function, *args, **kwargs) -> Future:
    """不强杀运行中的请求，也不允许超时请求无限堆积。"""
    future = Future()
    if not _SLOTS.acquire(blocking=False):
        future.set_exception(RuntimeError("外部调用并发已满，请稍后重试"))
        return future

    def run() -> None:
        try:
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            _SLOTS.release()

    Thread(target=run, daemon=True, name="world-readonly-call").start()
    return future
