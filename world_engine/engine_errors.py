"""`WorldEngine` 拆分后共享的模块级异常。

原先定义在 `world_engine/engine.py`，为解开 Mixin 与引擎本体之间的
循环导入而独立成模块；`world_engine.engine` 仍按原路径重导出，
`from world_engine.engine import ConcurrentWorldUpdateError` 保持不变。
"""

from __future__ import annotations


class ConcurrentWorldUpdateError(RuntimeError):
    pass
