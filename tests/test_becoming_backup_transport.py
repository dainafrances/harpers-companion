from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.becoming_backup import BackupError
from src.becoming_backup_transport import B2Transport, TIER_DAYS, _UploadReader


NOW = datetime(2030, 1, 1, 3, tzinfo=timezone.utc)
BACKUP_ID = "a" * 32
CIPHERTEXT = b"age-encryption.org/v1\nEntirely synthetic ciphertext fixture."


def configuration(**changes):
    values = dict(
        can_run=True, enabled=True, valid=True,
        endpoint_url="https://s3.test.invalid", region="synthetic-region",
        bucket="synthetic-bucket", prefix="room-of-becoming",
        access_key_id="synthetic-access", secret_access_key="synthetic-secret",
        timeout_seconds=60, max_plaintext_bytes=134217728,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def key_for(tier="daily"):
    return f"room-of-becoming/{tier}/2030/01/01/20300101T030000Z-{BACKUP_ID}.tar.age"


def metadata_for(tier="daily", ciphertext=CIPHERTEXT):
    return {
        "backup_format_version": "1", "schema_version": "1", "backup_id": BACKUP_ID,
        "tier": tier, "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "code_commit": "b" * 40,
    }


class FakeBody(io.BytesIO):
    def __init__(self, data, fail_after_read=False):
        super().__init__(data)
        self.fail_after_read = fail_after_read
        self.read_count = 0

    def read(self, size=-1):
        self.read_count += 1
        if self.fail_after_read and self.read_count > 1:
            raise TimeoutError("PRIVATE STREAM FAILURE")
        return super().read(size)


class FakeS3:
    """Only the six permitted APIs exist; all requests are audited."""

    def __init__(self):
        self.calls = []
        self.objects = {}
        self.fail = None
        self.lock = {"ObjectLockEnabled": "Enabled"}
        self.head_changes = {}
        self.get_changes = {}
        self.retention_changes = {}
        self.download_bytes = None
        self.partial_put = False
        self.existing_key = None
        self.body = None
        self.fail_after_read = False

    def _call(self, method, kwargs):
        self.calls.append((method, {k: v for k, v in kwargs.items() if k != "Body"}))
        if self.fail == method:
            raise RuntimeError("RAW PRIVATE EXCEPTION MUST NEVER ESCAPE")
        if self.fail == "timeout:" + method:
            raise TimeoutError("RAW PRIVATE TIMEOUT")
        if "Key" in kwargs:
            assert kwargs["Key"].startswith("room-of-becoming/")
        if "Prefix" in kwargs:
            assert kwargs["Prefix"] == "room-of-becoming/"
        assert kwargs["Bucket"] == "synthetic-bucket"
        assert not any("bypass" in k.lower() for k in kwargs)

    def head_bucket(self, **kwargs):
        self._call("head_bucket", kwargs)
        return {}

    def get_object_lock_configuration(self, **kwargs):
        self._call("get_object_lock_configuration", kwargs)
        return {"ObjectLockConfiguration": self.lock}

    def list_objects_v2(self, **kwargs):
        self._call("list_objects_v2", kwargs)
        assert kwargs["MaxKeys"] == 1
        return {"Contents": [{"Key": self.existing_key}]} if self.existing_key else {}

    def put_object(self, **kwargs):
        self._call("put_object", kwargs)
        data = kwargs["Body"].read()
        if self.partial_put:
            data = data[:-1]
        self.objects[kwargs["Key"]] = {
            "data": data, "Metadata": dict(kwargs["Metadata"]),
            "ContentType": kwargs["ContentType"], "VersionId": "synthetic-version",
            "Retention": {"Mode": kwargs["ObjectLockMode"], "RetainUntilDate": kwargs["ObjectLockRetainUntilDate"]},
        }
        return {"VersionId": "synthetic-version"}

    def head_object(self, **kwargs):
        self._call("head_object", kwargs)
        obj = self.objects[kwargs["Key"]]
        return {
            "Metadata": dict(obj["Metadata"]), "ContentLength": len(obj["data"]),
            "ContentType": obj["ContentType"], "VersionId": obj["VersionId"], **self.head_changes,
        }

    def get_object_retention(self, **kwargs):
        self._call("get_object_retention", kwargs)
        retention = self.objects[kwargs["Key"]]["Retention"]
        return {"Retention": {**retention, **self.retention_changes}}

    def get_object(self, **kwargs):
        self._call("get_object", kwargs)
        obj = self.objects[kwargs["Key"]]
        self.body = FakeBody(self.download_bytes if self.download_bytes is not None else obj["data"], self.fail_after_read)
        return {"Metadata": dict(obj["Metadata"]), "ContentLength": len(obj["data"]), "Body": self.body, **self.get_changes}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="room-transport-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.tar.age"
        self.path.write_bytes(CIPHERTEXT)
        self.path.chmod(0o600)
        self.fake = FakeS3()
        self.transport = B2Transport(configuration(), client=self.fake, clock=lambda: NOW)

    def upload(self, tier="daily", **changes):
        values = dict(key=key_for(tier), metadata=metadata_for(tier), retain_until=NOW + timedelta(days=TIER_DAYS[tier]))
        values.update(changes)
        return self.transport.upload_and_verify(self.path, **values)

    def assert_category(self, category, operation):
        with self.assertRaises(BackupError) as caught:
            operation()
        self.assertEqual(caught.exception.category, category)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_explicit_client_configuration_never_uses_environment_credentials(self):
        with patch("boto3.client", return_value=self.fake) as factory:
            B2Transport(configuration())
        args, kwargs = factory.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(kwargs["endpoint_url"], "https://s3.test.invalid")
        self.assertEqual(kwargs["region_name"], "synthetic-region")
        self.assertEqual(kwargs["aws_access_key_id"], "synthetic-access")
        self.assertEqual(kwargs["aws_secret_access_key"], "synthetic-secret")
        sdk = kwargs["config"]
        self.assertEqual(sdk.signature_version, "s3v4")
        self.assertEqual(sdk.s3, {"addressing_style": "path"})
        self.assertEqual(sdk.retries, {"max_attempts": 0})
        self.assertLessEqual(sdk.connect_timeout, 10)
        self.assertLessEqual(sdk.read_timeout, 30)

    def test_invalid_or_disabled_configuration_cannot_create_client(self):
        for changes in ({"can_run": False}, {"access_key_id": ""}, {"secret_access_key": ""}, {"prefix": "room/../other"}):
            with self.subTest(changes=changes), patch("boto3.client") as factory:
                self.assert_category("invalid_configuration", lambda: B2Transport(configuration(**changes)))
                factory.assert_not_called()

    def test_readiness_on_empty_prefix_does_not_claim_upload_proven(self):
        result = self.transport.readiness()
        self.assertEqual(result, {"bucket_reachable": True, "prefix_listable": True, "object_lock_enabled": True})
        self.assertEqual([method for method, _ in self.fake.calls], ["head_bucket", "get_object_lock_configuration", "list_objects_v2"])

    def test_each_tier_has_explicit_compliance_minimum_and_full_download(self):
        for tier, days in TIER_DAYS.items():
            with self.subTest(tier=tier):
                self.fake.calls.clear()
                result = self.upload(tier)
                self.assertEqual(result["ciphertext_size"], len(CIPHERTEXT))
                self.assertTrue(result["retention_verified"] and result["download_verified"])
                put = next(kwargs for method, kwargs in self.fake.calls if method == "put_object")
                self.assertEqual(put["ObjectLockMode"], "COMPLIANCE")
                self.assertEqual(put["ObjectLockRetainUntilDate"], NOW + timedelta(days=days))
                self.assertEqual(put["ContentType"], "application/octet-stream")
                self.assertTrue(put["ContentMD5"])
                for method, kwargs in self.fake.calls:
                    if method in {"head_object", "get_object_retention", "get_object"}:
                        self.assertEqual(kwargs["VersionId"], "synthetic-version")
                self.assertTrue(self.fake.body.closed)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_no_delete_bucket_administration_or_governance_bypass(self):
        self.upload()
        allowed = {"head_bucket", "get_object_lock_configuration", "list_objects_v2", "put_object", "head_object", "get_object_retention", "get_object"}
        self.assertTrue(all(method in allowed for method, _ in self.fake.calls))
        for _, kwargs in self.fake.calls:
            self.assertFalse(any("bypass" in key.lower() for key in kwargs))

    def test_object_lock_absent_or_governance_default_rejected_before_upload(self):
        for lock in ({}, {"ObjectLockEnabled": "Disabled"}, {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE"}}}):
            self.fake.lock = lock
            self.assert_category("object_lock_required", self.upload)
        self.assertNotIn("put_object", [method for method, _ in self.fake.calls])

    def test_all_required_remote_capabilities_fail_closed(self):
        for stage in ("head_bucket", "get_object_lock_configuration", "list_objects_v2", "put_object", "head_object", "get_object_retention", "get_object"):
            with self.subTest(stage=stage):
                self.fake.fail = stage
                self.assert_category("storage_unavailable", self.upload)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_timeout_is_bounded(self):
        self.fake.fail = "timeout:put_object"
        self.assert_category("timeout", self.upload)

    def test_expired_deadline_prevents_network(self):
        with patch("src.becoming_backup_transport.time.monotonic", side_effect=[0, 61]):
            self.assert_category("timeout", self.upload)
        self.assertFalse(self.fake.calls)

    def test_outer_deadline_is_never_extended_by_transport(self):
        with patch("src.becoming_backup_transport.time.monotonic", return_value=100):
            self.assert_category("timeout", lambda: self.upload(deadline=99))
            self.assert_category("timeout", lambda: self.transport.readiness(deadline=99))
            self.assert_category("timeout", lambda: self.transport.download_for_restore(key_for(), self.path.parent / "restore.age", deadline=99))
        self.assertFalse(self.fake.calls)

    def test_put_body_checks_deadline_during_slow_upload_and_preserves_bounded_error(self):
        from botocore.exceptions import HTTPClientError
        clock = [100.0]
        chunks = []
        payload = CIPHERTEXT * 10000
        self.path.write_bytes(payload)

        def slow_put(**kwargs):
            self.fake._call("put_object", kwargs)
            body = kwargs["Body"]
            self.assertTrue(body.seekable() and body.readable())
            self.assertEqual(body.tell(), 0)
            body.seek(0)
            try:
                while block := body.read(1024 * 1024):
                    chunks.append(len(block))
                    clock[0] += 40
            except BackupError as error:
                # Match the real botocore HTTP-session wrapping behavior.
                raise HTTPClientError(error=error) from error
            self.fail("Slow upload should stop before reaching completion")

        self.fake.put_object = slow_put
        with patch("src.becoming_backup_transport.time.monotonic", side_effect=lambda: clock[0]):
            self.assert_category("timeout", lambda: self.upload(metadata=metadata_for(ciphertext=payload)))
        self.assertEqual(chunks, [65536, 65536])
        self.assertLess(sum(chunks), len(payload))
        self.assertFalse(self.fake.objects)
        self.assertNotIn("head_object", [method for method, _ in self.fake.calls])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_put_reader_preserves_seek_readinto_and_read_all_semantics(self):
        payload = CIPHERTEXT * 10000
        reader = _UploadReader(io.BytesIO(payload), float("inf"))
        self.assertEqual(reader.read(1024 * 1024), payload[:65536])
        self.assertEqual(reader.tell(), 65536)
        reader.seek(0)
        buffer = bytearray(8)
        self.assertEqual(reader.readinto(buffer), 8)
        self.assertEqual(bytes(buffer), payload[:8])
        reader.seek(-8, os.SEEK_END)
        self.assertEqual(reader.read(), payload[-8:])
        reader.seek(0)
        self.assertEqual(reader.read(), payload)

    def test_partial_upload_rejected(self):
        self.fake.partial_put = True
        self.assert_category("download_size_mismatch", self.upload)

    def test_head_metadata_mismatch_rejected(self):
        self.fake.head_changes = {"Metadata": {**metadata_for(), "ciphertext_sha256": "0" * 64}}
        self.assert_category("metadata_mismatch", self.upload)

    def test_head_content_type_mismatch_rejected(self):
        self.fake.head_changes = {"ContentType": "text/plain"}
        self.assert_category("metadata_mismatch", self.upload)

    def test_remote_retention_wrong_mode_or_shorter_date_rejected(self):
        for changes in ({"Mode": "GOVERNANCE"}, {"RetainUntilDate": NOW}, {"RetainUntilDate": "invalid"}):
            with self.subTest(changes=changes):
                self.fake.retention_changes = changes
                self.assert_category("retention_mismatch", self.upload)

    def test_longer_remote_retention_is_accepted(self):
        self.fake.retention_changes = {"RetainUntilDate": NOW + timedelta(days=400)}
        self.assertTrue(self.upload()["download_verified"])

    def test_requested_retention_cannot_weaken_any_fixed_minimum(self):
        for tier, days in TIER_DAYS.items():
            self.assert_category("retention_mismatch", lambda: self.upload(tier, retain_until=NOW + timedelta(days=days, seconds=-1)))
        self.assertFalse(self.fake.calls)

    def test_late_upload_uses_actual_upload_time_for_minimum(self):
        self.transport.clock = lambda: NOW + timedelta(days=2)
        self.assert_category("retention_mismatch", self.upload)
        self.assertFalse(self.fake.calls)

    def test_download_content_length_header_mismatch_rejected(self):
        self.fake.get_changes = {"ContentLength": len(CIPHERTEXT) - 1}
        self.assert_category("download_size_mismatch", self.upload)
        self.assertTrue(self.fake.body.closed)

    def test_download_short_or_long_body_rejected_and_cleaned(self):
        for data in (CIPHERTEXT[:-1], CIPHERTEXT + b"x"):
            self.fake.download_bytes = data
            self.assert_category("download_size_mismatch", self.upload)
            self.assertTrue(self.fake.body.closed)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_independent_download_sha_mismatch_rejected(self):
        self.fake.download_bytes = b"x" * len(CIPHERTEXT)
        self.assert_category("download_hash_mismatch", self.upload)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_timeout_after_partial_download_cleans_pending_and_closes_body(self):
        self.fake.fail_after_read = True
        self.assert_category("timeout", self.upload)
        self.assertTrue(self.fake.body.closed)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_get_metadata_mismatch_rejected(self):
        self.fake.get_changes = {"Metadata": {}}
        self.assert_category("metadata_mismatch", self.upload)

    def test_scoped_listing_cannot_trigger_access_outside_prefix(self):
        self.fake.existing_key = "unrelated/private.sqlite3"
        self.assert_category("invalid_object_key", self.upload)
        self.assertNotIn("get_object_retention", [method for method, _ in self.fake.calls])

    def test_existing_scoped_object_retention_is_inspected_before_upload(self):
        self.upload()
        self.fake.existing_key = key_for()
        self.fake.calls.clear()
        self.upload()
        methods = [method for method, _ in self.fake.calls]
        self.assertLess(methods.index("get_object_retention"), methods.index("put_object"))

    def test_invalid_keys_never_access_storage(self):
        for key in ("other/secret", "room-of-becoming/../outside", key_for().replace("/01/01/", "/01/02/"), key_for().replace(BACKUP_ID, "private-title"), key_for().replace("/daily/", "/unknown/")):
            self.assert_category("invalid_object_key", lambda: self.upload(key=key))
        self.assertFalse(self.fake.calls)

    def test_metadata_rejects_private_fields_and_invalid_public_fields(self):
        for changes in ({"plaintext_sha256": "a" * 64}, {"title": "PRIVATE TITLE"}, {"tier": "weekly"}, {"ciphertext_sha256": "f" * 64}, {"backup_id": "invalid"}, {"code_commit": "PRIVATE"}):
            self.assert_category("metadata_mismatch", lambda: self.upload(metadata={**metadata_for(), **changes}))
        self.assertFalse(self.fake.calls)

    def test_ciphertext_ceiling_enforced_before_remote_access(self):
        self.transport.config.max_plaintext_bytes = 1
        with self.path.open("ab") as stream:
            stream.write(b"x" * 1048576)
        self.assert_category("ciphertext_invalid", self.upload)
        self.assertFalse(self.fake.calls)

    def test_restore_download_verifies_all_bytes_to_new_private_file(self):
        self.upload()
        destination = self.path.parent / "restore.age"
        metadata = self.transport.download_for_restore(key_for(), destination)
        self.assertEqual(destination.read_bytes(), CIPHERTEXT)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(metadata["ciphertext_size"], len(CIPHERTEXT))
        self.assertEqual(metadata["ciphertext_sha256"], hashlib.sha256(CIPHERTEXT).hexdigest())
        self.assertTrue(metadata["download_verified"])

    def test_restore_refuses_existing_destination_without_network(self):
        self.assert_category("destination_exists", lambda: self.transport.download_for_restore(key_for(), self.path))
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.path.read_bytes(), CIPHERTEXT)

    def test_restore_corruption_leaves_no_complete_or_pending_file(self):
        self.upload()
        destination = self.path.parent / "restore.age"
        self.fake.download_bytes = b"x" * len(CIPHERTEXT)
        self.assert_category("download_hash_mismatch", lambda: self.transport.download_for_restore(key_for(), destination))
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_restore_bad_metadata_rejected_before_body_download(self):
        self.upload()
        self.fake.calls.clear()
        self.fake.head_changes = {"Metadata": {"title": "PRIVATE"}}
        self.assert_category("metadata_mismatch", lambda: self.transport.download_for_restore(key_for(), self.path.parent / "restore.age"))
        self.assertNotIn("get_object", [method for method, _ in self.fake.calls])


class ReferenceAgeInteroperabilityTests(unittest.TestCase):
    def test_streaming_pyrage_and_independent_reference_age_interoperate(self):
        import pyrage
        reference = os.environ.get("BECOMING_TEST_REFERENCE_AGE") or shutil.which("age")
        if not reference:
            self.skipTest("Independent reference age binary is not installed")
        plaintext = b"Harmless synthetic streaming interoperability proof.\n" * 8192
        expected = hashlib.sha256(plaintext).hexdigest()
        with tempfile.TemporaryDirectory(prefix="room-age-reference-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            identity = pyrage.x25519.Identity.generate()
            identity_path = root / "synthetic-identity.txt"
            recipient_path = root / "synthetic-recipient.txt"
            plaintext_path = root / "synthetic-plaintext.bin"
            pyrage_path = root / "synthetic-pyrage.age"
            reference_path = root / "synthetic-reference.age"
            decrypted_path = root / "synthetic-decrypted.bin"
            for path, value in ((identity_path, str(identity).encode()), (recipient_path, str(identity.to_public()).encode()), (plaintext_path, plaintext)):
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    stream.write(value)
            with plaintext_path.open("rb") as source, pyrage_path.open("xb") as target:
                pyrage_path.chmod(0o600)
                pyrage.encrypt_io(source, target, [identity.to_public()])
            result = subprocess.run(
                [reference, "--decrypt", "--identity", str(identity_path), "--output", str(decrypted_path), str(pyrage_path)],
                capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, "Reference decryption rejected synthetic ciphertext")
            self.assertEqual(hashlib.sha256(decrypted_path.read_bytes()).hexdigest(), expected)
            result = subprocess.run(
                [reference, "--encrypt", "--recipients-file", str(recipient_path), "--output", str(reference_path), str(plaintext_path)],
                capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, "Reference encryption rejected synthetic recipient")
            decrypted = io.BytesIO()
            with reference_path.open("rb") as source:
                pyrage.decrypt_io(source, decrypted, [identity])
            self.assertEqual(hashlib.sha256(decrypted.getvalue()).hexdigest(), expected)
        self.assertFalse(Path(directory).exists())


if __name__ == "__main__":
    unittest.main()
