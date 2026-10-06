"""文档归档生命周期引擎。"""

from .errors import (
    ArchiveDataCorruptedError,
    ArchiveDataMissingError,
    ArchiveEngineError,
    DocumentLockedError,
    DocumentNotFoundError,
    InvalidStateTransitionError,
    LegalHoldActiveError,
    PolicyNotFoundError,
    ProcessingFailedError,
    TaskCancelledError,
    TaskTimeoutError,
)
from .models import (
    BatchResult,
    Document,
    DocumentFailure,
    DocumentStatus,
    FailureReason,
    LegalHold,
    RetentionPolicy,
)

__all__ = [
    "ArchiveDataCorruptedError",
    "ArchiveDataMissingError",
    "ArchiveEngineError",
    "BatchResult",
    "Document",
    "DocumentFailure",
    "DocumentLockedError",
    "DocumentNotFoundError",
    "DocumentStatus",
    "FailureReason",
    "InvalidStateTransitionError",
    "LegalHold",
    "LegalHoldActiveError",
    "PolicyNotFoundError",
    "ProcessingFailedError",
    "RetentionPolicy",
    "TaskCancelledError",
    "TaskTimeoutError",
]
