"""归档内容存储。

每个归档单元由两个文件组成：
- <archive_id>.bin  ：原文内容
- <archive_id>.meta ：JSON 元数据，含 sha256 校验和与原始 doc_id

读取时强制校验：
- 文件缺失      -> ContentMissingError
- 校验和不匹配  -> ContentCorruptError
绝不静默返回空结果。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading

from .models import ContentCorruptError, ContentMissingError


class ArchiveStore:
    def __init__(self, root: str) -> None:
        self._root = root
        self._lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    def _paths(self, archive_id: str) -> tuple[str, str]:
        if not archive_id or os.sep in archive_id or ".." in archive_id:
            raise ValueError(f"bad archive_id: {archive_id!r}")
        base = os.path.join(self._root, archive_id)
        return base + ".bin", base + ".meta"

    def put(self, archive_id: str, content: bytes, metadata: dict) -> str:
        """原子写入归档内容（临时文件 + rename），返回 sha256 校验和。"""
        checksum = hashlib.sha256(content).hexdigest()
        meta = dict(metadata)
        meta["sha256"] = checksum
        meta["size"] = len(content)
        bin_path, meta_path = self._paths(archive_id)
        with self._lock:
            # 先写临时文件再 rename，崩溃不会留下半个归档文件。
            for path, data in ((bin_path, content),
                               (meta_path, json.dumps(meta).encode())):
                fd, tmp = tempfile.mkstemp(dir=self._root)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                except BaseException:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
        return checksum

    def read_verified(self, archive_id: str) -> tuple[bytes, dict]:
        """读取并校验归档内容；缺失/损坏抛出可区分错误。"""
        bin_path, meta_path = self._paths(archive_id)
        if not os.path.exists(bin_path) or not os.path.exists(meta_path):
            raise ContentMissingError(f"archive {archive_id} not found")
        with open(meta_path, "rb") as f:
            meta = json.loads(f.read().decode())
        with open(bin_path, "rb") as f:
            content = f.read()
        if hashlib.sha256(content).hexdigest() != meta.get("sha256"):
            raise ContentCorruptError(f"archive {archive_id} checksum mismatch")
        return content, meta

    def remove(self, archive_id: str) -> None:
        bin_path, meta_path = self._paths(archive_id)
        with self._lock:
            for path in (bin_path, meta_path):
                if os.path.exists(path):
                    os.unlink(path)
