"""领域模型与错误类型定义（骨架：仅类型定义，无实现逻辑）。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class DocumentStatus(str, enum.Enum):
    """文档生命周期状态。任何时刻读取只能看到其中一个完整状态。"""

    ACTIVE = "active"                # 生效
    ARCHIVING = "archiving"          # 归档中
    ARCHIVED = "archived"            # 已归档
    PENDING_DESTRUCTION = "pending_destruction"  # 待销毁
    DESTROYED = "destroyed"          # 已销毁


class FailureReason(str, enum.Enum):
    """单文档处理失败的可区分原因。"""

    CONTENT_MISSING = "content_missing"      # 底层原文缺失
    CONTENT_CORRUPT = "content_corrupt"      # 校验和不匹配
    LEGAL_HOLD = "legal_hold"                # 命中法务保全
    INVALID_STATE = "invalid_state"          # 状态不允许该流转
    CANCELLED = "cancelled"                  # 任务被取消
    TIMEOUT = "timeout"                      # 处理超时
    INTERNAL = "internal"                    # 其他内部错误


class ArchiveError(Exception):
    """归档引擎基础错误。"""


class ContentMissingError(ArchiveError):
    """归档底层数据缺失。"""


class ContentCorruptError(ArchiveError):
    """归档数据校验失败（损坏）。"""


class InvalidStateError(ArchiveError):
    """非法状态流转。"""


@dataclass(frozen=True)
class RetentionPolicy:
    """保留策略：文档创建后 retain_days 天到期。"""

    policy_id: str
    retain_days: int
    version: int = 1


@dataclass
class Document:
    doc_id: str
    policy_id: str
    status: DocumentStatus
    created_at: float
    expires_at: float
    policy_version: int = 1
    archive_id: Optional[str] = None
    updated_at: float = 0.0


@dataclass(frozen=True)
class ProcessResult:
    """单个文档一次处理的结果（成功或带原因的失败）。"""

    doc_id: str
    ok: bool
    reason: Optional[FailureReason] = None
    detail: str = ""


# 允许的状态流转表：引擎与存储层共同遵守，除此之外的流转一律拒绝。
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.ACTIVE: frozenset({DocumentStatus.ARCHIVING}),
    DocumentStatus.ARCHIVING: frozenset({
        DocumentStatus.ARCHIVED,
        DocumentStatus.ACTIVE,  # 失败/取消/超时回滚
    }),
    DocumentStatus.ARCHIVED: frozenset({DocumentStatus.PENDING_DESTRUCTION}),
    DocumentStatus.PENDING_DESTRUCTION: frozenset({DocumentStatus.DESTROYED}),
    DocumentStatus.DESTROYED: frozenset(),
}


def can_transition(src: DocumentStatus, dst: DocumentStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())
