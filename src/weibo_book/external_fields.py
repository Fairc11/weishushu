"""基于真实 fixture metadata 已验证路径的外部字段读取器。"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class ExternalFieldAdapter:
    """仅按 metadata.verified_paths 中的精确字段路径读取响应。"""

    def __init__(self, metadata: dict) -> None:
        verified_paths = metadata.get("verified_paths") if isinstance(metadata, dict) else None
        source_scope = metadata.get("source_scope") if isinstance(metadata, dict) else None
        if not isinstance(verified_paths, dict):
            raise ValueError("fixture metadata 缺少有效 verified_paths")
        if source_scope is not None and (
            not isinstance(source_scope, list)
            or not source_scope
            or any(not isinstance(segment, str) or not segment for segment in source_scope)
        ):
            raise ValueError("fixture metadata 的 source_scope 格式无效")
        for field_name, path in verified_paths.items():
            if (
                not isinstance(field_name, str)
                or not isinstance(path, list)
                or not path
                or any(not isinstance(segment, str) or not segment for segment in path)
            ):
                raise ValueError("fixture metadata 的 verified_paths 格式无效")
        self._verified_paths = verified_paths
        self._source_scope = source_scope

    def source_items(self, payload: dict) -> list:
        if self._source_scope is None:
            logger.debug("fixture metadata 缺少 source_scope")
            return []
        current = payload
        for segment in self._source_scope:
            if not isinstance(current, dict):
                logger.debug("fixture source_scope 路径缺失")
                return []
            current = current.get(segment)
            if current is None:
                logger.debug("fixture source_scope 路径缺失")
                return []
        if not isinstance(current, list):
            logger.debug("fixture source_scope 未指向列表")
            return []
        return current

    def read(self, payload: dict, field_name: str):
        path = self._verified_paths.get(field_name)
        if path is None:
            logger.debug("外部字段未经 fixture 验证: %s", field_name)
            return ""

        current = payload
        for segment in path:
            if not isinstance(current, dict):
                logger.debug("外部字段路径缺失: %s", field_name)
                return ""
            current = current.get(segment)
            if current is None:
                logger.debug("外部字段路径缺失: %s", field_name)
                return ""
        return current
