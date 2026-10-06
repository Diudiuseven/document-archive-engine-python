"""领域模型：文档状态机、保留策略、法务保全、任务与批次结果。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class DocumentStatus(str, enum.Enum):
    """文档生命周期状态机。

    合法流转：
        ACTIVE -> ARCHIVING -> ARCHIVED -> PENDING_DESTRUCTION -> DESTROYED
    法务保全不改变状态本身，只在转换层拦截销毁。
    """

    ACTIVE = "active"
    ARCHIVING = "archiving"
    ARCHIVED = "archived"
    PENDING_DESTRUCTION = "pending_destruction"
    DESTROYED = "destroyed"


#: 允许的状态流转表（引擎强制校验）。
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.ACTIVE: frozenset({DocumentStatus.ARCHIVING}),
    DocumentStatus.ARCHIVING: frozenset(
        {DocumentStatus.ARCHIVED, DocumentStatus.ACTIVE}  # 失败/取消可回滚
    ),
    DocumentStatus.ARCHIVED: frozenset({DocumentStatus.PENDING_DESTRUCTION}),
    DocumentStatus.PENDING_DESTRUCTION: frozenset(
        {DocumentStatus.DESTROYED, DocumentStatus.ARCHIVED}  # 保全拦截可回退
    ),
    DocumentStatus.DESTROYED: frozenset(),
}


class FailureReason(str, enum.Enum):
    """单文档处理失败的可区分原因。"""

    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    LOCKED = "locked"
    LEGAL_HOLD = "legal_hold"
    DATA_MISSING = "data_missing"
    DATA_CORRUPTED = "data_corrupted"
    INVALID_STATE = "invalid_state"
    INTERNAL = "internal"


@dataclass(frozen=True)
class RetentionPolicy:
    """保留策略。

    active_days:    生效期天数，到期后进入归档流程。
    archived_days:  归档保留天数，到期后进入待销毁。
    version:        策略版本，每次变更递增；重算只影响未完成文档。
    """

    policy_id: str
    active_days: int
    archived_days: int
    version: int = 1


@dataclass(frozen=True)
class Document:
    """文档元数据（内容单独存取）。"""

    document_id: str
    policy_id: str
    status: DocumentStatus
    created_at: datetime
    archive_due_at: Optional[datetime]      # 到期应归档时间
    destroy_due_at: Optional[datetime]      # 到期应销毁时间
    policy_version: int                     # 计算到期时间时使用的策略版本
    archived_at: Optional[datetime] = None
    destroyed_at: Optional[datetime] = None
    content_checksum: Optional[str] = None  # 归档时记录的原文校验和


@dataclass(frozen=True)
class LegalHold:
    """法务保全。"""

    hold_id: str
    document_id: str
    reason: str
    placed_at: datetime
    released_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self.released_at is None


@dataclass(frozen=True)
class DocumentFailure:
    """单文档失败记录。"""

    document_id: str
    reason: FailureReason
    detail: str


@dataclass
class BatchResult:
    """一批任务的推进结果。"""

    processed: list[str] = field(default_factory=list)
    failures: list[DocumentFailure] = field(default_factory=list)
    cancelled: bool = False

    @property
    def succeeded(self) -> int:
        return len(self.processed)
