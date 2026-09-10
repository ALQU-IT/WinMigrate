# Manifest schema (v1.0)

The manifest is the contract between the four stages and the record the user can
audit afterwards. `schema/manifest.schema.json` is the machine-readable version;
`winmigrate/models.py` is the implementation; a test
(`tests/test_schema_sync.py`) fails if the two drift apart.

## The three artefacts

A bundle is three files that travel together:

### 1. `<name>.dat` — the bundle

```
magic ("WINMIGRATE\0") || header_length (uint32 BE) || header_json || ciphertext
```

The header is plaintext because the KDF parameters and salt are needed *before*
a key exists:

```json
{
  "format": 1,
  "schema_version": "1.0",
  "tool": { "name": "winmigrate", "version": "0.1.0" },
  "created_utc": "2026-09-09T18:30:00Z",
  "cipher": "AES-256-GCM",
  "nonce_b64": "…",
  "kdf": {
    "name": "argon2id",
    "time_cost": 3, "memory_cost_kib": 262144, "parallelism": 4,
    "key_len": 32, "salt_b64": "…"
  }
}
```

It deliberately contains **no content description and no plaintext-payload
digest** — a digest of the plaintext, stored in the clear, would let anyone
holding the file confirm guesses about what is inside it without the passphrase.
Integrity of the ciphertext comes from the GCM tag, and from the ciphertext
digest in the sidecar.

The passphrase is never stored, never logged, and never written to disk.

### 2. `<name>.manifest.json` — the public sidecar

The redacted view, produced by `manifest.public_view()`. It exists so a bundle
can be identified, sized and integrity-checked **without the passphrase**. It
carries the ciphertext digest, totals, follow-up titles and the non-secret
items. `Sensitivity.SECRET` items appear only as stubs:

```json
{ "id": "dev:ssh", "category": "dev_config", "kind": "tree", "title": "SSH keys",
  "action": "capture", "sensitivity": "secret", "size_bytes": 4096,
  "file_count": 3, "redacted": true }
```

Size and count are kept because they are not sensitive and they make the totals
add up; the source path, archive path, record payload and notes are dropped.

`record` payloads are withheld from the sidecar too, unless the item sets
`record_public`. An installed-software inventory is not credential material, but
it names every application and version on the machine in a file that needs no
passphrase — a useful map for anyone looking for an unpatched version. The item
still appears, marked `record_withheld`, so nothing about the bundle's contents
is a mystery; only the detail moves inside the encryption. Small, genuinely
useful records — which sync provider owns a folder — set `record_public` and
stay visible.

### 3. `manifest.json` — inside the encrypted payload

The authoritative copy. Same schema, nothing redacted, per-item digests filled
in by the package stage. This is the copy restore trusts.

## Top-level keys

| Key | Meaning |
| --- | --- |
| `schema_version` | `MAJOR.MINOR`. Restore refuses a manifest with a different major version rather than silently dropping items it does not understand. |
| `tool` | Name and version that wrote it. |
| `created_utc` | `YYYY-MM-DDTHH:MM:SSZ`, the format used for every timestamp. |
| `mode` | `full` or `files-only` (no credential material captured at all). |
| `source` | Hostname, username, profile path, OS version/build, architecture, locale. |
| `scan` | Start, finish, duration and scan-level notes. |
| `sync_roots` | Detected cloud-sync folders and how much each holds. |
| `items` | The migration units — see below. |
| `followups` | What the user must do by hand, with steps. |
| `totals` | Roll-up used by the preview and the space pre-check. |
| `bundle` | Cipher, KDF, compression and digests (encrypted copy only). |

## Items

An item is one unit of migration. Its `id` is stable and unique within the
manifest (`files:documents`, `sync:onedrive:0`, `dev:ssh`), so a restore report
can refer back to a preview.

| Field | Meaning |
| --- | --- |
| `category` | `user_files`, `browser_profile`, `browser_passwords`, `wifi`, `printers`, `mapped_drives`, `env_vars`, `scheduled_tasks`, `fonts`, `outlook`, `notepad`, `dev_config`, `file_associations`, `software`, `office`. |
| `kind` | `tree` (directory), `file`, `record` (structured data living in the manifest itself, e.g. an app inventory), `report` (captured for reporting only; restores nothing). |
| `action` | `capture`, `skip`, or `manual` (only the user can do it). |
| `sensitivity` | `normal` or `secret`. Secret items exist only in the encrypted payload. |
| `skip_reason` | Why it is not captured: `synced`, `excluded`, `regenerable`, `cloud_placeholder`, `empty`, `unreadable`, `not_present`, `reparse_point`, `files_only_mode`. |
| `source_path` | Where it came from. |
| `archive_path` | Where it lives inside the payload, POSIX separators, so a bundle written on one machine reads identically on another. |
| `size_bytes`, `file_count` | What will actually be captured — never including anything in `skipped`. |
| `skipped` | Grouped accounting of what was left out beneath this item, with bytes and file counts. |
| `restore` | `target` (may contain `%USERPROFILE%`), `strategy`, `requires_elevation`, notes. |
| `digest`, `digest_algo` | Filled in by the package stage; `sha256` or `sha256-tree-v1`. |
| `record` | Payload for `kind: record` items (software inventory, Office installation, sync-root detail). |
| `record_public` | Whether that payload may appear in the plaintext sidecar. Off by default. |
| `notes` | Severity-tagged messages shown in the preview and the restore report. |

### Restore strategies

* `merge` — copy in, leaving files the bundle does not know about alone. The
  default for user data.
* `replace` — the target is emptied first.
* `guided` — the tool prepares the step and the **user completes it**: signing
  in to a sync provider, activating a licence, importing a password CSV.
* `manual` — reported only; Windows resists programmatic transfer (file
  associations are the standing example).

### Digests

* `sha256` — plain SHA-256 of a byte stream.
* `sha256-tree-v1` — SHA-256 over `"<sha256hex>  <relative/posix/path>\n"` lines
  sorted by path. Sorting and the `/` separator make the digest independent of
  filesystem ordering and of the OS that produced it, so a tree hashes the same
  on the source and target machines.

Restore verifies the whole-archive digest first, then per-item digests, and
fails loudly on any mismatch or truncation rather than restoring part of a
corrupt bundle.

## Follow-ups

`followups` is the human half of the migration, generated during **scan** so
the preview is honest about it before anything is captured, and reproduced
verbatim in the restore report:

```json
{
  "id": "signin:onedrive:c:/users/alice/onedrive",
  "title": "Sign in to onedrive on the new machine",
  "why": "OneDrive was not captured because it is already synced. Signing in brings it back without doubling the bundle.",
  "steps": ["Install and open the onedrive client on the new machine.", "Sign in as personal: alice@example.com.", "…"],
  "category": "user_files",
  "required": true
}
```

The public sidecar keeps the titles and drops the steps, so a bundle's follow-up
list can be seen at a glance without exposing account detail in a plaintext file.

## Compatibility

* **Minor** version bumps add optional fields. An older restore ignores them.
* **Major** version bumps change or remove a field; restore refuses the bundle
  and says so, rather than restoring a subset it has misunderstood.
