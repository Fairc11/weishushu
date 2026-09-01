"""v1.2.0 M3-10 拆分：备份索引服务（路径校验 + 索引读写 + 原子写）。

从 routers/router_backup.py 抽出。
目标：每个模块职责清楚（router 只管端点，service 管业务），测试按功能分开。

公开 API（被 router_backup.py 重导出，保持兼容）：
- INDEX_FILENAME
- _validate_output_dir
- read_index
- write_index
- stage_legacy_archive / restore_legacy_archive
- finalize_legacy_archive / cleanup_legacy_audit
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PureWindowsPath
from typing import Optional

from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.schemas import BackupIndex
from weibo_book.errors import WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)

INDEX_FILENAME = ".weishushu_index.json"
LEGACY_AUDIT_DIRECTORY = Path(".work") / "legacy"
LEGACY_FINALIZE_MARKER = ".weishushu-legacy-finalize.json"
_TASK_ID_RE = re.compile(r"[0-9a-f]{12}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_plain_directory(path: Path, message: str) -> None:
    try:
        marker = path.lstat()
    except OSError as exc:
        raise WeiboError(message, kind=WeiboErrorKind.API, original=exc) from exc
    if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
        raise WeiboError(message, kind=WeiboErrorKind.API)


def _require_no_symlink_chain(path: Path, message: str) -> None:
    """拒绝目标及用户路径层级中已有父目录的符号链接。

    文件系统根目录及其直接子项属于系统路径边界，不把 macOS 的
    ``/tmp``、``/var`` 等系统别名误判为用户目录链。
    """
    current = path
    while True:
        parent = current.parent
        if parent == current or parent.parent == parent:
            return
        try:
            marker = current.lstat()
        except FileNotFoundError:
            marker = None
        except OSError as exc:
            raise WeiboError(message, kind=WeiboErrorKind.API, original=exc) from exc
        if marker is not None:
            if stat.S_ISLNK(marker.st_mode):
                raise WeiboError(message, kind=WeiboErrorKind.API)
            if current != path and not stat.S_ISDIR(marker.st_mode):
                raise WeiboError(message, kind=WeiboErrorKind.API)
        current = parent


def _require_plain_file(path: Path, message: str) -> None:
    try:
        marker = path.lstat()
    except OSError as exc:
        raise WeiboError(message, kind=WeiboErrorKind.API, original=exc) from exc
    if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode):
        raise WeiboError(message, kind=WeiboErrorKind.API)


def legacy_stage_path(output_dir: str | Path, task_id: str) -> Path:
    """仅由输出目录和持久任务标识派生旧版目录的唯一暂存路径。"""
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise WeiboError("持久任务标识无效", kind=WeiboErrorKind.API)
    root = Path(output_dir)
    return root.parent / f".{root.name}.legacy-task-{task_id}"


def _legacy_delete_path(output_dir: str | Path, task_id: str) -> Path:
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise WeiboError("持久任务标识无效", kind=WeiboErrorKind.API)
    root = Path(output_dir)
    return root.parent / f".{root.name}.legacy-delete-task-{task_id}"


def _validate_legacy_stage(root: Path, staged: Path, task_id: str) -> None:
    _require_no_symlink_chain(root, "旧版备份路径包含符号链接")
    _require_no_symlink_chain(staged, "旧版备份暂存路径包含符号链接")
    if staged != legacy_stage_path(root, task_id):
        raise WeiboError("旧版备份暂存路径无效", kind=WeiboErrorKind.API)


def _read_regular_snapshot(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or identity
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        ):
            raise WeiboError("旧版备份索引在读取时已变化", kind=WeiboErrorKind.API)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        final_opened = os.fstat(descriptor)
        final_current = path.lstat()
        final_identity = (
            final_opened.st_dev,
            final_opened.st_ino,
            final_opened.st_size,
            final_opened.st_mtime_ns,
        )
        if (
            stat.S_ISLNK(final_current.st_mode)
            or final_identity != identity
            or final_identity
            != (
                final_current.st_dev,
                final_current.st_ino,
                final_current.st_size,
                final_current.st_mtime_ns,
            )
        ):
            raise WeiboError("旧版备份索引在读取时已变化", kind=WeiboErrorKind.API)
        return payload, identity
    finally:
        os.close(descriptor)


def _read_regular_bytes(path: Path) -> bytes:
    return _read_regular_snapshot(path)[0]


def _parse_legacy_index(payload: bytes) -> BackupIndex:
    try:
        return BackupIndex.model_validate_json(payload)
    except (ValidationError, UnicodeError, ValueError) as exc:
        raise WeiboError(
            "旧版备份索引已损坏，不能建立完整档案",
            kind=WeiboErrorKind.PARSE,
            original=exc,
        ) from exc


def _read_legacy_index(root: Path, message: str) -> BackupIndex:
    _require_no_symlink_chain(root, f"{message}：路径包含符号链接")
    _require_plain_directory(root, message)
    index_path = root / INDEX_FILENAME
    _require_plain_file(index_path, message)
    return _parse_legacy_index(_read_regular_bytes(index_path))


def staged_legacy_archive_uid(output_dir: str | Path, task_id: str) -> str:
    """从当前任务的精确暂存目录安全读取旧索引 UID。"""
    root = Path(output_dir)
    staged = legacy_stage_path(root, task_id)
    _validate_legacy_stage(root, staged, task_id)
    return _read_legacy_index(staged, "旧版备份暂存目录无法安全读取").uid


def staged_legacy_archive_sha256(output_dir: str | Path, task_id: str) -> str:
    root = Path(output_dir)
    staged = legacy_stage_path(root, task_id)
    _validate_legacy_stage(root, staged, task_id)
    _require_plain_directory(staged, "旧版备份暂存目录无法安全读取")
    _require_plain_file(staged / INDEX_FILENAME, "旧版备份索引无法安全读取")
    return hashlib.sha256(_read_regular_bytes(staged / INDEX_FILENAME)).hexdigest()


def staged_legacy_archive_exists(output_dir: str | Path, task_id: str) -> bool:
    """安全检查当前任务的精确旧版暂存目录是否存在。"""
    root = Path(output_dir)
    staged = legacy_stage_path(root, task_id)
    try:
        staged.lstat()
    except FileNotFoundError:
        return False
    _validate_legacy_stage(root, staged, task_id)
    return True


def stage_legacy_archive(
    output_dir: str | Path,
    expected_uid: str,
    task_id: str,
) -> Path:
    """暂时移开旧版目录，为首次完整建档腾出原路径。

    旧索引不参与正文数据库转换。旧版 ``BackupIndex`` 没有媒体 URL
    到本地文件的可验证映射，因此这里也不导入旧媒体。
    """
    root = Path(output_dir)
    _require_no_symlink_chain(root, "旧版备份路径包含符号链接")
    index = _read_legacy_index(root, "旧版备份目录无法安全读取")
    if index.uid != expected_uid:
        raise WeiboError(
            "该旧版备份属于其他账号，不允许覆盖",
            kind=WeiboErrorKind.AUTH,
        )
    staged = legacy_stage_path(root, task_id)
    _require_no_symlink_chain(staged, "旧版备份暂存路径包含符号链接")
    if staged.exists() or staged.is_symlink():
        raise WeiboError(
            "当前任务的旧版备份暂存目录已存在",
            kind=WeiboErrorKind.API,
        )
    try:
        os.replace(root, staged)
        _fsync_directory(root.parent)
    except OSError as exc:
        raise WeiboError("无法暂存旧版备份目录", original=exc) from exc
    return staged


def restore_legacy_archive(
    output_dir: str | Path,
    staged: Path,
    task_id: str,
    expected_uid: str,
) -> None:
    """首次完整建档失败时原样恢复旧版目录。"""
    root = Path(output_dir)
    staged = Path(staged)
    _validate_legacy_stage(root, staged, task_id)
    index = _read_legacy_index(staged, "旧版备份暂存目录无法安全恢复")
    if index.uid != expected_uid:
        raise WeiboError("旧版备份账号与当前任务不一致", kind=WeiboErrorKind.AUTH)
    if root.exists():
        _require_plain_directory(root, "首次建档失败后原目录已变化，旧版备份已保留在暂存目录")
        if any(root.iterdir()):
            raise WeiboError(
                "首次建档失败后原目录已变化，旧版备份已保留在暂存目录",
                kind=WeiboErrorKind.API,
            )
        root.rmdir()
    os.replace(staged, root)
    _fsync_directory(root.parent)


def _copy_legacy_index(source: Path, target: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    target_fd: int | None = None
    try:
        opened = os.fstat(source_fd)
        current = source.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (current.st_dev, current.st_ino, current.st_size)
        ):
            raise WeiboError("旧版备份索引在读取时已变化", kind=WeiboErrorKind.API)
        target_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fsync(target_fd)
        os.close(target_fd)
        target_fd = None
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_regular_bytes(target: Path, payload: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _legacy_finalize_payload(
    task_id: str,
    expected_uid: str,
    archive_run_id: str,
    mode: str,
    index_sha256: str,
) -> dict[str, object]:
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise WeiboError("旧索引审计任务标识无效", kind=WeiboErrorKind.PARSE)
    if not isinstance(expected_uid, str) or not expected_uid.strip():
        raise WeiboError("旧索引审计账号标识无效", kind=WeiboErrorKind.PARSE)
    try:
        canonical_run_id = str(uuid.UUID(archive_run_id))
    except (TypeError, ValueError) as exc:
        raise WeiboError("旧索引审计同步记录标识无效", kind=WeiboErrorKind.PARSE) from exc
    if (
        canonical_run_id != archive_run_id
        or mode != "create"
        or not isinstance(index_sha256, str)
        or len(index_sha256) != 64
        or any(character not in "0123456789abcdef" for character in index_sha256)
    ):
        raise WeiboError("旧索引审计任务信息无效", kind=WeiboErrorKind.PARSE)
    return {
        "schema_version": 1,
        "task_id": task_id,
        "expected_uid": expected_uid,
        "archive_run_id": archive_run_id,
        "mode": mode,
        "index_sha256": index_sha256,
    }


def _write_finalize_marker(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_finalize_audit(
    root: Path,
    task_id: str,
    expected_uid: str,
    archive_run_id: str,
    mode: str,
    expected_index_sha256: str,
) -> bool:
    audit = root / LEGACY_AUDIT_DIRECTORY
    try:
        audit.lstat()
    except FileNotFoundError:
        return False
    _require_plain_directory(audit, "旧索引审计目录无法安全读取")
    expected_names = {INDEX_FILENAME, LEGACY_FINALIZE_MARKER}
    if {item.name for item in audit.iterdir()} != expected_names:
        raise WeiboError("旧索引审计目录内容无效", kind=WeiboErrorKind.PARSE)
    index_payload = _read_regular_bytes(audit / INDEX_FILENAME)
    index = _parse_legacy_index(index_payload)
    if index.uid != expected_uid:
        raise WeiboError("旧索引审计账号与持久任务不一致", kind=WeiboErrorKind.AUTH)
    marker = audit / LEGACY_FINALIZE_MARKER
    _require_plain_file(marker, "旧索引审计标记无法安全读取")
    try:
        payload = json.loads(_read_regular_bytes(marker).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WeiboError("旧索引审计标记已损坏", kind=WeiboErrorKind.PARSE) from exc
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
        raise WeiboError("旧索引审计标记无效", kind=WeiboErrorKind.PARSE)
    index_sha256 = hashlib.sha256(index_payload).hexdigest()
    if index_sha256 != expected_index_sha256:
        raise WeiboError("旧索引审计摘要与持久任务不一致", kind=WeiboErrorKind.PARSE)
    if payload != _legacy_finalize_payload(
        task_id,
        expected_uid,
        archive_run_id,
        mode,
        index_sha256,
    ):
        raise WeiboError("旧索引审计标记与持久任务不一致", kind=WeiboErrorKind.PARSE)
    return True


def legacy_finalize_completed(
    output_dir: str | Path,
    task_id: str,
    expected_uid: str,
    archive_run_id: str,
    mode: str,
    expected_index_sha256: str,
) -> bool:
    """只认当前任务的精确旧索引审计结果。"""
    root = Path(output_dir)
    _require_no_symlink_chain(root, "旧索引审计路径包含符号链接")
    _require_plain_directory(root, "新微博书目录无法安全读取旧索引审计")
    return _validate_finalize_audit(
        root,
        task_id,
        expected_uid,
        archive_run_id,
        mode,
        expected_index_sha256,
    )


def finalize_legacy_archive(
    output_dir: str | Path,
    staged: Path,
    task_id: str,
    expected_uid: str,
    archive_run_id: str,
    mode: str,
    expected_index_sha256: str,
) -> None:
    """首次完整建档成功后，仅留存旧索引供一轮审计。"""
    root = Path(output_dir)
    staged = Path(staged)
    _validate_legacy_stage(root, staged, task_id)
    isolation = _legacy_delete_path(root, task_id)
    _require_no_symlink_chain(isolation, "旧版备份删除隔离路径包含符号链接")
    _require_plain_directory(root, "新微博书目录无法安全写入旧索引")
    stage_exists = staged.exists() or staged.is_symlink()
    isolation_exists = isolation.exists() or isolation.is_symlink()
    if stage_exists and isolation_exists:
        raise WeiboError("当前任务的旧版备份删除隔离路径已存在", kind=WeiboErrorKind.API)
    if not stage_exists:
        if not _validate_finalize_audit(
            root, task_id, expected_uid, archive_run_id, mode, expected_index_sha256
        ):
            raise WeiboError("旧版备份暂存目录不存在且无精确审计结果", kind=WeiboErrorKind.PARSE)
        if not isolation_exists:
            return
        _require_plain_directory(isolation, "旧版备份删除隔离目录无法安全读取")
        isolated_payload, isolated_identity = _read_regular_snapshot(
            isolation / INDEX_FILENAME
        )
        isolated_index = _parse_legacy_index(isolated_payload)
        if isolated_index.uid != expected_uid:
            raise WeiboError("旧版备份删除隔离账号不一致", kind=WeiboErrorKind.AUTH)
        if hashlib.sha256(isolated_payload).hexdigest() != expected_index_sha256:
            raise WeiboError("旧版备份删除隔离摘要不一致", kind=WeiboErrorKind.PARSE)
        final_payload, final_identity = _read_regular_snapshot(isolation / INDEX_FILENAME)
        if final_identity != isolated_identity or final_payload != isolated_payload:
            raise WeiboError("旧版备份删除隔离身份已变化", kind=WeiboErrorKind.API)
        shutil.rmtree(isolation)
        _fsync_directory(root.parent)
        return
    _require_plain_directory(staged, "旧版备份暂存目录无法安全读取")
    staged_marker = staged.lstat()
    staged_identity = (staged_marker.st_dev, staged_marker.st_ino)
    source = staged / INDEX_FILENAME
    _require_plain_file(source, "旧版备份索引无法安全读取")
    source_bytes, source_identity = _read_regular_snapshot(source)
    index = _parse_legacy_index(source_bytes)
    if index.uid != expected_uid:
        raise WeiboError("旧版备份账号与当前任务不一致", kind=WeiboErrorKind.AUTH)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != expected_index_sha256:
        raise WeiboError("旧版备份索引摘要与持久任务不一致", kind=WeiboErrorKind.PARSE)
    payload = _legacy_finalize_payload(
        task_id,
        expected_uid,
        archive_run_id,
        mode,
        source_sha256,
    )

    work = root / ".work"
    if work.exists():
        _require_plain_directory(work, "微博书暂存目录类型错误")
    else:
        work.mkdir()
        _fsync_directory(root)
    audit = work / "legacy"
    if audit.exists():
        _require_plain_directory(audit, "旧索引审计目录类型错误")
    else:
        audit.mkdir()
        _fsync_directory(work)
    entries = {item.name for item in audit.iterdir()}
    if not entries.issubset({INDEX_FILENAME, LEGACY_FINALIZE_MARKER}):
        raise WeiboError("旧索引审计目录含无关内容", kind=WeiboErrorKind.API)
    audit_index = audit / INDEX_FILENAME
    if audit_index.exists() or audit_index.is_symlink():
        _require_plain_file(audit_index, "旧索引审计文件类型错误")
        if _read_regular_bytes(audit_index) != source_bytes:
            raise WeiboError("旧索引审计内容与当前任务不一致", kind=WeiboErrorKind.API)
    else:
        _write_regular_bytes(audit_index, source_bytes)
    marker = audit / LEGACY_FINALIZE_MARKER
    if marker.exists() or marker.is_symlink():
        if not _validate_finalize_audit(
            root, task_id, expected_uid, archive_run_id, mode, expected_index_sha256
        ):
            raise WeiboError("旧索引审计标记与当前任务不一致", kind=WeiboErrorKind.API)
    else:
        _write_finalize_marker(marker, payload)
    final_source_bytes, final_source_identity = _read_regular_snapshot(source)
    if final_source_identity != source_identity or final_source_bytes != source_bytes:
        raise WeiboError("旧版备份索引在收尾时已变化", kind=WeiboErrorKind.API)
    if not _validate_finalize_audit(
        root,
        task_id,
        expected_uid,
        archive_run_id,
        mode,
        expected_index_sha256,
    ):
        raise WeiboError("旧索引审计结果不存在", kind=WeiboErrorKind.PARSE)
    os.replace(staged, isolation)
    _fsync_directory(root.parent)
    isolation_marker = isolation.lstat()
    if (
        stat.S_ISLNK(isolation_marker.st_mode)
        or not stat.S_ISDIR(isolation_marker.st_mode)
        or (isolation_marker.st_dev, isolation_marker.st_ino) != staged_identity
    ):
        raise WeiboError("旧版备份删除隔离身份与安全读取结果不一致", kind=WeiboErrorKind.API)
    isolated_source = isolation / INDEX_FILENAME
    isolated_bytes, isolated_identity = _read_regular_snapshot(isolated_source)
    if isolated_identity != source_identity or isolated_bytes != source_bytes:
        raise WeiboError("旧版备份删除隔离身份与安全读取结果不一致", kind=WeiboErrorKind.API)
    shutil.rmtree(isolation)
    _fsync_directory(root.parent)


def cleanup_legacy_audit(output_dir: str | Path) -> None:
    """下一次成功更新后清理已留存一轮的旧索引。"""
    root = Path(output_dir)
    work = root / ".work"
    audit = root / LEGACY_AUDIT_DIRECTORY
    try:
        work_marker = work.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(work_marker.st_mode) or not stat.S_ISDIR(work_marker.st_mode):
        raise WeiboError("微博书暂存目录类型错误", kind=WeiboErrorKind.API)
    try:
        audit_marker = audit.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(audit_marker.st_mode) or not stat.S_ISDIR(audit_marker.st_mode):
        raise WeiboError("旧索引审计目录类型错误", kind=WeiboErrorKind.API)
    for name in (INDEX_FILENAME, LEGACY_FINALIZE_MARKER):
        target = audit / name
        try:
            marker = target.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode):
            raise WeiboError("旧索引审计文件类型错误", kind=WeiboErrorKind.API)
        target.unlink()
        _fsync_directory(audit)
    try:
        audit.rmdir()
        _fsync_directory(work)
    except OSError:
        return
    try:
        work.rmdir()
        _fsync_directory(root)
    except OSError:
        pass


def _is_absolute_user_path(path: str) -> bool:
    """接受当前平台绝对路径，也接受 Windows drive/UNC 绝对路径。"""
    return os.path.isabs(path) or PureWindowsPath(path).is_absolute()


def _validate_output_dir(path: str) -> Path:
    """v1.1.5 关键风险缓解：选 C:\\Windows 这种目录会爆。
    要求：① 绝对路径 ② 父目录存在 ③ 当前用户可写。
    """
    if not path or not _is_absolute_user_path(path):
        raise HTTPException(status_code=400, detail=f"output_dir 必须是绝对路径: {path!r}")
    p = Path(path)
    parent = p.parent
    if not parent.exists():
        raise HTTPException(status_code=400, detail=f"父目录不存在: {parent}")
    # 当前用户可写
    if not os.access(parent, os.W_OK):
        raise HTTPException(status_code=403, detail=f"父目录不可写: {parent}")
    return p


def read_index(output_dir: Path) -> Optional[BackupIndex]:
    """读 <output_dir>/.weishushu_index.json，不存在返 None。"""
    f = output_dir / INDEX_FILENAME
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return BackupIndex(**data)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("索引文件损坏 %s: %s", f, e)
        return None


def write_index(output_dir: Path, idx: BackupIndex) -> None:
    """原子写：先写 .tmp 再 os.replace 防断电写一半。"""
    f = output_dir / INDEX_FILENAME
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(idx.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, f)
