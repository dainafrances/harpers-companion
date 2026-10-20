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


def _error(category: str, **fields):
    # Core imports this module, so resolve the bounded application error lazily.
    from .becoming_backup import BackupError
    return BackupError(category, **fields)


def _check_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _error("timeout")


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error("retention_mismatch")
    return value.astimezone(timezone.utc)


def _bounded_error(error: BaseException, fallback: str):
    from .becoming_backup import BackupError
    if isinstance(error, BackupError):
        return _error(error.category, cleanup_failed=error.cleanup_failed,
                      verification_reason=error.verification_reason,
                      remote_uploaded=error.remote_uploaded)
    if type(error).__name__ == "CancelledError":
        return _error("cancelled")
    if "timeout" in type(error).__name__.lower():
        return _error("timeout")
    if type(error).__name__ == "IncompleteReadError":
        return _error("download_size_mismatch")
    return _error(fallback)


def _cleanup_failed(primary=None):
    if primary is None:
        return _error("cleanup_failure", cleanup_failed=True)
    return _error(
        primary.category, cleanup_failed=True,
        verification_reason=getattr(primary, "verification_reason", None),
        remote_uploaded=getattr(primary, "remote_uploaded", False),
    )


def _remote_error(primary, stage: str):
    if primary.category == "remote_unverified":
        return primary
    reasons = {
        "metadata_mismatch": "metadata_mismatch",
        "retention_mismatch": "retention_mismatch",
        "retention_too_short": "retention_too_short",
        "download_size_mismatch": "length_mismatch",
        "download_hash_mismatch": "hash_mismatch",
        "cleanup_failure": "verification_cleanup_failure",
        "cancelled": "cancelled",
    }
    reason = reasons.get(primary.category)
    if reason is None:
        if primary.category == "timeout":
            reason = "download_timeout" if stage == "download" else "verification_timeout"
        else:
            reason = "download_failure" if stage == "download" else "storage_unavailable"
    return _error(
        "remote_unverified", verification_reason=reason, remote_uploaded=True,
        cleanup_failed=getattr(primary, "cleanup_failed", False),
    )


def _clean_verification_directory(directory: Path | None) -> bool:
    """Attempt every owned cleanup independently; never expose OS diagnostics."""
    if directory is None:
        return False
    failed = False
    try:
        children = list(directory.iterdir())
    except BaseException:
        children = []
        failed = True
    for child in children:
        try:
            child.unlink(missing_ok=True)
        except BaseException:
            failed = True
    try:
        directory.rmdir()
    except BaseException:
        failed = True
    return failed


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
        primary = None
        try:
            result = getattr(self.client, method)(**kwargs)
        except BaseException as exc:
            # Neither provider messages nor possibly sensitive request fields
            # cross this boundary. A failed PUT is not a confirmed upload.
            if isinstance(exc, Exception):
                primary = _error(_exception_category(exc))
            else:
                primary = _bounded_error(exc, "storage_unavailable")
        if primary is not None:
            raise primary from None
        primary = None
        try:
            _check_time(deadline)
            if not isinstance(result, dict):
                raise _error("storage_unavailable")
        except BaseException as exc:
            primary = _bounded_error(exc, "storage_unavailable")
        if primary is not None:
            # GET ownership begins as soon as the SDK returns, before the
            # wrapper's post-call deadline can raise.
            body = result.get("Body") if isinstance(result, dict) else None
            if body is not None:
                try:
                    body.close()
                except BaseException:
                    primary = _cleanup_failed(primary)
            if method == "put_object":
                primary = _remote_error(primary, "verification")
            raise primary from None
        return result

    def _retention(self, key: str, deadline: float, *, version: str | None = None,
                   expected: datetime | None = None, minimum_days: int | None = None) -> datetime:
        kwargs = {"Bucket": self.config.bucket, "Key": key}
        if version:
            kwargs["VersionId"] = version
        response = self._call("get_object_retention", deadline, **kwargs)
        retention = response.get("Retention", {})
        if not isinstance(retention, Mapping) or retention.get("Mode") != "COMPLIANCE":
            raise _error("retention_mismatch")
        until = _utc(retention.get("RetainUntilDate"))
        if minimum_days is not None and until < _utc(self.clock()) + timedelta(days=minimum_days):
            raise _error("retention_too_short")
        if expected is not None and until < expected:
            raise _error("retention_mismatch")
        return until

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
        body = None
        pending: Path | None = None
        descriptor = None
        primary = None
        published = False
        try:
            response = self._call("get_object", deadline, **kwargs)
            body = response.get("Body")
            if type(response.get("ContentLength")) is not int or response["ContentLength"] != size:
                raise _error("download_size_mismatch")
            if response.get("Metadata") != metadata:
                raise _error("metadata_mismatch")
            if body is None:
                raise _error("download_size_mismatch")
            descriptor, name = tempfile.mkstemp(prefix=".ciphertext-", suffix=".pending", dir=destination.parent)
            pending = Path(name)
            count = 0
            digest = hashlib.sha256()
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                os.fchmod(stream.fileno(), 0o600)
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
        except BaseException as exc:
            primary = _bounded_error(exc, "download_failure")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    primary = _cleanup_failed(primary)
            if body is not None:
                try:
                    body.close()
                except BaseException:
                    primary = _cleanup_failed(primary)
        # Close the remote response before publishing any verification file.
        if primary is None:
            try:
                _check_time(deadline)
                os.link(pending, destination)
                published = True
            except BaseException as exc:
                primary = _error("destination_exists") if isinstance(exc, FileExistsError) else _bounded_error(exc, "download_failure")
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except BaseException:
                primary = _cleanup_failed(primary)
        if primary is None:
            try:
                _check_time(deadline)
            except BaseException as exc:
                primary = _bounded_error(exc, "download_failure")
        if primary is not None and published:
            try:
                destination.unlink(missing_ok=True)
            except BaseException:
                primary = _cleanup_failed(primary)
        if primary is not None:
            raise primary from None

    def upload_and_verify(self, ciphertext_path: str | Path, *, key: str,
                          metadata: Mapping[str, str], retain_until: datetime,
                          deadline: float | None = None) -> dict[str, Any]:
        remote_uploaded = False
        directory = None
        primary = None
        stage = "verification"
        result = None
        try:
            tier, created, backup_id = self._key(key)
            metadata = _bounded_metadata(metadata)
            requested_lower_bound = _utc(retain_until)
            if metadata["tier"] != tier or metadata["backup_id"] != backup_id:
                raise _error("metadata_mismatch")
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
                _check_time(deadline)
                # The complete operation timeout is a conservative bounded
                # margin covering every remaining within-budget transfer and
                # verification step. A caller's date can only raise this floor.
                until = max(requested_lower_bound, max(created, _utc(self.clock())) + timedelta(
                    days=TIER_DAYS[tier], seconds=float(self.config.timeout_seconds)))
                uploaded = self._call(
                    "put_object", deadline, Bucket=self.config.bucket, Key=key, Body=_UploadReader(source, deadline),
                    ContentLength=size, ContentType="application/octet-stream", Metadata=metadata,
                    ContentMD5=base64.b64encode(md5.digest()).decode("ascii"),
                    ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=until,
                )
                remote_uploaded = True
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
            retained_until = self._retention(key, deadline, version=version, expected=until, minimum_days=TIER_DAYS[tier])
            directory = Path(tempfile.mkdtemp(prefix="room-ciphertext-verify-", dir=path.parent))
            os.chmod(directory, 0o700)
            stage = "download"
            self._download(key, directory / "verified.tar.age", metadata, size, deadline, version=version)
            stage = "verification"
            _check_time(deadline)
            if retained_until < _utc(self.clock()) + timedelta(days=TIER_DAYS[tier]):
                raise _error("retention_too_short")
            result = {
                "ciphertext_size": size, "ciphertext_sha256": sha.hexdigest(),
                "retention_verified": True, "download_verified": True,
            }
        except BaseException as exc:
            primary = _bounded_error(exc, "storage_unavailable")
        finally:
            if _clean_verification_directory(directory):
                primary = _cleanup_failed(primary)
        if primary is None:
            try:
                _check_time(deadline)
                if retained_until < _utc(self.clock()) + timedelta(days=TIER_DAYS[tier]):
                    raise _error("retention_too_short")
            except BaseException as exc:
                primary = _bounded_error(exc, "storage_unavailable")
        if primary is not None:
            if remote_uploaded or getattr(primary, "remote_uploaded", False):
                primary = _remote_error(primary, stage)
            raise primary from None
        return result

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
