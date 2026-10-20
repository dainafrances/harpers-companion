"""Owned operational status documents; no backup payloads or optional SDKs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

from . import becoming

APPLICATION = "room-of-becoming-backup-status"
FORMAT_VERSION = 1
MAX_BYTES = 8192
TIERS = frozenset({"daily", "weekly", "monthly", "manual"})
FIELDS = frozenset({
    "application", "format_version", "last_attempt_time", "last_successful_time",
    "backup_id", "successful_tiers", "failed_tiers", "ciphertext_size",
    "consecutive_failure_count", "schema_version", "retention_verified",
    "download_verified", "outcome_category", "code_commit", "cleanup_failed",
    "tier_results",
})
_SIDECARS = ("-wal", "-shm", "-journal")


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() == timezone.utc.utcoffset(None)
    except ValueError:
        return False


def validate(value, categories, reasons=()):
    """Reject, rather than adopt or migrate, anything outside owned v1."""
    if (type(value) is not dict or set(value) - FIELDS
            or value.get("application") != APPLICATION
            or type(value.get("format_version")) is not int
            or value["format_version"] != FORMAT_VERSION
            or type(value.get("outcome_category")) is not str
            or value["outcome_category"] not in categories):
        raise ValueError("status_unavailable")
    for key in ("last_attempt_time", "last_successful_time"):
        if key in value and not _timestamp(value[key]):
            raise ValueError("status_unavailable")
    for key, pattern in (("backup_id", r"[0-9a-f]{32}"), ("code_commit", r"[0-9a-f]{40}")):
        if key in value and (type(value[key]) is not str or not re.fullmatch(pattern, value[key])):
            raise ValueError("status_unavailable")
    for key in ("successful_tiers", "failed_tiers"):
        if key in value:
            items = value[key]
            if (type(items) is not list or len(items) > len(TIERS)
                    or any(type(item) is not str or item not in TIERS for item in items)
                    or len(set(items)) != len(items)):
                raise ValueError("status_unavailable")
    if set(value.get("successful_tiers", ())) & set(value.get("failed_tiers", ())):
        raise ValueError("status_unavailable")
    for key in ("ciphertext_size", "consecutive_failure_count", "schema_version"):
        if key in value and (type(value[key]) is not int or not 0 <= value[key] <= 1_000_000_000):
            raise ValueError("status_unavailable")
    for key in ("retention_verified", "download_verified", "cleanup_failed"):
        if key in value and type(value[key]) is not bool:
            raise ValueError("status_unavailable")
    successful = set(value.get("successful_tiers", ()))
    failed = set(value.get("failed_tiers", ()))
    outcome = value["outcome_category"]
    if "successful_tiers" in value and any(
        key in value and value[key] != bool(successful)
        for key in ("retention_verified", "download_verified")
    ):
        raise ValueError("status_unavailable")
    if outcome == "success" and (
        value.get("cleanup_failed") is True or failed
        or any(value.get(key) is False for key in ("retention_verified", "download_verified"))
        or ("successful_tiers" in value and not successful)
        or value.get("consecutive_failure_count", 0) != 0
    ):
        raise ValueError("status_unavailable")
    if outcome == "partial_success" and (
        ("successful_tiers" in value and not successful)
        or ("failed_tiers" in value and not failed)
    ):
        raise ValueError("status_unavailable")
    if outcome == "remote_unverified" and successful:
        raise ValueError("status_unavailable")
    if "tier_results" in value:
        results = value["tier_results"]
        if type(results) is not dict or any(type(tier) is not str or tier not in TIERS for tier in results):
            raise ValueError("status_unavailable")
        if "successful_tiers" in value and "failed_tiers" in value and set(results) != successful | failed:
            raise ValueError("status_unavailable")
        for tier, result in results.items():
            if (type(result) is not dict or set(result) - {"outcome", "reason", "cleanup_failed"}
                    or type(result.get("outcome")) is not str
                    or result["outcome"] not in {"verified", "not_uploaded", "remote_unverified"}
                    or ("reason" in result and (type(result["reason"]) is not str
                        or result["reason"] not in set(categories) | set(reasons)))
                    or ("cleanup_failed" in result and type(result["cleanup_failed"]) is not bool)):
                raise ValueError("status_unavailable")
            verified = result["outcome"] == "verified"
            if (verified and ("reason" in result or result.get("cleanup_failed") is True)
                    or ("successful_tiers" in value and (tier in successful) != verified)
                    or ("failed_tiers" in value and (tier in failed) == verified)
                    or (outcome == "success" and not verified)
                    or (outcome == "remote_unverified" and verified)):
                raise ValueError("status_unavailable")
            if "reason" in result:
                if (result["outcome"] == "remote_unverified" and result["reason"] not in reasons
                        or (result["outcome"] == "not_uploaded" and result["reason"] in
                            {"success", "partial_success", "not_run", "remote_unverified"})):
                    raise ValueError("status_unavailable")
    # A detached copy prevents callers from changing cached ownership or fields.
    return json.loads(json.dumps(value, separators=(",", ":"), sort_keys=True))


def owned(value, categories, reasons=()):
    if type(value) is not dict:
        raise ValueError("status_unavailable")
    return validate({"application": APPLICATION, "format_version": FORMAT_VERSION, **value}, categories, reasons)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("status_unavailable")
        result[key] = value
    return result


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink, info.st_uid, info.st_gid)


def inspect(path, categories, reasons=()):
    """Return the owned document and stable identity, or (None, None) if absent."""
    try:
        initial = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_size > MAX_BYTES:
        raise ValueError("status_unavailable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if fingerprint(opened) != fingerprint(initial):
            raise ValueError("status_unavailable")
        payload = stream.read(MAX_BYTES + 1)
        final = os.fstat(stream.fileno())
    if (len(payload) > MAX_BYTES or fingerprint(final) != fingerprint(initial)
            or fingerprint(path.lstat()) != fingerprint(initial)):
        raise ValueError("status_unavailable")
    result = validate(json.loads(payload, object_pairs_hook=_pairs), categories, reasons)
    return result, fingerprint(initial)


def safe_path(path, database, *, protected_paths=(), env=None):
    env = os.environ if env is None else env
    try:
        path, database = Path(path).absolute(), Path(database).absolute()
        if ".." in path.parts or path.is_symlink():
            return False
        target, database = path.resolve(), database.resolve()
        if target.parent != database.parent or target.suffix != ".json":
            return False
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", target.name):
            return False
        root = becoming.REPOSITORY_ROOT.resolve()
        identity = becoming._filesystem_identity(root)
        if (becoming._private_repository_route(path, root, identity)
                or becoming._private_repository_route(target, root, identity)):
            return False
        # Common macOS /tmp and /var aliases are trusted system routes. Other
        # symlinked ancestors are not accepted as status publication locations.
        for parent in path.parents:
            if parent.is_symlink() and not (
                str(parent) in {"/tmp", "/var"}
                and str(parent.resolve()) in {"/private/tmp", "/private/var"}
            ):
                return False
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode):
            return False
        if parent == target.parent and (info.st_mode & 0o022 or info.st_uid != os.geteuid()):
            return False
        defaults = ("data/colin_memory.sqlite3", "data/paintbox-usage.sqlite", "data/discord_status.json")
        protected = [Path(item) for item in defaults]
        protected += [root / item for item in defaults]
        protected += [database.parent / Path(item).name for item in defaults]
        protected += [Path(env[name]) for name in ("DISCORD_STATUS_PATH", "PAINTBOX_USAGE_PATH") if env.get(name)]
        protected += list(map(Path, protected_paths))
        protected.append(database)
        for item in protected:
            item = item.expanduser().resolve()
            for other in (item, *(Path(str(item) + suffix) for suffix in _SIDECARS)):
                if (target == other or becoming._prospective_location(target) == becoming._prospective_location(other)
                        or (target.exists() and other.exists() and os.path.samefile(target, other))):
                    return False
        if target.exists():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
        return True
    except (OSError, RuntimeError, ValueError, TypeError, becoming.RoomError):
        return False
