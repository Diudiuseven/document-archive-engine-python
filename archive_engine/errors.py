"""错误类型定义：所有可区分的失败原因都在这里。"""

from __future__ import annotations


class ArchiveEngineError(Exception):
    """引擎所有错误的基类。"""


class DocumentNotFoundError(ArchiveEngineError):
    """文档不存在。"""


class PolicyNotFoundError(ArchiveEngineError):
    """保留策略不存在。"""


class InvalidStateTransitionError(ArchiveEngineError):
    """非法的文档状态流转。"""

    def __init__(self, document_id: str, current: str, target: str) -> None:
        self.document_id = document_id
        self.current = current
        self.target = target
        super().__init__(
            f"document {document_id!r}: cannot transition {current} -> {target}"
        )


class DocumentLockedError(ArchiveEngineError):
    """文档正在被其他任务处理（并发冲突）。"""


class LegalHoldActiveError(ArchiveEngineError):
    """文档命中法务保全，禁止销毁。"""


class TaskCancelledError(ArchiveEngineError):
    """任务被取消。"""


class TaskTimeoutError(ArchiveEngineError):
    """任务处理超时。"""


class ArchiveDataMissingError(ArchiveEngineError):
    """归档底层数据缺失。"""


class ArchiveDataCorruptedError(ArchiveEngineError):
    """归档底层数据损坏（校验和不匹配）。"""


class ProcessingFailedError(ArchiveEngineError):
    """单个文档处理失败（可重试）。"""
