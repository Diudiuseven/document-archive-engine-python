"""保留策略服务：策略变更后对已有文档按新策略重算到期时间。

重算只影响尚未处理完成的文档（active/archived 且未进入销毁终态），
已归档完成时间与已销毁记录等历史结果不被追溯改写。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .errors import PolicyNotFoundError
from .models import Document, DocumentStatus, RetentionPolicy
from .storage import Storage

#: 终态或进入销毁流程的状态不参与重算（历史结果不可改写）。
_NON_RECOMPUTABLE = frozenset(
    {DocumentStatus.PENDING_DESTRUCTION, DocumentStatus.DESTROYED}
)


class PolicyService:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def create_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        raise NotImplementedError

    def update_policy(
        self,
        policy_id: str,
        *,
        active_days: Optional[int] = None,
        archived_days: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> RetentionPolicy:
        """更新策略并分页重算受影响文档的到期时间。返回新版本策略。"""
        raise NotImplementedError

    def get_policy(self, policy_id: str) -> RetentionPolicy:
        policy = self._storage.get_policy(policy_id)
        if policy is None:
            raise PolicyNotFoundError(policy_id)
        return policy

    def compute_schedule(
        self, policy: RetentionPolicy, created_at: datetime
    ) -> tuple[datetime, datetime]:
        """按策略计算 (archive_due_at, destroy_due_at)。"""
        archive_due = created_at + timedelta(days=policy.active_days)
        destroy_due = archive_due + timedelta(days=policy.archived_days)
        return archive_due, destroy_due

    def _recompute_documents(
        self, policy: RetentionPolicy, now: datetime
    ) -> int:
        """游标分页重算，返回受影响文档数。"""
        raise NotImplementedError
