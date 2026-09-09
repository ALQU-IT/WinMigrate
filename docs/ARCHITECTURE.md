# WinMigrate architecture

WinMigrate is a Windows user-profile migration and backup tool, run **by the
owner of the profile it captures**. It inventories a user's data and installed
software, packages it into a single encrypted, integrity-checked archive, and
restores it on a new machine — then tells the user, plainly, what is left for
them to do by hand.

## Operating principles

These are enforced by the code, not just documented:

| Principle | How it shows up in the code |
| --- | --- |
| **Owner-run and visible** | Every stage renders what it touched. `scan` is read-only and its preview is the same `ScanResult` object that capture consumes, so the preview cannot promise something different from what happens. |
| **Nothing leaves the machine** | There is no network client anywhere in the package. The only output is a local file. |
| **Consent-first restore** | Anything the OS gates behind human authentication — account sign-ins, licence activation, password import — is a `RestoreStrategy.GUIDED` item plus a `Followup`. The tool prepares and hands off; it never impersonates the user. |
| **Secrets are encrypted, never logged** | `Sensitivity.SECRET` items are written only into the encrypted payload. `manifest.public_view()` reduces them to a stub, and `SecretRedactingFilter` drops any log record marked as carrying secrets. |

### Explicit boundary: browser passwords

WinMigrate does **not** decrypt browsers' saved-password stores. It will not
read Chromium `Login Data` and call DPAPI `CryptUnprotectData`, and it will not
attempt to defeat app-bound encryption. That routine is an infostealer payload
regardless of who runs it, and it is out of scope.

The sanctioned path, landing in phase 4:

1. Detect sign-in and sync state per browser by **reading configuration files
   only** (Chromium `Preferences`/`Local State`, Firefox `signedInUser.json`
   and `prefs.js`).
2. Sync on → report "these are in the cloud; sign in to `<account>` on the new
   machine and they sync down." Nothing is handled locally.
3. Sync off → guide the user through the browser's own export (Settings →
   Autofill → Passwords → Export), which triggers the browser's legitimate OS
   re-authentication. The tool encrypts the CSV *the user produced* into the
   bundle and guides re-import on restore.

The same rule applies to Office: detect the edition and reinstall it; never
extract or recover a product key.

## Stages

Four stages, each independently testable, each consuming the previous one's
output:

```
scan     → discover what exists; produce a ScanResult and a preview plan
collect  → copy data (VSS for locked files), skipping already-synced content
package  → compress, encrypt to <name>.dat, write the manifest with hashes
restore  → verify, decrypt, replace files, reinstall apps, emit a to-do report
```

`scan` writes nothing. `collect` and `package` write only inside the output
directory. `restore` is the only stage that writes into a user profile.

## Layout

```
winmigrate/
  cli.py             argparse CLI; the only module that talks to the terminal
  config.py          ScanConfig, exclusion rules, config-file loading
  models.py          the object model: Item, ScanResult, Followup, enums
  manifest.py        manifest build/validate, public view, bundle header spec
  platform_win.py    every Windows-specific read, behind a testable Environment
  logging_setup.py   log file + console handler + secret-redacting filter
  report.py          rich rendering of the preview (and later, restore reports)
  errors.py          exception hierarchy
  util/
    paths.py         long-path handling, containment tests, %VAR% expansion
    hashing.py       sha256 and the sha256-tree-v1 directory digest
    humanize.py      byte, count and duration formatting
  scan/
    runner.py        orchestration: profile → ScanResult
    userfiles.py     the profile walk and its skip accounting
    syncroots.py     OneDrive and Nextcloud detection (config-read only)
schema/manifest.schema.json   machine-readable mirror of the manifest
docs/manifest-schema.md       prose description of the same
tests/                        pytest suite; runs on any OS via fixture profiles
```

### Why `platform_win.Environment` exists

Every registry read, known-folder resolution and file-attribute check goes
through one object. On Windows it uses `winreg` and `os.stat`; in tests it is
constructed over a fixture profile tree with a supplied registry dictionary. So
the scan logic — including OneDrive Known Folder Move redirection, which is
awkward to reproduce otherwise — is exercised on any OS, and the Windows
specifics stay in one reviewable file.

`require_windows()` still refuses to run against a live profile off Windows;
`--profile-root` (or `WINMIGRATE_ALLOW_NON_WINDOWS=1`) enables only the
read-only stages against a fixture.

## Skip accounting

A migration tool that quietly drops data is worse than one that copies too much,
so every byte not captured is attributed to a reason: `synced`, `regenerable`,
`excluded`, `cloud_placeholder`, `reparse_point`, `unreadable`, `empty`,
`not_present`, `files_only_mode`.

Two rules keep the numbers honest:

* **Synced content is counted once, on the sync root.** A folder can be both
  inside `C:\Users\me\OneDrive` and the target of Known Folder Move; counting it
  in the folder *and* on the sync root would inflate the total. `syncroots.measure()`
  measures each root once, and the profile walk records synced subtrees as
  explanatory entries carrying no bytes.
* **`--fast` reduces precision, not honesty.** It skips the extra walk into
  subtrees that will not be captured, so those report zero bytes rather than a
  guess.

## Phase plan

| Phase | Contents | Status |
| --- | --- | --- |
| 1 | Project structure, manifest schema, `scan` + preview, config, logging | **shipped** (preview half) |
| 1b | File capture with sync-skip, packaging (AES-256-GCM + Argon2id), manifest hashes, restore with verification | next |
| 2 | VSS for locked files, resume, space pre-check, long-path handling, restore report | |
| 3 | App inventory + `winget import`; Office detect + ODT reinstall | |
| 4 | Browser profiles, sign-in/sync detection, native-export password handoff; Wi-Fi, printers, env vars, fonts, dev config, Outlook | |
| 5 | Files-only mode polish, config file, optional GUI | |

Phase 1 deliberately ships no `capture`, `package` or `restore` subcommand
rather than stubbing them, so `--help` never advertises something that does not
work.

## Scope assumption

The target case is **cross-machine, different Windows account**. Cloud-attached
state does not travel in the bundle and is not meant to: OneDrive and Nextcloud
content, browser passwords held by sync, and Microsoft 365 activation all
reattach on the new machine by signing in. The bundle carries what sign-in will
not bring back, and the follow-up list carries the rest.
