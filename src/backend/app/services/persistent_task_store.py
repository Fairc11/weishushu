"""本人归档单任务的本地持久记录。"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from weibo_book.errors import WeiboError, WeiboErrorKind

PersistentTaskState = Literal[
    "running",
    "pausing",
    "waiting_resume",
    "cancelling",
    "done",
    "error",
    "cancelled",
    "abandoned",
]
PersistentTaskMode = Literal["create", "incremental", "rebuild", "update"]
PersistentTaskPhase = Literal[
    "sync", "render", "bloggers", "supertopics", "duration"
]
PersistentPacingMode = Literal[
    "standard",
    "low_2_3_hours",
    "low_4_6_hours",
    "low_8_12_hours",
]
PersistentPacingState = Literal[
    "standard",
    "estimating",
    "waiting",
    "requesting",
    "power_saving",
    "paused",
]
PersistentRequestKind = Literal["profile", "detail", "comments", "media"]

_TASK_ID_RE = re.compile(r"[0-9a-f]{12}")
_ALLOWED_STATES = {
    "running", "pausing", "waiting_resume", "cancelling",
    "done", "error", "cancelled", "abandoned",
}
_ALLOWED_MODES = {"create", "incremental", "rebuild", "update"}
_ALLOWED_PHASES = {"sync", "render", "bloggers", "supertopics", "duration"}
_ALLOWED_PACING_MODES = {
    "standard", "low_2_3_hours", "low_4_6_hours", "low_8_12_hours",
}
_ALLOWED_PACING_STATES = {
    "standard", "estimating", "waiting", "requesting", "power_saving", "paused",
}
_ALLOWED_REQUEST_KINDS = {"profile", "detail", "comments", "media"}
_MAX_RECORD_BYTES = 1024 * 1024
_IS_WINDOWS = os.name == "nt"


class PersistentTaskStoreError(WeiboError):
    """持久任务记录无法安全读取或写入。"""


@dataclass(frozen=True)
class PersistentTaskRecord:
    schema_version: int
    task_id: str
    task_kind: Literal["personal_archive", "following_archive"]
    mode: PersistentTaskMode
    output_dir: str
    state: PersistentTaskState
    phase: PersistentTaskPhase
    archive_run_id: str | None
    progress_current: int
    progress_total: int | None
    progress_unit: str
    started_at: str
    saved_at: str
    pause_reason: str
    saved_content: str
    expected_uid: str | None = None
    legacy_index_sha256: str | None = None
    error_recoverable: bool = False
    pacing_mode: PersistentPacingMode = "standard"
    keep_awake_when_plugged: bool = False
    pacing_state: PersistentPacingState = "standard"
    pacing_request_kind: PersistentRequestKind | None = None
    next_wait_seconds: float | None = None
    checkpoint: dict[str, object] = field(default_factory=dict)
    target_label: str | None = None


def _is_non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_aware_timestamp(value: object) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_canonical_uuid(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _valid_following_checkpoint(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "snapshot_id",
        "blogger_next_page",
        "blogger_next_cursor",
        "blogger_completed_count",
        "bloggers_done",
        "supertopics_done",
        "blogger_reported_total",
        "supertopic_reported_total",
    }:
        return False
    snapshot_id = value["snapshot_id"]
    page = value["blogger_next_page"]
    cursor = value["blogger_next_cursor"]
    completed_count = value["blogger_completed_count"]
    bloggers_done = value["bloggers_done"]
    supertopics_done = value["supertopics_done"]
    blogger_total = value["blogger_reported_total"]
    supertopic_total = value["supertopic_reported_total"]
    if (
        not isinstance(snapshot_id, str)
        or not _is_canonical_uuid(snapshot_id)
        or type(page) is not int
        or page < 1
        or not _is_non_negative_int(completed_count)
        or type(bloggers_done) is not bool
        or type(supertopics_done) is not bool
        or (blogger_total is not None and not _is_non_negative_int(blogger_total))
        or (supertopic_total is not None and not _is_non_negative_int(supertopic_total))
    ):
        return False
    if cursor is not None and (type(cursor) is not int or cursor < 0):
        return False
    if not bloggers_done:
        if supertopic_total is not None or supertopics_done:
            return False
        return (
            page == 1
            and cursor is None
            and completed_count == 0
            and blogger_total is None
        ) or (
            page > 1
            and type(cursor) is int
            and cursor > 0
            and _is_non_negative_int(blogger_total)
            and 0 < completed_count <= blogger_total
        )
    if cursor != 0 or blogger_total is None or completed_count != blogger_total:
        return False
    return (supertopics_done and supertopic_total is not None) or (
        not supertopics_done and supertopic_total is None
    )


def _validate_record_payload(payload: object) -> PersistentTaskRecord:
    current_fields = {field.name for field in fields(PersistentTaskRecord)}
    schema_one_fields = current_fields - {
        "expected_uid", "legacy_index_sha256", "error_recoverable",
        "pacing_mode", "keep_awake_when_plugged", "pacing_state",
        "pacing_request_kind", "next_wait_seconds", "checkpoint",
        "target_label",
    }
    schema_two_fields = current_fields - {
        "legacy_index_sha256", "error_recoverable",
        "pacing_mode", "keep_awake_when_plugged", "pacing_state",
        "pacing_request_kind", "next_wait_seconds", "checkpoint",
        "target_label",
    }
    schema_three_fields = current_fields - {
        "error_recoverable", "pacing_mode", "keep_awake_when_plugged",
        "pacing_state", "pacing_request_kind", "next_wait_seconds", "checkpoint",
        "target_label",
    }
    schema_four_fields = current_fields - {
        "pacing_mode", "keep_awake_when_plugged", "pacing_state",
        "pacing_request_kind", "next_wait_seconds", "checkpoint",
        "target_label",
    }
    schema_five_fields = current_fields - {"checkpoint", "target_label"}
    schema_six_fields = current_fields - {"target_label"}
    if not isinstance(payload, dict):
        raise PersistentTaskStoreError(
            "持久任务记录字段不完整或包含未知字段",
            kind=WeiboErrorKind.PARSE,
        )
    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is int
        and schema_version == 1
        and set(payload) == schema_one_fields
    ):
        payload = dict(payload)
        payload["schema_version"] = 7
        payload["expected_uid"] = None
        payload["legacy_index_sha256"] = None
        payload["error_recoverable"] = payload["state"] == "error"
        payload["target_label"] = None
    elif (
        type(schema_version) is int
        and schema_version == 2
        and set(payload) == schema_two_fields
    ):
        payload = dict(payload)
        payload["schema_version"] = 7
        payload["legacy_index_sha256"] = None
        payload["error_recoverable"] = payload["state"] == "error"
        payload["target_label"] = None
    elif (
        type(schema_version) is int
        and schema_version == 3
        and set(payload) == schema_three_fields
    ):
        payload = dict(payload)
        payload["schema_version"] = 7
        payload["error_recoverable"] = payload["state"] == "error"
        payload["target_label"] = None
    elif (
        type(schema_version) is int
        and schema_version == 4
        and set(payload) == schema_four_fields
    ):
        payload = dict(payload)
        payload["schema_version"] = 7
        payload["target_label"] = None
    elif (
        type(schema_version) is int
        and schema_version == 5
        and set(payload) == schema_five_fields
    ):
        payload = dict(payload)
        payload["schema_version"] = 7
        payload["target_label"] = None
    elif (
        type(schema_version) is int
        and schema_version == 6
        and set(payload) == schema_six_fields
    ):
        payload = dict(payload)
        payload["schema_version"] = 7
        payload["target_label"] = None
    elif (
        type(schema_version) is not int
        or schema_version != 7
        or set(payload) != current_fields
    ):
        raise PersistentTaskStoreError(
            "持久任务记录字段不完整或包含未知字段",
            kind=WeiboErrorKind.PARSE,
        )

    payload.setdefault("pacing_mode", "standard")
    payload.setdefault("keep_awake_when_plugged", False)
    payload.setdefault("pacing_state", "standard")
    payload.setdefault("pacing_request_kind", None)
    payload.setdefault("next_wait_seconds", None)
    payload.setdefault("checkpoint", {})

    output_dir = payload["output_dir"]
    current = payload["progress_current"]
    total = payload["progress_total"]
    valid = (
        type(payload["schema_version"]) is int
        and payload["schema_version"] == 7
        and isinstance(payload["task_id"], str)
        and _TASK_ID_RE.fullmatch(payload["task_id"]) is not None
        and payload["task_kind"] in {"personal_archive", "following_archive"}
        and isinstance(payload["mode"], str)
        and payload["mode"] in _ALLOWED_MODES
        and isinstance(output_dir, str)
        and bool(output_dir)
        and Path(output_dir).is_absolute()
        and isinstance(payload["state"], str)
        and payload["state"] in _ALLOWED_STATES
        and isinstance(payload["phase"], str)
        and payload["phase"] in _ALLOWED_PHASES
        and _is_canonical_uuid(payload["archive_run_id"])
        and _is_non_negative_int(current)
        and (total is None or _is_non_negative_int(total))
        and (total is None or current <= total)
        and isinstance(payload["progress_unit"], str)
        and bool(payload["progress_unit"])
        and _is_aware_timestamp(payload["started_at"])
        and _is_aware_timestamp(payload["saved_at"])
        and isinstance(payload["pause_reason"], str)
        and isinstance(payload["saved_content"], str)
        and type(payload["error_recoverable"]) is bool
        and (
            payload["state"] == "error"
            or payload["error_recoverable"] is False
        )
        and (
            payload["expected_uid"] is None
            or (
                isinstance(payload["expected_uid"], str)
                and bool(payload["expected_uid"].strip())
            )
        )
        and (
            payload["legacy_index_sha256"] is None
            or (
                isinstance(payload["legacy_index_sha256"], str)
                and len(payload["legacy_index_sha256"]) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in payload["legacy_index_sha256"]
                )
            )
        )
        and (
            payload["target_label"] is None
            or (
                isinstance(payload["target_label"], str)
                and bool(payload["target_label"].strip())
                and len(payload["target_label"]) <= 64
            )
        )
        and isinstance(payload["pacing_mode"], str)
        and payload["pacing_mode"] in _ALLOWED_PACING_MODES
        and type(payload["keep_awake_when_plugged"]) is bool
        and isinstance(payload["pacing_state"], str)
        and payload["pacing_state"] in _ALLOWED_PACING_STATES
        and (
            payload["pacing_request_kind"] is None
            or (
                isinstance(payload["pacing_request_kind"], str)
                and payload["pacing_request_kind"] in _ALLOWED_REQUEST_KINDS
            )
        )
        and (
            payload["next_wait_seconds"] is None
            or (
                type(payload["next_wait_seconds"]) in {int, float}
                and math.isfinite(payload["next_wait_seconds"])
                and payload["next_wait_seconds"] >= 0
            )
        )
        and (
            payload["pacing_mode"] != "standard"
            or (
                payload["pacing_state"] == "standard"
                and payload["pacing_request_kind"] is None
                and payload["next_wait_seconds"] is None
            )
        )
        and (
            (
                payload["task_kind"] == "personal_archive"
                and payload["mode"] in {"create", "incremental", "rebuild"}
                and payload["phase"] in {"sync", "render"}
                and payload["checkpoint"] == {}
            )
            or (
                payload["task_kind"] == "following_archive"
                and payload["mode"] == "update"
                and payload["phase"] in {"bloggers", "supertopics", "duration"}
                and payload["archive_run_id"] is None
                and payload["legacy_index_sha256"] is None
                and isinstance(payload["expected_uid"], str)
                and payload["expected_uid"].isdigit()
                and _valid_following_checkpoint(payload["checkpoint"])
            )
        )
    )
    if not valid:
        raise PersistentTaskStoreError(
            "持久任务记录包含无效值",
            kind=WeiboErrorKind.PARSE,
        )
    return PersistentTaskRecord(**payload)


class PersistentTaskStore:
    """用同目录原子替换保存唯一活动的本人归档任务。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _reject_unsafe_marker(path: Path, marker: os.stat_result) -> None:
        if stat.S_ISLNK(marker.st_mode):
            raise PersistentTaskStoreError(
                "持久任务记录不能是符号链接",
                kind=WeiboErrorKind.PARSE,
            )
        if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
            raise PersistentTaskStoreError(
                "持久任务记录必须是单链接普通文件",
                kind=WeiboErrorKind.PARSE,
            )
        if marker.st_size > _MAX_RECORD_BYTES:
            raise PersistentTaskStoreError(
                "持久任务记录大小异常",
                kind=WeiboErrorKind.PARSE,
            )

    def load(self) -> PersistentTaskRecord | None:
        try:
            marker = self.path.lstat()
        except FileNotFoundError:
            return None
        self._reject_unsafe_marker(self.path, marker)

        descriptor = -1
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            current = self.path.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (current.st_dev, current.st_ino, current.st_size)
            ):
                raise PersistentTaskStoreError(
                    "持久任务记录在读取时已变化",
                    kind=WeiboErrorKind.PARSE,
                )
            chunks: list[bytes] = []
            remaining = _MAX_RECORD_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _MAX_RECORD_BYTES:
                raise PersistentTaskStoreError(
                    "持久任务记录大小异常",
                    kind=WeiboErrorKind.PARSE,
                )
            payload = json.loads(raw.decode("utf-8"))
        except PersistentTaskStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PersistentTaskStoreError(
                "读取持久任务记录失败",
                kind=WeiboErrorKind.PARSE,
                original=exc,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        validated = _validate_record_payload(payload)
        if payload.get("schema_version") != validated.schema_version:
            self.save(validated)
        return validated

    def save(self, record: PersistentTaskRecord) -> None:
        validated = _validate_record_payload(asdict(record))
        parent = self.path.parent
        temporary: Path | None = None
        descriptor = -1
        try:
            parent.mkdir(parents=True, exist_ok=True)
            parent_marker = parent.lstat()
            if stat.S_ISLNK(parent_marker.st_mode) or not stat.S_ISDIR(parent_marker.st_mode):
                raise PersistentTaskStoreError(
                    "持久任务记录目录不安全",
                    kind=WeiboErrorKind.API,
                )
            try:
                existing = self.path.lstat()
            except FileNotFoundError:
                existing = None
            if existing is not None:
                self._reject_unsafe_marker(self.path, existing)

            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=parent,
            )
            temporary = Path(name)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            content = (
                json.dumps(
                    asdict(validated),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            os.chmod(self.path, 0o600)
            if not _IS_WINDOWS:
                directory_fd = os.open(
                    parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except PersistentTaskStoreError:
            raise
        except OSError as exc:
            raise PersistentTaskStoreError(
                "保存持久任务记录失败",
                kind=WeiboErrorKind.API,
                original=exc,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def clear(self) -> None:
        try:
            marker = self.path.lstat()
        except FileNotFoundError:
            return
        self._reject_unsafe_marker(self.path, marker)
        try:
            self.path.unlink()
            if not _IS_WINDOWS:
                directory_fd = os.open(
                    self.path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise PersistentTaskStoreError(
                "清理持久任务记录失败",
                kind=WeiboErrorKind.API,
                original=exc,
            ) from exc

    def reconcile_after_process_start(self) -> PersistentTaskRecord | None:
        record = self.load()
        if record is None:
            return None
        if record.state in {"done", "cancelled", "abandoned"}:
            self.clear()
            return None
        if record.state in {"running", "pausing"}:
            record = replace(
                record,
                state="waiting_resume",
                saved_at=datetime.now(timezone.utc).isoformat(),
                pause_reason="unexpected_exit",
            )
            self.save(record)
        return record
