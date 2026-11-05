"""Safe host-folder to FAT staging and clean-stop commit support.

The host folder is a user data root, not a path passed to QEMU.  This module
keeps the source tree and the guest FAT image as separate persistence
domains.  A session owns an exclusive root lock, stages a no-symlink snapshot
into a private directory, and commits guest changes only after QEMU has
exited.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import stat
import struct
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


IMAGE_BYTES = 64 * 1024 * 1024
INTERNAL_LOCK = ".protopandaemu.lock"
JOURNAL_NAME = ".protopandaemu-journal.json"
_INTERNAL_PREFIXES = (".protopandaemu-",)


class HostDirError(RuntimeError):
    """A named, fail-closed host-folder error."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class InjectedCrash(HostDirError):
    """Deterministic test fault at a durable commit boundary."""

    def __init__(self, point: str):
        super().__init__(f"crash injected at durable boundary {point}", code="crash_injected")
        self.point = point


def _raise(code: str, message: str) -> None:
    raise HostDirError(message, code=code)


def _is_internal(name: str) -> bool:
    return (name == INTERNAL_LOCK or name == JOURNAL_NAME or
            any(name.startswith(prefix) for prefix in _INTERNAL_PREFIXES) or
            ".protopandaemu-backup-" in name or
            ".protopandaemu-conflict-" in name)


def _safe_rel(value: str | os.PathLike[str]) -> pathlib.PurePosixPath:
    text = pathlib.PurePosixPath(str(value).replace("\\", "/"))
    if text.is_absolute() or not text.parts or any(part in {"", ".", ".."} for part in text.parts):
        _raise("symlink_escape", f"unsafe guest path: {value!s}")
    return text


def _hash_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        _raise("hostdir_permission_denied", f"cannot open directory for fsync {path}: {exc}")
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_fd_to_path(fd: int, destination: pathlib.Path, *, exclusive: bool = False) -> str:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    try:
        out_fd = os.open(destination, flags, 0o644)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            _raise("disk_full", f"disk full writing {destination}")
        if exc.errno in {errno.EACCES, errno.EROFS}:
            _raise("hostdir_permission_denied", f"permission denied writing {destination}")
        raise
    digest = hashlib.sha256()
    try:
        with os.fdopen(os.dup(fd), "rb", closefd=True) as source, os.fdopen(out_fd, "wb", closefd=True) as output:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                try:
                    output.write(block)
                except OSError as exc:
                    if exc.errno in {errno.ENOSPC, errno.EFBIG}:
                        _raise("disk_full", f"disk full writing {destination}")
                    raise
            output.flush()
            os.fsync(output.fileno())
    except HostDirError:
        with contextlib.suppress(OSError):
            destination.unlink()
        raise
    _fsync_dir(destination.parent)
    return digest.hexdigest()


def _copy_path(source: pathlib.Path, destination: pathlib.Path, *, exclusive: bool = False) -> str:
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError as exc:
        _raise("hostdir_missing", f"source file disappeared: {source}")
    except PermissionError as exc:
        _raise("hostdir_permission_denied", f"permission denied reading {source}")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _raise("symlink_escape", f"symlink encountered: {source}")
        raise
    try:
        return _copy_fd_to_path(fd, destination, exclusive=exclusive)
    finally:
        os.close(fd)


def _walk_fd(fd: int, relative: tuple[str, ...] = ()) -> Iterator[tuple[pathlib.PurePosixPath, str, int]]:
    """Yield regular files using directory handles and no-follow opens."""
    try:
        names = sorted(os.listdir(fd))
    except PermissionError as exc:
        _raise("hostdir_permission_denied", f"cannot list source directory: {exc}")
    for name in names:
        if _is_internal(name):
            continue
        rel = pathlib.PurePosixPath(*(relative + (name,)))
        try:
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            _raise("source_tree_changed", f"source entry disappeared during snapshot: {rel}")
        if stat.S_ISLNK(entry.st_mode):
            _raise("symlink_escape", f"symlink is not allowed in hostdir: {rel}")
        if stat.S_ISDIR(entry.st_mode):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    _raise("symlink_escape", f"directory changed to symlink: {rel}")
                if exc.errno in {errno.EACCES, errno.EROFS}:
                    _raise("hostdir_permission_denied", f"cannot open source directory {rel}: {exc}")
                raise
            try:
                yield from _walk_fd(child, relative + (name,))
            finally:
                os.close(child)
        elif stat.S_ISREG(entry.st_mode):
            try:
                child = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    _raise("symlink_escape", f"file changed to symlink: {rel}")
                if exc.errno in {errno.EACCES, errno.EROFS}:
                    _raise("hostdir_permission_denied", f"cannot read source file {rel}: {exc}")
                raise
            yield rel, "file", child
        else:
            _raise("symlink_escape", f"unsupported special file in hostdir: {rel}")


def _manifest_hash(manifest: dict[str, str]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_fd(root_fd: int, *, stage: pathlib.Path | None = None) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for rel, _kind, fd in _walk_fd(root_fd):
        try:
            before = os.fstat(fd)
            if stage is not None:
                target = stage.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = _copy_fd_to_path(fd, target)
            else:
                with os.fdopen(os.dup(fd), "rb", closefd=True) as stream:
                    digest = _hash_stream(stream)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                _raise("source_tree_changed", f"source file changed during snapshot: {rel}")
            manifest[rel.as_posix()] = digest
        finally:
            os.close(fd)
    return manifest


def _snapshot(root: pathlib.Path, *, stage: pathlib.Path | None = None) -> dict[str, str]:
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        _raise("hostdir_missing", f"hostdir does not exist: {root}")
    except NotADirectoryError as exc:
        _raise("hostdir_not_directory", f"hostdir is not a directory: {root}")
    except PermissionError as exc:
        _raise("hostdir_permission_denied", f"cannot open hostdir: {root}")
    try:
        return _snapshot_fd(root_fd, stage=stage)
    finally:
        os.close(root_fd)


def _open_root(root: pathlib.Path) -> pathlib.Path:
    raw = pathlib.Path(root).expanduser()
    if not raw.is_absolute():
        raw = pathlib.Path.cwd() / raw
    try:
        canonical = raw.resolve(strict=True)
    except FileNotFoundError:
        _raise("hostdir_missing", f"hostdir does not exist: {raw}")
    if not canonical.is_dir():
        _raise("hostdir_not_directory", f"hostdir is not a directory: {canonical}")
    mode = canonical.stat().st_mode
    if not (mode & 0o444) or not (mode & 0o222):
        _raise("hostdir_permission_denied", f"hostdir is not readable and writable: {canonical}")
    try:
        fd = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except PermissionError as exc:
        _raise("hostdir_permission_denied", f"cannot open hostdir: {canonical}")
    else:
        os.close(fd)
    return canonical


def _validate_png(path: pathlib.Path, data: bytes) -> None:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        _raise("png_invalid", f"invalid PNG signature: {path}")
    offset = 8
    seen_ihdr = seen_idat = seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            _raise("png_invalid", f"truncated PNG chunk: {path}")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        expected = struct.unpack(">I", data[offset + 8 + length:end])[0]
        actual = __import__("zlib").crc32(kind + payload) & 0xffffffff
        if expected != actual:
            _raise("png_invalid", f"PNG CRC mismatch: {path}")
        if kind == b"IHDR":
            if length != 13 or struct.unpack(">II", payload[:8]) == (0, 0):
                _raise("png_invalid", f"invalid PNG IHDR: {path}")
            seen_ihdr = True
        elif kind == b"IDAT":
            seen_idat = True
        elif kind == b"IEND":
            seen_iend = True
            break
        offset = end
    if not (seen_ihdr and seen_idat and seen_iend):
        _raise("png_invalid", f"PNG is missing IHDR, IDAT, or IEND: {path}")


_FIRMWARE_CONFIG_JSON = frozenset({
    "animation.json", "keybinds.json", "misc.json", "wifi.json", "hardware.json", "models.json",
})  # root-level files the firmware's configloader.lua/models.lua require as valid JSON


def _validate_inputs(root: pathlib.Path, manifest: dict[str, str]) -> None:
    json_values: dict[str, Any] = {}
    for rel in sorted(manifest):
        path = root.joinpath(*rel.split("/"))
        # Only the firmware's own known root config files must parse as JSON;
        # an arbitrary user file that happens to end in .json (a note, a save,
        # a test fixture) is the user's own data, not a firmware input.
        if "/" not in rel and rel.lower() in _FIRMWARE_CONFIG_JSON:
            try:
                json_values[rel] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                _raise("malformed_json", f"malformed JSON in {rel}: {exc}")
        if rel.lower().endswith(".png"):
            try:
                _validate_png(path, path.read_bytes())
            except HostDirError:
                raise
            except OSError as exc:
                _raise("png_invalid", f"cannot read PNG {rel}: {exc}")
    animation = json_values.get("animation.json")
    if isinstance(animation, dict):
        references: list[str] = []
        def collect(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    collect(child, str(child_key))
            elif isinstance(value, list):
                for child in value:
                    collect(child, key)
            elif isinstance(value, str) and key in {"file", "files"} and value.lower().endswith(".png"):
                references.append(value)
        collect(animation)
        for reference in references:
            try:
                # Firmware paths are rooted at the virtual SD volume.  A
                # leading slash is therefore normal; traversal is not.
                guest_reference = reference.replace("\\", "/")
                if guest_reference.startswith("/"):
                    guest_reference = guest_reference[1:]
                rel = _safe_rel(guest_reference).as_posix()
            except HostDirError:
                raise
            if rel not in manifest:
                _raise("png_missing", f"animation.json references missing PNG: {reference}")


def _run_tool(argv: list[str], *, code: str, timeout: float = 120.0) -> None:
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, check=False, timeout=timeout)
    except OSError as exc:
        _raise(code, f"could not run {' '.join(argv)}: {exc}")
    except subprocess.TimeoutExpired as exc:
        _raise(code, f"timeout running {' '.join(argv)}: {exc}")
    if result.returncode != 0:
        output = (result.stdout or "")[-1200:]
        if "No space" in output or "disk full" in output.lower():
            _raise("disk_full", output or "disk full while building guest image")
        _raise(code, output or f"command returned {result.returncode}: {' '.join(argv)}")


def _fault(point: str, fault_at: str | None) -> None:
    wanted = fault_at or os.environ.get("PROTOPANDAEMU_HOSTDIR_CRASH_AT")
    if wanted and (wanted == point or wanted == "*" or (wanted == "after_rename" and point.startswith("after_rename"))):
        raise InjectedCrash(point)


def _safe_target(root: pathlib.Path, rel: str, *, create_parents: bool = True) -> pathlib.Path:
    relative = _safe_rel(rel)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            if not create_parents:
                _raise("recovery_invalid", f"journal target parent is missing: {relative}")
            current.mkdir()
            _fsync_dir(current.parent)
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            _raise("symlink_escape", f"unsafe target parent: {relative}")
    target = root.joinpath(*relative.parts)
    with contextlib.suppress(FileNotFoundError):
        if target.is_symlink():
            _raise("symlink_escape", f"unsafe target symlink: {relative}")
    return target


def _existing_hash(path: pathlib.Path) -> str | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        _raise("symlink_escape", f"target is not a regular file: {path}")
    try:
        with path.open("rb") as stream:
            return _hash_stream(stream)
    except PermissionError:
        _raise("hostdir_permission_denied", f"cannot read target: {path}")


def _write_json_fsync(path: pathlib.Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


@dataclass
class HostDirSession:
    root: pathlib.Path
    session_id: str
    session_dir: pathlib.Path
    lock_fd: int
    root_fd: int
    start_manifest: dict[str, str]
    start_tree_sha256: str
    stage_dir: pathlib.Path
    conflicts: list[dict[str, str]] = field(default_factory=list)
    backups: list[dict[str, str]] = field(default_factory=list)
    current_manifest: dict[str, str] | None = None
    guest_manifest: dict[str, str] | None = None
    _released: bool = False

    @classmethod
    def open(cls, hostdir: str | os.PathLike[str], session_dir: pathlib.Path, session_id: str) -> "HostDirSession":
        root = _open_root(pathlib.Path(hostdir))
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        lock_path = root / INTERNAL_LOCK
        try:
            lock_fd = os.open(INTERNAL_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
                              dir_fd=root_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileExistsError:
            _raise("symlink_escape", f"hostdir lock is a symlink: {lock_path}")
        except BlockingIOError:
            _raise("hostdir_locked", f"hostdir is already in use: {root}")
        except PermissionError:
            _raise("hostdir_permission_denied", f"cannot acquire hostdir lock: {root}")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _raise("symlink_escape", f"hostdir lock is a symlink: {lock_path}")
            raise
        try:
            recover(root)
            manifest = _snapshot_fd(root_fd)
            stage = session_dir / "hostdir-stage"
            stage.mkdir(parents=True, exist_ok=False)
            return cls(root=root, session_id=session_id, session_dir=session_dir, lock_fd=lock_fd,
                       root_fd=root_fd,
                       start_manifest=manifest, start_tree_sha256=_manifest_hash(manifest), stage_dir=stage)
        except BaseException:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            with contextlib.suppress(OSError):
                os.close(root_fd)
            raise

    def build_image(self, image: pathlib.Path) -> pathlib.Path:
        # Rebuild the stage from the no-follow source handles; never copy by a
        # path that was merely checked before this operation.
        stage_manifest = _snapshot_fd(self.root_fd, stage=self.stage_dir)
        if stage_manifest != self.start_manifest:
            _raise("source_tree_changed", "hostdir changed while the guest image was being staged")
        _validate_inputs(self.stage_dir, stage_manifest)
        image.parent.mkdir(parents=True, exist_ok=True)
        try:
            with image.open("xb") as stream:
                stream.truncate(IMAGE_BYTES)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            _raise("sd_state_invalid", f"session image already exists: {image}")
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EFBIG}:
                _raise("disk_full", f"disk full creating guest image: {image}")
            raise
        _run_tool(["mformat", "-i", str(image), "-F", "::"], code="sd_image_failed")
        for child in sorted(self.stage_dir.iterdir(), key=lambda p: p.name):
            _run_tool(["mcopy", "-D", "o", "-i", str(image), "-s", str(child), "::"], code="sd_image_failed")
        final_manifest = _snapshot_fd(self.root_fd)
        if final_manifest != self.start_manifest:
            _raise("source_tree_changed", "hostdir changed before guest image creation completed")
        _fault("after_image_build", None)
        return image

    def _extract(self, image: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
        extraction = self.session_dir / "guest-extracted"
        extraction.mkdir(parents=True, exist_ok=False)
        _run_tool(["mcopy", "-s", "-i", str(image), "::*", str(extraction) + os.sep], code="guest_extract_failed")
        manifest = _snapshot(extraction)
        return extraction, manifest

    def _conflict(self, extraction: pathlib.Path, rel: str, *, deleted: bool) -> None:
        target = _safe_target(self.root, rel)
        suffix = f".protopandaemu-conflict-{self.session_id}-{uuid.uuid4().hex}"
        conflict = target.with_name(target.name + suffix + (".deleted" if deleted else ""))
        if deleted:
            payload = json.dumps({"path": rel, "guest_action": "delete"}, sort_keys=True).encode() + b"\n"
            conflict.write_bytes(payload)
            with conflict.open("rb") as stream:
                digest = _hash_stream(stream)
            with conflict.open("ab") as stream:
                stream.flush(); os.fsync(stream.fileno())
        else:
            source = extraction.joinpath(*rel.split("/"))
            digest = _copy_path(source, conflict, exclusive=True)
        _fsync_dir(conflict.parent)
        self.conflicts.append({"path": rel, "retained_path": str(conflict), "sha256": digest,
                               "kind": "deleted" if deleted else "guest_version"})

    def sync(self, image: pathlib.Path, *, fault_at: str | None = None) -> dict[str, Any]:
        extraction, guest_manifest = self._extract(image)
        current = _snapshot_fd(self.root_fd)
        self.guest_manifest = guest_manifest
        self.current_manifest = current
        operations: list[dict[str, Any]] = []
        paths = sorted(set(self.start_manifest) | set(current) | set(guest_manifest))
        for rel in paths:
            before = self.start_manifest.get(rel)
            host_now = current.get(rel)
            guest_now = guest_manifest.get(rel)
            host_changed = host_now != before
            guest_changed = guest_now != before
            if not guest_changed:
                continue
            if host_changed:
                self._conflict(extraction, rel, deleted=guest_now is None)
                continue
            operations.append({"target": rel, "action": "write" if guest_now is not None else "delete",
                               "old_hash": before, "new_hash": guest_now,
                               "source": str(extraction.joinpath(*rel.split("/"))) if guest_now is not None else None})
        if operations:
            self.backups.extend(_commit(self.root, self.session_id, operations, fault_at=fault_at))
        self.current_manifest = _snapshot_fd(self.root_fd)
        return {"start_manifest": self.start_manifest, "current_manifest": self.current_manifest,
                "guest_manifest": guest_manifest, "conflicts": self.conflicts, "backups": self.backups,
                "tree_hash_start": self.start_tree_sha256, "tree_hash_current": _manifest_hash(self.current_manifest),
                "sync_status": "committed"}

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with contextlib.suppress(OSError):
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.lock_fd)
        with contextlib.suppress(OSError):
            os.close(self.root_fd)


def _commit(root: pathlib.Path, session_id: str, operations: list[dict[str, Any]], *, fault_at: str | None) -> list[dict[str, str]]:
    records: list[dict[str, Any]] = []
    backups: list[dict[str, str]] = []
    # Prepare all target paths and durable payloads before publishing the journal.
    for index, operation in enumerate(operations):
        target = _safe_target(root, operation["target"])
        old_hash = _existing_hash(target)
        if old_hash != operation["old_hash"]:
            _raise("hostdir_concurrent_edit", f"target changed during commit: {operation['target']}")
        backup = None
        if old_hash is not None:
            backup = target.with_name(target.name + f".protopandaemu-backup-{session_id}-{uuid.uuid4().hex}")
            digest = _copy_path(target, backup, exclusive=True)
            if digest != old_hash:
                _raise("commit_failed", f"backup hash mismatch: {backup}")
            backups.append({"path": str(backup), "sha256": digest, "target": operation["target"]})
            _fault("after_backup_fsync", fault_at)
        temporary = None
        if operation["action"] == "write":
            temporary = target.with_name(f".{target.name}.protopandaemu-tmp-{session_id}-{uuid.uuid4().hex}")
            digest = _copy_path(pathlib.Path(operation["source"]), temporary, exclusive=True)
            if digest != operation["new_hash"]:
                _raise("commit_failed", f"temporary hash mismatch: {temporary}")
            _fault("after_temp_fsync", fault_at)
        records.append({"target": operation["target"], "action": operation["action"],
                        "old_hash": operation["old_hash"], "new_hash": operation["new_hash"],
                        "temp": str(temporary.relative_to(root)) if temporary else None,
                        "backup": str(backup.relative_to(root)) if backup else None})
    journal = {"schema": "protopandaemu/hostdir-journal/v1", "session_id": session_id,
               "records": records}
    journal_tmp = root / f".protopandaemu-journal-{session_id}-{uuid.uuid4().hex}.tmp"
    _write_json_fsync(journal_tmp, journal)
    _fault("after_journal_fsync", fault_at)
    os.replace(journal_tmp, root / JOURNAL_NAME)
    _fault("after_journal_publish", fault_at)
    _fsync_dir(root)
    _fault("after_journal_dir_fsync", fault_at)
    _apply_records(root, journal, fault_at=fault_at)
    journal_path = root / JOURNAL_NAME
    journal_path.unlink()
    _fsync_dir(root)
    _fault("after_journal_remove", fault_at)
    return backups


def _validate_journal(root: pathlib.Path, journal: dict[str, Any]) -> None:
    if journal.get("schema") != "protopandaemu/hostdir-journal/v1" or not isinstance(journal.get("records"), list):
        _raise("recovery_invalid", "hostdir journal schema is invalid")
    for record in journal["records"]:
        if not isinstance(record, dict) or record.get("action") not in {"write", "delete"}:
            _raise("recovery_invalid", "hostdir journal record is invalid")
        for key in ("target", "temp", "backup"):
            value = record.get(key)
            if value is not None:
                _safe_rel(value)
        if record.get("new_hash") is not None and not isinstance(record["new_hash"], str):
            _raise("recovery_invalid", "hostdir journal new hash is invalid")


def _apply_records(root: pathlib.Path, journal: dict[str, Any], *, fault_at: str | None) -> None:
    # Validate every record before changing any target; a malformed journal is
    # fail-closed rather than a best-effort repair.
    _validate_journal(root, journal)
    for index, record in enumerate(journal["records"]):
        target = _safe_target(root, record["target"], create_parents=False)
        current = _existing_hash(target)
        old_hash = record["old_hash"]
        new_hash = record["new_hash"]
        temp = root / record["temp"] if record.get("temp") else None
        backup = root / record["backup"] if record.get("backup") else None
        if old_hash is not None:
            if backup is None or _existing_hash(backup) != old_hash:
                _raise("recovery_invalid", f"journal backup is missing or mismatched: {record['target']}")
        if record["action"] == "write":
            if current == new_hash:
                if temp is not None and temp.exists():
                    temp.unlink()
            elif current == old_hash:
                if temp is None or _existing_hash(temp) != new_hash:
                    _raise("recovery_invalid", f"journal temporary content is missing: {record['target']}")
                os.replace(temp, target)
                _fsync_dir(target.parent)
                _fault(f"after_rename_{index}", fault_at)
                _fault("after_rename", fault_at)
            else:
                _raise("recovery_invalid", f"target hash is neither old nor new: {record['target']}")
            if _existing_hash(target) != new_hash:
                _raise("recovery_invalid", f"target hash did not reach new value: {record['target']}")
        else:
            if current == new_hash or current is None:
                continue
            if current != old_hash:
                _raise("recovery_invalid", f"delete target hash is unexpected: {record['target']}")
            target.unlink()
            _fsync_dir(target.parent)
            _fault(f"after_rename_{index}", fault_at)
            _fault("after_rename", fault_at)


def recover(root: pathlib.Path, *, fault_at: str | None = None) -> bool:
    journal_path = root / JOURNAL_NAME
    if not journal_path.exists():
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _raise("recovery_invalid", f"cannot read hostdir journal: {exc}")
    _validate_journal(root, journal)
    _apply_records(root, journal, fault_at=fault_at)
    journal_path.unlink()
    _fsync_dir(root)
    return True


def recover_hostdir(hostdir: str | os.PathLike[str], *, fault_at: str | None = None) -> bool:
    root = _open_root(pathlib.Path(hostdir))
    lock_path = root / INTERNAL_LOCK
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except PermissionError:
        _raise("hostdir_permission_denied", f"cannot acquire hostdir lock: {root}")
    try:
        return recover(root, fault_at=fault_at)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def manifest(hostdir: str | os.PathLike[str]) -> dict[str, str]:
    """Return a safe file manifest without changing the host tree."""
    root = _open_root(pathlib.Path(hostdir))
    return _snapshot(root)

