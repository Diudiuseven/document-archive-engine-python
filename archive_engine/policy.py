"""保留策略服务：策略变更后对已有文档按新策略重算到期时间。

重算只影响尚未处理完成的文档（active/archiving/archived），
已进入销毁流程或已销毁的文档属于历史结果，不被追溯改写。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import PolicyNotFoundError
from .models import Document, DocumentStatus, RetentionPolicy
from .storage import Storage

#: 终态或进入销毁流程的状态不参与重算（历史结果不可改写）。
_NON_RECOMPUTABLE = frozenset(
    {DocumentStatus.PENDING_DESTRUCTION, DocumentStatus.DESTROYED}
)

#: 重算时分页大小，避免一次性加载全部文档。
_RECOMPUTE_PAGE = 200


class PolicyService:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def create_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        self._storage.put_policy(policy)
        return policy

    def update_policy(
        self,
        policy_id: str,
        *,
        active_days: Optional[int] = None,
        archived_days: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> RetentionPolicy:
        """更新策略并分页重算受影响文档的到期时间。返回新版本策略。"""
        current = self.get_policy(policy_id)
        updated = RetentionPolicy(
            policy_id=policy_id,
            active_days=active_days if active_days is not None else current.active_days,
            archived_days=(
                archived_days if archived_days is not None else current.archived_days
            ),
            version=current.version + 1,
        )
        self._storage.put_policy(updated)
        self._recompute_documents(updated, now or datetime.now(timezone.utc))
        return updated

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
        affected = 0
        after_id = ""
        while True:
            page = self._storage.iter_documents_by_policy(
                policy.policy_id, after_id=after_id, limit=_RECOMPUTE_PAGE
            )
            if not page:
                break
            for doc in page:
                after_id = doc.document_id
                if doc.status in _NON_RECOMPUTABLE:
                    continue  # 历史结果不改写
                archive_due, destroy_due = self.compute_schedule(
                    policy, doc.created_at
                )
                self._storage.update_document_schedule(
                    doc.document_id, archive_due, destroy_due, policy.version
                )
                affected += 1
        return affected
