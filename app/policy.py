"""保留策略服务。

策略以版本递增方式更新；更新后仅对仍处于 ACTIVE 的文档
按新策略重算 expires_at（基于原 created_at，不漂移）。
已进入归档/销毁流程或已完结的文档及其历史结果不被追溯改写。
"""
from __future__ import annotations

from .models import RetentionPolicy


class PolicyService:
    def __init__(self, store) -> None:
        self._store = store

    def set_policy(self, policy: RetentionPolicy) -> int:
        """写入新策略版本并重算受影响文档，返回重算条数。"""
        existing = self._store.get_policy(policy.policy_id)
        if existing is not None and policy.version <= existing.version:
            raise ValueError(
                f"policy version must increase: {policy.version} <= {existing.version}")
        self._store.upsert_policy(policy)
        return self._store.recompute_expiry(policy)

    def get_policy(self, policy_id: str) -> RetentionPolicy | None:
        return self._store.get_policy(policy_id)
