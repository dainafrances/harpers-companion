# Room encrypted backup and recovery operations

This subsystem is implemented for one private Backblaze B2 bucket using its
S3-compatible API and age X25519 encryption. Room and backup remain disabled by
default. A code commit does not authorize production setup, Room enablement,
production-data restoration, or a live drill. The legacy `colin_memory.sqlite3`
database remains outside this subsystem and has **no verified recovery path**.

## Manual storage and identity setup

1. The owner creates one **private, dedicated** B2 bucket and enables Object Lock
   before use. Use a separate administrative credential for setup, kept off the
   bot. Do not change the bucket to public or share its recovery objects.
2. Use COMPLIANCE retention. Configure either no bucket default or a COMPLIANCE
   default; a GOVERNANCE default is rejected. Each application upload explicitly
   requests its own COMPLIANCE retain-until date. Locked versions cannot have
   their retention shortened. See [Backblaze Object Lock](https://www.backblaze.com/docs/cloud-storage-object-lock).
3. Apply the four non-overlapping lifecycle rules below with administrative
   access. The application neither configures lifecycle/retention policies nor
   deletes remote objects. Do not add an overlapping broader rule that expires
   these objects earlier.
4. Create a bucket-restricted application key, restricted further to the chosen
   root prefix plus `/`. Grant only `listBuckets` (targeted bucket existence),
   `listFiles`, `readFiles`, `writeFiles`, `readBucketRetentions`,
   `readFileRetentions`, and `writeFileRetentions`. The transport calls Head
   Bucket rather than List Buckets, so it does not need `listAllBucketNames`.
   Verify these exact capabilities with the administrative account rather than
   using a broad console “read/write” preset. Do **not** grant `deleteFiles`,
   `writeBuckets` (including bucket deletion), `writeBucketRetentions`,
   `bypassGovernance`, or access to another bucket. [B2 app-key API capabilities](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys)
   and [key restrictions](https://www.backblaze.com/docs/cloud-storage-application-keys)
   describe the provider permissions.
5. On Daina's local machine, install the official reference
   [age implementation](https://github.com/FiloSottile/age/releases). Create the
   X25519 identity in a private local directory with `umask 077` and
   `age-keygen -o room-recovery-identity.txt`. Obtain its public recipient using
   `age-keygen -y room-recovery-identity.txt`. Neither command belongs in bot
   startup or Railway build scripts.
6. Put **only the public recipient** into the backup recipient setting. Store
   the private identity in Daina's password manager and keep a second encrypted
   or physically protected offline copy. Never place it in Railway, an
   environment variable, a committed file, logs, a chat message, or a shell
   argument. Local recovery reads it from an explicit private file or secure
   stdin. An identity file must be mode 0600, a regular file without links, and
   outside every Git checkout or worktree; the utility rejects any `.git` file
   or directory in its ancestor route. Losing every private-key copy makes the
   backups unrecoverable.
7. Create a separate local recovery key with only the bucket/prefix listing,
   read-file and read-retention capabilities needed for recovery. Do not give
   it upload or deletion capability. Do not reuse production bot credentials
   in a synthetic test.

## Retention and lifecycle rules

| Tier | Capture boundary | Minimum COMPLIANCE lock | Hide after upload | Delete after hiding |
|---|---|---:|---:|---:|
| daily | Every scheduled UTC run | 30 days | 32 days | 1 day |
| weekly | Monday UTC | 84 days | 86 days | 1 day |
| monthly | First UTC day of month | 366 days | 368 days | 1 day |
| manual | Owner's manual command | 90 days | 92 days | 1 day |

Weekly/monthly tiers reuse the same verified snapshot as that day's daily
object; every tier receives a separate upload and independent verification.
There is no environment override to shorten these minima. Retain-until dates
are finalized immediately before PUT, after local hashing and readiness. Each
adds the entire configured operation-timeout budget as a conservative transfer
margin. Retention is checked again against the full tier minimum at the current
verification time, including after verification cleanup. A delayed within-budget
transfer therefore cannot shorten a verified recovery point's minimum retention.

For the default root prefix, the B2 Native API `lifecycleRules` value is:

```json
[
  {"fileNamePrefix":"room-of-becoming/daily/","daysFromUploadingToHiding":32,"daysFromHidingToDeleting":1},
  {"fileNamePrefix":"room-of-becoming/weekly/","daysFromUploadingToHiding":86,"daysFromHidingToDeleting":1},
  {"fileNamePrefix":"room-of-becoming/monthly/","daysFromUploadingToHiding":368,"daysFromHidingToDeleting":1},
  {"fileNamePrefix":"room-of-becoming/manual/","daysFromUploadingToHiding":92,"daysFromHidingToDeleting":1}
]
```

If the root prefix changes, replace all four prefixes consistently and keep
previous-prefix rules until their objects have expired. Use the B2 Native API
or console consistently for lifecycle administration. Processing occurs daily;
the two-day margin leaves at least one complete processing day beyond each
minimum lock, followed by another day before deletion. A longer server-side
lock continues to prevent deletion. Before an administrator extends a lock or
sets a longer bucket default, extend the matching hide age to preserve at least
one full processing day after that longer retention expires. Hidden versions
still consume storage.
[Backblaze lifecycle semantics](https://www.backblaze.com/docs/cloud-storage-lifecycle-rules)

## Configuration and startup boundary

Populate only through a separately authorized private configuration release:

| Setting | Meaning/default |
|---|---|
| `BECOMING_BACKUP_ENABLED` | `false` |
| `BECOMING_BACKUP_REQUIRED_FOR_ROOM` | `true`; absent/invalid enabled-backup configuration blocks Room execution while ordinary companion operation continues |
| `BECOMING_BACKUP_TIME_UTC` | Strict `HH:MM`, default `03:00` UTC |
| `BECOMING_BACKUP_AGE_RECIPIENT` | Exactly one X25519 public recipient; never a private identity |
| `BECOMING_BACKUP_S3_ENDPOINT_URL` | Bucket's HTTPS B2 S3 endpoint, without bucket/path/query |
| `BECOMING_BACKUP_S3_REGION` | Bucket's matching region |
| `BECOMING_BACKUP_S3_BUCKET` | Dedicated private bucket |
| `BECOMING_BACKUP_S3_PREFIX` | Neutral root prefix, default `room-of-becoming` |
| `BECOMING_BACKUP_S3_ACCESS_KEY_ID` | Restricted application-key identifier |
| `BECOMING_BACKUP_S3_SECRET_ACCESS_KEY` | Restricted secret; never display it |
| `BECOMING_BACKUP_STATUS_PATH` | `/app/data/room-of-becoming/backup-status.json`; operational metadata outside the database file |
| `BECOMING_BACKUP_MAX_PLAINTEXT_BYTES` | Default 134217728 bytes; bounded archive/snapshot ceiling |
| `BECOMING_BACKUP_TIMEOUT_SECONDS` | Default 180 seconds; bounded complete-run budget |

No configuration values belong in operational responses. Invalid or partial
configuration must fail before database, provider or object-store access.
When backup is disabled, only the enablement and required-for-Room Boolean gates
are parsed. Saved recipient, credentials and other optional settings are ignored
and discarded, including malformed saved settings. The backup path performs no
optional SDK/native import, recipient parsing, client creation or storage work.
Required-for-Room still blocks executable Room operations when backup is disabled;
the explicit `false` exemption preserves Room operation without starting backup.
Ordinary chat, journal, status, Paintbox and continuity keep their own startup
paths. Backup startup is isolated and occurs after their timers. The scheduler
starts once per process despite reconnects, retries on later dates after
failure, and never invokes a model or creates Room memory/receipts.

## Capture, encryption and remote verification

Capture requires an existing valid Room database and refuses overlapping
manual/scheduled runs. The in-process storage lock pauses Room writes through
capture and verification. SQLite's backup API captures committed WAL changes;
a main-file-only copy is not a backup. Rollback-journal state requiring recovery
returns `maintenance_required`; never discard sidecars or recover a production
source merely to export it. All other Room writers must remain in this process
and use the integrated storage lock; external writers are unsupported.

Private temporary directories are mode 0700 and files mode 0600, outside the
persistent volume. Capture verifies marker/version, schema fingerprint,
integrity, foreign keys, independent ordered SQL manifests and immutable
history. The archive contains only the self-contained database and bounded
manifest. Plaintext hashes and counts are encrypted together with the database.
No title, content, reason, identity or route appears in keys or public metadata.

Pyrage 1.4.0 provides buffered streaming encryption. The plaintext ceiling still
applies. Ciphertext publication is atomic; plaintext and partial files are
cleaned independently on success and handled failure. Storage and backup locks
are released through unconditional outer cleanup, even if connection close,
rollback or unlink fails. An original bounded failure remains primary, with
`cleanup_failed=true` recording secondary cleanup trouble. Cleanup failure alone
is `cleanup_failure`; it never becomes success or permits uncertain plaintext
capture to proceed to encryption/upload. Independently removable files are still
removed. A file the operating system refuses to remove can remain in its private
temporary directory; the failure is reported without its path or raw exception.
This needs operator attention, rather than a false cleanup guarantee. The Room
write lock is released before
remote transfer. Storage readiness checks only the configured bucket/prefix.
For an empty prefix, preflight cannot prove permissions to read a nonexistent
object: the first actual encrypted upload must also pass HEAD, explicit
Get Object Retention and complete GET/hash verification before success.

Each upload requests `ObjectLockMode=COMPLIANCE` and an explicit date. HEAD
metadata alone is insufficient: the transport downloads the entire ciphertext
into another private file and independently verifies its length and SHA-256.
The upload's version identifier scopes verification where supplied. There are
no delete, bypass-governance, or bucket-administration calls. An uploaded object
whose verification fails can remain locked remotely; report failure and leave
its expiry to lifecycle rules. Never request deletion privileges to clean it up.
No multipart upload is used. Every tier whose PUT completed but whose subsequent
inspection, download, hash or verification cleanup failed is
`remote_unverified`, with a bounded reason such as `retention_too_short`,
`hash_mismatch` or `verification_cleanup_failure`. Its recovery point is not
accepted and it never advances `last_successful_time`. Verified sibling tiers
remain verified. A retry uses a fresh backup/object ID. Before a confirmed PUT,
the ordinary bounded failure category is retained; `not_uploaded` means no
successful PUT was confirmed, not proof that a timed-out remote request left
nothing behind.

Capture/encryption/hash loops check the deadline cooperatively. SDK connection
and blocking read calls have separate limits of at most 10 and 30 seconds;
deadline checks surround calls, bounded upload-body reads and each downloaded
chunk. Cancellation waits
for the active worker's bounded call and cleanup before releasing the backup
lock. It does not leave a detached upload running behind a returned result.

The local status document holds bounded operational fields only. It requires
`application="room-of-becoming-backup-status"` and integer `format_version=1`.
Only a strictly validated owned document may be replaced. Unrelated, malformed,
ambiguous, unowned or future-version JSON is preserved and rejected, including
known/default/configured companion persistence files and their aliases. An old
unowned backup-status file is not adopted or migrated; separately authorize its
archival/removal before activation. Missing status is created privately and
atomically. Existing owned status is replaced atomically after rechecking its
filesystem identity, ownership, size and modification/change timestamps.
Ordinary target replacement or mutation is detected. A separate hostile process
with arbitrary volume write access can still race the final check and rename;
that case is outside this single-service filesystem boundary.

Status is neither memory nor model context. `tier_results` distinguishes
`verified`, `not_uploaded` and `remote_unverified`, retaining only bounded failure
reasons and cleanup flags. Partial multi-tier results identify each successful
and failed tier; a partial or unverified run does not advance the last successful
time. For example, an operational status can describe a verified daily tier and
an unverified weekly tier with reason `hash_mismatch`, without any entry text,
object key or secret. Scheduled failures alert only through the exact private Room
route, with time/category/failure count. Routine success is silent; the first
fully verified success after process startup produces one private confirmation.
The owner can invoke `/becoming_backup_now` for the manual tier or
`/becoming_backup_status` for bounded state. Both require the existing exact
owner/route checks; neither command registers with disabled/invalid backup.

## First live verification and continuing operation

Live storage setup and all live-data operations require separate authorization.
Before Room enablement, verify bucket privacy, Object Lock, application-key
restriction, lifecycle policy and identity custody through the administrator.
Perform the synthetic recovery drill against a dedicated test bucket/prefix
and separately supplied test credentials, with Room still disabled.

After authorization for Room's first real entry, request one manual backup.
Accept it only after per-tier retention and full-download verification succeed.
Confirm the bounded status receipt, then independently download/decrypt and
restore that recovery object into a fresh private **local** directory. Verify
manifest, schema, integrity, exact revisions/receipts and immutable protections.
Do not point the bot at the local recovery copy. A provider success, HEAD
response, on-volume file or upload-only receipt does not establish recovery.

Review failures/overdue status at least daily. Check free persistent and private
temporary space before growth approaches the configured ceiling. Increase a
bounded limit only after measuring worst-case archive, encryption and transfer
cost and confirming recovery tooling uses the same bound. Keep the operator's
local reference age version and the recorded backup/restore-format version
with recovery instructions.

## Local recovery and monthly synthetic drill

`src.becoming_recovery` is a separate administrative utility and is never
imported by normal bot startup. Use its `--help` for exact invocation options.
Supply either one specific encrypted object key with separately supplied read
credentials, or a local ciphertext file and independently retained ciphertext
SHA-256. Supply the age identity only by an explicit private local file or
secure stdin. A hash fetched from the same untrusted local file is not an
independent integrity receipt.

The command forms are shown below with local path placeholders only. Do not
paste the resulting private values or key-file contents into chat or logs:

```text
python -B -m src.becoming_recovery restore --object-key OBJECT_KEY --read-credentials-file PRIVATE_READ_CREDENTIALS_JSON --identity-file PRIVATE_IDENTITY_FILE --destination NEW_EMPTY_PRIVATE_DIRECTORY
python -B -m src.becoming_recovery restore --ciphertext LOCAL_CIPHERTEXT --ciphertext-sha256 EXPECTED_SHA256 --metadata-file PRIVATE_METADATA_JSON --identity-stdin --destination NEW_EMPTY_PRIVATE_DIRECTORY
```

The read-credentials JSON contains exactly `endpoint_url`, `region`, `bucket`,
`prefix`, `access_key_id`, and `secret_access_key`; it is a local mode-0600 file
outside the checkout and production paths. The local metadata file is the
bounded upload receipt. Neither file carries an age identity. Secure stdin
reads the identity separately, with no terminal echo when attached to a TTY.

Recovery downloads/validates ciphertext, decrypts into disposable private
storage, rejects traversal/duplicates/links/unexpected archive members before
extraction, checks the encrypted manifest and database hash, and independently
validates SQL/schema/integrity. A fresh RoomStore process verifies history and
immutable protections. Destination must be new and empty: the utility never
overwrites the live database. Plaintext temporary files are cleaned; the
deliberately selected restored destination contains sensitive Room data and
must stay private.

Once a month, run the utility's explicit synthetic drill with harmless
actual-schema fixtures through the same encryption and S3 interfaces. Offline
proof uses `drill --synthetic --test-only` with the local fake S3 boundary.
After separate authorization, exercise the real test-bucket transport with:

```text
python -B -m src.becoming_recovery drill --synthetic --allow-remote-synthetic --storage-credentials-file PRIVATE_TEST_CREDENTIALS_JSON --identity-file LOCAL_PRIVATE_IDENTITY_FILE
```

Use only dedicated test credentials and the locally supplied test identity;
the generated public recipient is never a reason to upload its identity to
Railway. The remote drill deliberately leaves its harmless encrypted manual
recovery object under the 90-day COMPLIANCE lock, to expire through lifecycle
policy. Local cleanup does not claim to delete the protected remote object.
No remote drill is run automatically by this implementation. The drill must
refuse the known production database path, delete its synthetic source,
download/decrypt/restore, independently compare complete revisions/receipts,
prove immutability, append a synthetic revision and retrieve it in a second
fresh process. Remove the drill's fixtures, identities, manifests, ciphertext
and plaintext. Keep only a bounded pass/fail report. Never automate a
production-data recovery drill or use production credentials for this test.

## Production restoration and rollback

1. Obtain explicit authorization for the selected recovery point. **Restoring
   an older database intentionally discards later Room entries** unless they
   are preserved separately; acknowledge that data-loss boundary first.
2. Disable Room through an authorized configuration release, stop Room writers,
   preserve a consistent recovery object for current state, and choose a new
   private restore path. Do not overwrite the existing database or sidecars.
3. Download using the separate local read credential, verify the ciphertext
   receipt, decrypt locally and run the complete fresh-process validation.
4. Under separate authorization, transfer the validated restored database
   securely to the new persistent path. Recheck ownership/permissions and
   application validation. Select that path in configuration and enable Room.
5. Verify exact history and authorization. Retain the previous source intact
   until recovery has been accepted.

Application rollback, environment-variable rollback, database restore and
stale Discord-command cleanup are four separate operations. An old image does
not restore SQLite contents; disabling a feature does not guarantee removal of
already-published remote commands. Review configuration after application
rollback. No operation in this document silently promotes Room entries to
canonical memory or changes protected identity/relationship material.
