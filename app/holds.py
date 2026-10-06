"""法务保全服务。

保全只是给文档打上标记，不改变其生命周期状态；
执行引擎在销毁（以及归档推进）前检查该标记：
- 命中保全的文档被跳过并记录 LEGAL_HOLD 结果；
- 保全解除后文档仍在原状态，下一次扫描自然重新进入原进度，
  不会跳过也不会重复处理（状态流转由条件更新保证幂等）。
"""
from __future__ import annotations


class LegalHoldService:
    def __init__(self, store) -> None:
        self._store = store

    def place_hold(self, doc_id: str, reason: str = "") -> None:
        self._store.place_hold(doc_id, reason)

    def release_hold(self, doc_id: str) -> None:
        self._store.release_hold(doc_id)

    def is_held(self, doc_id: str) -> bool:
        return self._store.is_held(doc_id)
