"""Explicit, prefix-limited B2 transport for encrypted Room recovery objects.

No client or network operation exists at import time. There are deliberately no
delete, governance-bypass, bucket-write, lifecycle, or credential-discovery APIs.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


TIER_DAYS = {"daily": 30, "weekly": 84, "monthly": 366, "manual": 90}
_METADATA_REQUIRED = frozenset({
    "backup_format_version", "schema_version", "backup_id", "tier", "ciphertext_sha256",
})
_METADATA_ALLOWED = _METADATA_REQUIRED | {"code_commit"}
_NAME = re.compile(r"([0-9]{8}T[0-9]{6}Z)-([0-9a-f-]{16,64})\.tar\.age\Z")
_CHUNK = 65536


def _error(category: str):
    # Core imports this module, so resolve the bounded application error lazily.
    from .becoming_backup import BackupError
    return BackupError(category)


def _check_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _error("timeout")


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _error("retention_mismatch")
    return value.astimezone(timezone.utc)


def _bounded_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not _METADATA_REQUIRED <= value.keys():
        raise _error("metadata_mismatch")
    if set(value) - _METADATA_ALLOWED:
        raise _error("metadata_mismatch")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise _error("metadata_mismatch")
    metadata = dict(value)
    if (
        not re.fullmatch(r"[1-9][0-9]{0,3}", metadata["backup_format_version"])
        or not re.fullmatch(r"[1-9][0-9]{0,3}", metadata["schema_version"])
        or not re.fullmatch(r"[0-9a-f-]{16,64}", metadata["backup_id"])
        or metadata["tier"] not in TIER_DAYS
        or not re.fullmatch(r"[0-9a-f]{64}", metadata["ciphertext_sha256"])
        or ("code_commit" in metadata and not re.fullmatch(r"[0-9a-f]{40,64}", metadata["code_commit"]))
    ):
        raise _error("metadata_mismatch")
    return metadata


class _UploadReader:
    """Seekable SDK body with deadline checks between bounded file reads."""

    def __init__(self, stream, deadline: float):
        self._stream = stream
        self._deadline = deadline

    def read(self, size: int = -1) -> bytes:
        _check_time(self._deadline)
        if size is None or size < 0:
            # Preserve file.read() semantics for callers requesting the entire
            # remainder, while still checking between bounded disk reads.
            chunks = []
            while True:
                block = self.read(_CHUNK)
                if not block:
                    return b"".join(chunks)
                chunks.append(block)
        block = self._stream.read(min(size, _CHUNK))
        _check_time(self._deadline)
        return block

    def readinto(self, buffer) -> int:
        block = self.read(len(buffer))
        buffer[:len(block)] = block
        return len(block)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        _check_time(self._deadline)
        position = self._stream.seek(offset, whence)
        _check_time(self._deadline)
        return position

    def tell(self) -> int:
        _check_time(self._deadline)
        return self._stream.tell()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


def _exception_category(error: Exception) -> str:
    from .becoming_backup import BackupError
    # botocore wraps errors raised while reading the HTTP request body in
    # HTTPClientError(error=...). Preserve only our own bounded category;
    # never stringify a wrapper or expose request/credential fields.
    pending = [error]
    seen = set()
    for _ in range(8):
        if not pending:
            break
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BackupError):
            return current.category
        if "timeout" in type(current).__name__.lower():
            return "timeout"
        for nested in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(nested, BaseException):
                pending.append(nested)
        kwargs = getattr(current, "kwargs", None)
        if isinstance(kwargs, dict) and isinstance(kwargs.get("error"), BaseException):
            pending.append(kwargs["error"])
    return "storage_unavailable"


class B2Transport:
    """One configured bucket/prefix, explicit credentials, no ambient AWS chain."""

    def __init__(self, config: Any, *, client: Any = None, clock=None):
        if not getattr(config, "can_run", False):
            raise _error("invalid_configuration")
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.prefix = str(config.prefix)
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9_-]{0,63}){0,3}", self.prefix)
            or not all(isinstance(getattr(config, name, None), str) and getattr(config, name)
                       for name in ("endpoint_url", "region", "bucket", "access_key_id", "secret_access_key"))
        ):
            raise _error("invalid_configuration")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
                timeout = min(30, max(1, int(config.timeout_seconds)))
                client = boto3.client(
                    "s3", endpoint_url=config.endpoint_url, region_name=config.region,
                    aws_access_key_id=config.access_key_id,
                    aws_secret_access_key=config.secret_access_key,
                    config=Config(
                        signature_version="s3v4", connect_timeout=min(10, timeout),
                        read_timeout=timeout, retries={"max_attempts": 0},
                        s3={"addressing_style": "path"},
                        request_checksum_calculation="when_required",
                        response_checksum_validation="when_required",
                    ),
                )
            except Exception:
                raise _error("storage_unavailable") from None
        self.client = client

    def _deadline(self) -> float:
        return time.monotonic() + float(self.config.timeout_seconds)

    def _key(self, key: str) -> tuple[str, datetime, str]:
        if not isinstance(key, str) or not key.startswith(self.prefix + "/"):
            raise _error("invalid_object_key")
        parts = key[len(self.prefix) + 1:].split("/")
        if len(parts) != 5 or parts[0] not in TIER_DAYS:
            raise _error("invalid_object_key")
        match = _NAME.fullmatch(parts[4])
        if match is None:
            raise _error("invalid_object_key")
        try:
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            raise _error("invalid_object_key") from None
        if parts[1:4] != [created.strftime("%Y"), created.strftime("%m"), created.strftime("%d")]:
            raise _error("invalid_object_key")
        return parts[0], created, match[2]

    def _call(self, method: str, deadline: float, **kwargs):
        _check_time(deadline)
        try:
            result = getattr(self.client, method)(**kwargs)
        except Exception as exc:
            # Neither provider error bodies nor their possibly sensitive request
            # fields cross this boundary.
            raise _error(_exception_category(exc)) from None
        _check_time(deadline)
        if not isinstance(result, dict):
            raise _error("storage_unavailable")
        return result

    def _retention(self, key: str, deadline: float, *, version: str | None = None,
                   expected: datetime | None = None) -> None:
        kwargs = {"Bucket": self.config.bucket, "Key": key}
        if version:
            kwargs["VersionId"] = version
        response = self._call("get_object_retention", deadline, **kwargs)
        retention = response.get("Retention", {})
        if not isinstance(retention, Mapping) or retention.get("Mode") != "COMPLIANCE":
            raise _error("retention_mismatch")
        until = _utc(retention.get("RetainUntilDate"))
        if expected is not None and until < expected:
            raise _error("retention_mismatch")

    def _readiness(self, deadline: float) -> dict[str, bool]:
        self._call("head_bucket", deadline, Bucket=self.config.bucket)
        lock = self._call("get_object_lock_configuration", deadline, Bucket=self.config.bucket)
        configuration = lock.get("ObjectLockConfiguration", {})
        if not isinstance(configuration, Mapping) or configuration.get("ObjectLockEnabled") != "Enabled":
            raise _error("object_lock_required")
        rule = configuration.get("Rule", {})
        if not isinstance(rule, Mapping):
            raise _error("object_lock_required")
        default = rule.get("DefaultRetention", {})
        if not isinstance(default, Mapping):
            raise _error("object_lock_required")
        if default and default.get("Mode") != "COMPLIANCE":
            raise _error("object_lock_required")
        listed = self._call(
            "list_objects_v2", deadline, Bucket=self.config.bucket,
            Prefix=self.prefix + "/", MaxKeys=1,
        )
        contents = listed.get("Contents", [])
        if not isinstance(contents, list) or len(contents) > 1:
            raise _error("storage_unavailable")
        for item in contents:
            if not isinstance(item, Mapping):
                raise _error("storage_unavailable")
            key = item.get("Key")
            self._key(key)
            self._retention(key, deadline)
        # An empty prefix cannot prove object-read/retention-read permissions.
        # Those are mandatory independent checks on the actual uploaded object;
        # readiness alone is never a successful-backup receipt.
        return {"bucket_reachable": True, "prefix_listable": True, "object_lock_enabled": True}

    def readiness(self, *, deadline: float | None = None) -> dict[str, bool]:
        deadline = min(self._deadline(), deadline) if deadline is not None else self._deadline()
        return self._readiness(deadline)

    def _download(self, key: str, destination: Path, metadata: dict[str, str],
                  size: int, deadline: float, *, version: str | None = None) -> None:
        if destination.exists() or destination.is_symlink():
            raise _error("destination_exists")
        kwargs = {"Bucket": self.config.bucket, "Key": key}
        if version:
            kwargs["VersionId"] = version
        response = self._call("get_object", deadline, **kwargs)
        body = response.get("Body")
        pending: Path | None = None
        try:
            if type(response.get("ContentLength")) is not int or response["ContentLength"] != size:
                raise _error("download_size_mismatch")
            if response.get("Metadata") != metadata:
                raise _error("metadata_mismatch")
            if body is None:
                raise _error("download_size_mismatch")
            fd, name = tempfile.mkstemp(prefix=".ciphertext-", suffix=".pending", dir=destination.parent)
            pending = Path(name)
            os.fchmod(fd, 0o600)
            count = 0
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as stream:
                while True:
                    _check_time(deadline)
                    chunk = body.read(_CHUNK)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise _error("download_size_mismatch")
                    count += len(chunk)
                    if count > size:
                        raise _error("download_size_mismatch")
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if count != size:
                raise _error("download_size_mismatch")
            if digest.hexdigest() != metadata["ciphertext_sha256"]:
                raise _error("download_hash_mismatch")
            _check_time(deadline)
            # Hard-link publication never replaces an existing destination,
            # including one appearing after the initial existence check.
            os.link(pending, destination)
        except Exception as exc:
            from .becoming_backup import BackupError
            if isinstance(exc, BackupError):
                raise
            if isinstance(exc, FileExistsError):
                raise _error("destination_exists") from None
            category = "timeout" if "timeout" in type(exc).__name__.lower() else "download_failure"
            raise _error(category) from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass
            if pending is not None:
                pending.unlink(missing_ok=True)

    def upload_and_verify(self, ciphertext_path: str | Path, *, key: str,
                          metadata: Mapping[str, str], retain_until: datetime,
                          deadline: float | None = None) -> dict[str, Any]:
        tier, created, backup_id = self._key(key)
        metadata = _bounded_metadata(metadata)
        until = _utc(retain_until)
        if metadata["tier"] != tier or metadata["backup_id"] != backup_id:
            raise _error("metadata_mismatch")
        if until < max(created, _utc(self.clock())) + timedelta(days=TIER_DAYS[tier]):
            raise _error("retention_mismatch")
        path = Path(ciphertext_path)
        if not path.is_file() or path.is_symlink():
            raise _error("ciphertext_invalid")
        size = path.stat().st_size
        maximum = int(self.config.max_plaintext_bytes) + 1048576
        if not 0 < size <= maximum:
            raise _error("ciphertext_invalid")
        deadline = min(self._deadline(), deadline) if deadline is not None else self._deadline()
        sha = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK):
                _check_time(deadline)
                sha.update(chunk)
                md5.update(chunk)
        if sha.hexdigest() != metadata["ciphertext_sha256"]:
            raise _error("metadata_mismatch")
        self._readiness(deadline)
        with path.open("rb") as source:
            uploaded = self._call(
                "put_object", deadline, Bucket=self.config.bucket, Key=key, Body=_UploadReader(source, deadline),
                ContentLength=size, ContentType="application/octet-stream", Metadata=metadata,
                ContentMD5=base64.b64encode(md5.digest()).decode("ascii"),
                ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=until,
            )
        version = uploaded.get("VersionId")
        if version is not None and (not isinstance(version, str) or not 0 < len(version) <= 1024):
            raise _error("metadata_mismatch")
        kwargs = {"Bucket": self.config.bucket, "Key": key}
        if version:
            kwargs["VersionId"] = version
        head = self._call("head_object", deadline, **kwargs)
        if type(head.get("ContentLength")) is not int or head["ContentLength"] != size:
            raise _error("download_size_mismatch")
        if head.get("Metadata") != metadata or head.get("ContentType") != "application/octet-stream":
            raise _error("metadata_mismatch")
        self._retention(key, deadline, version=version, expected=until)
        with tempfile.TemporaryDirectory(prefix="room-ciphertext-verify-", dir=path.parent) as directory:
            os.chmod(directory, 0o700)
            self._download(key, Path(directory) / "verified.tar.age", metadata, size, deadline, version=version)
        return {
            "ciphertext_size": size, "ciphertext_sha256": sha.hexdigest(),
            "retention_verified": True, "download_verified": True,
        }

    def download_for_restore(self, key: str, destination_path: str | Path, *,
                             deadline: float | None = None) -> dict[str, Any]:
        tier, _, backup_id = self._key(key)
        destination = Path(destination_path)
        if destination.exists() or destination.is_symlink():
            raise _error("destination_exists")
        if not destination.parent.is_dir() or destination.parent.is_symlink():
            raise _error("destination_unavailable")
        deadline = min(self._deadline(), deadline) if deadline is not None else self._deadline()
        head = self._call("head_object", deadline, Bucket=self.config.bucket, Key=key)
        metadata = _bounded_metadata(head.get("Metadata"))
        if metadata["tier"] != tier or metadata["backup_id"] != backup_id:
            raise _error("metadata_mismatch")
        size = head.get("ContentLength")
        if type(size) is not int or not 0 < size <= int(self.config.max_plaintext_bytes) + 1048576:
            raise _error("download_size_mismatch")
        if head.get("ContentType") != "application/octet-stream":
            raise _error("metadata_mismatch")
        version = head.get("VersionId")
        if version is not None and (not isinstance(version, str) or not 0 < len(version) <= 1024):
            raise _error("metadata_mismatch")
        self._retention(key, deadline, version=version)
        self._download(key, destination, metadata, size, deadline, version=version)
        return {**metadata, "ciphertext_size": size, "retention_verified": True, "download_verified": True}
