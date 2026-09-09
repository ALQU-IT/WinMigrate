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
capture  → stream the plan into an encrypted bundle (VSS for locked files),
           compressing, hashing and encrypting in a single pass
restore  → verify, decrypt, put files back, emit a to-do report
```

Collect and package are one stage rather than two: staging a copy of a profile
before packaging it would need a second copy of the data on disk, which a
217 GiB profile cannot afford. Files stream from source to bundle in one pass,
hashed on the way through.

`scan` writes nothing. `capture` writes only its bundle and sidecar. `restore`
is the only stage that writes into a user profile.

## Layout

```
winmigrate/
  cli.py             argparse CLI; the only module that talks to the terminal
  config.py          ScanConfig, exclusion rules, config-file loading
  models.py          the object model: Item, ScanResult, Followup, enums
  manifest.py        manifest build/validate, public view, bundle header spec
  platform_win.py    every Windows-specific read, behind a testable Environment
  logging_setup.py   log file + console handler + secret-redacting filter
  crypto.py          Argon2id/PBKDF2 key derivation and the chunked AEAD stream
  bundle.py          the .dat container: framing, gzip, streamed tar membership
  capture.py         plan → bundle, with space pre-check, hashing and VSS
  restore.py         verify → decrypt → write → report
  vss.py             Volume Shadow Copy lifecycle and path translation
  odt.py             Office Deployment Tool configuration generation
  reinstall.py       the reinstall artifacts, and running winget/Office setup
  report.py          rich rendering of the preview, capture and restore reports
  errors.py          exception hierarchy
  util/
    paths.py         long-path handling, containment tests, %VAR% expansion
    hashing.py       sha256 and the sha256-tree-v1 directory digest
    humanize.py      byte, count and duration formatting
  scan/
    runner.py        orchestration: profile → ScanResult
    userfiles.py     the profile walk and its skip accounting
    syncroots.py     OneDrive and Nextcloud detection (config-read only)
    software.py      installed-software inventory (registry + winget + Appx)
    office.py        Click-to-Run detection and licence status (no key extraction)
    devconfig.py     developer/credential config (encrypted-only, secret items)
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

## Software is a list, not a payload

Applications are inventoried, never copied. What migrates is the *list*, so the
target machine reinstalls from its own sources — carrying binaries would mean
the wrong build, an unlicensed copy, or something already out of date by the
time it lands.

Three sources are merged because none is complete on its own: registry uninstall
keys (classic desktop installers, per-machine, WOW6432Node and per-user),
`winget export` (what winget can reinstall, and under which package id), and
`Get-AppxPackage` (Store and UWP apps, which appear in neither). Anything winget
cannot reinstall goes into a "by hand" list rather than being quietly dropped.

The import file restore writes is **winget's own export, verbatim**.

Which applications winget can reinstall is answered by `winget list`, not
guessed: it reports every installed application with either a real package id or
a synthetic `ARP\...` id meaning "no package for this", and the display names it
prints come from the same registry values we read, so they join up exactly. A
name-similarity fallback covers only entries `winget list` did not mention, and
even then it decides a report label rather than the reinstall.

The first real machine this ran against showed why that matters: 302
applications, of which the original fuzzy matcher claimed 249 needed installing
by hand. It was taking only the *last* dotted component of a package id, so
`Microsoft.VCRedist.2015+.x64` was matched on `x64` and
`Microsoft.VisualStudio.2022.Community` on `community`. Every contiguous run of
components after the publisher is now tried, longest first.

Runtimes, redistributables and driver packages are classified separately. They
are really installed, but nobody reinstalls them deliberately — whatever needs
them brings them along — so counting them as chores buries the handful that
genuinely need a person. Office's Click-to-Run entries are excluded for the same
reason: it registers one per product and language, and listing them as
applications to reinstall by hand contradicts the Office follow-up printed
directly beneath them.

Three further things that same machine taught, all of them invisible to a
fixture:

* **MSIX packages join on identity, not display name.** `Get-AppxPackage` reports
  `Microsoft.WindowsTerminal`; `winget list` prints `Windows Terminal`. They only
  meet through the package id.
* **Generic words cannot carry a match.** `desktop`, `runtime`, `client` and the
  like appear in half of all package ids and identify nothing, so
  `Microsoft.VCLibs.Desktop.14` was matching `PowerAutomateDesktop` on the word
  "desktop".
* **The join rate is recorded** — rows listed, rows with a package, joins by
  exact and truncated name, and packages that joined to nothing — because it is
  the number that says whether the automatic/manual split can be trusted, and it
  can only be checked on a machine with real software on it.

### Installing is a separate, explicit step

Restore writes the reinstall inputs — the winget import file, an ODT
configuration, the by-hand list — into `WinMigrate-Reinstall\` and installs
nothing. `winmigrate reinstall` is a separate command that shows its plan and
asks before running. Putting files back is what the user asked for; installing
software is slow to undo, may need elevation, and pulls current versions rather
than the ones that were on the source machine.

### Office: reinstall, never key extraction

Click-to-Run's registry configuration gives the product ids, bitness, language
and channel needed to generate a matching ODT `configuration.xml`. `ospp.vbs
/dstatus` gives the licence family and status.

**No product key is extracted.** `ospp.vbs` itself prints the last five
characters of an installed key, and that is all that is recorded — enough for
the user to recognise which key they need, useless to anyone else. The generated
configuration sets `AUTOACTIVATE=0` and contains no `PIDKEY`. Reactivation is
the user's step, and the follow-up text differs by licence type: a subscription
reactivates on sign-in, retail needs the key the user owns, volume goes through
their administrator.

## Secret material: encrypted-only, placed by convention

Developer and credential config -- `.ssh`, `.aws/credentials`, `.gitconfig`,
`.npmrc` and the like -- is captured on by default and marked
`Sensitivity.SECRET`. Such items live only in the encrypted payload: the
plaintext sidecar carries a redacted stub (id, size, count -- no path, no
contents), the log never sees them, and `--files-only` drops them entirely
while still reporting that they were found.

Their placement is by archive-path convention, the same mechanism user files
use, under a `secrets/` prefix: `.ssh/id_rsa` is stored as `secrets/.ssh/id_rsa`
and restores to `~/.ssh/id_rsa`. This is safe precisely because tar member names
live *inside* the ciphertext -- a holder of the `.dat` without the passphrase
cannot read them, so the placement information is not exposed even though it is
not itself in the (encrypted) authoritative manifest's redacted twin. WSL
distributions are recorded as a list only; the virtual disks are not copied.

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
| 1 | Project structure, manifest schema, `scan` + preview, config, logging | **shipped** |
| 1b | File capture with sync-skip, packaging (AES-256-GCM + Argon2id), manifest hashes, restore with verification | **shipped** |
| 2 | VSS for locked files, resume, space pre-check, long-path handling, restore report | **shipped** (VSS untested on real Windows) |
| 3 | App inventory + `winget import`; Office detect + ODT reinstall | **shipped** (untested against real winget/Office) |
| 4 | Dev config (encrypted-only) + game-launcher classification **shipped**; browsers, password handoff, Wi-Fi, printers, env vars, fonts, Outlook next | in progress |
| 5 | Files-only mode polish, optional exclusion presets (device backups, VM images), config file, optional GUI | |

## Why the payload is encrypted in chunks

AES-GCM is safe for roughly 64 GiB under one (key, nonce) pair, and a single
`encrypt()` cannot exceed 2^39-256 bits at all. The first real profile this was
run against had 217 GiB to capture, so a one-shot encrypt is not merely slow —
it is invalid. The payload is therefore a sequence of independently
authenticated chunks, each binding its associated data to the bundle header, its
index in the stream, and whether it is the last one. That makes reordering,
duplication, splicing between bundles and truncation all detectable, and keeps
memory flat regardless of profile size.

The manifest is the **last** member of the tar, because per-item digests are not
known until the files have been read and reading a 217 GiB profile twice is not
acceptable. Restore hashes each file as it extracts and checks the manifest when
it arrives. Bundle authenticity does not depend on that ordering — the AEAD tags
already guarantee it — so the per-item digests serve their real purpose:
catching corruption that happened on the *source* side, before encryption.

## What cannot be resumed, and why

Restore resumes: files already present and matching are skipped, so an
interrupted run picks up where it left off, and each file is written to a
temporary name and renamed, so a half-written file is never mistaken for a
finished one.

**Capture cannot be resumed.** The bundle is one authenticated stream, so an
interrupted capture leaves no usable prefix. The partial file is deleted rather
than left looking restorable, and the run starts again. Splitting bundles into
resumable volumes would change that, at the cost of a more complex format; it is
not currently implemented.

## Scope assumption

The target case is **cross-machine, different Windows account**. Cloud-attached
state does not travel in the bundle and is not meant to: OneDrive and Nextcloud
content, browser passwords held by sync, and Microsoft 365 activation all
reattach on the new machine by signing in. The bundle carries what sign-in will
not bring back, and the follow-up list carries the rest.
