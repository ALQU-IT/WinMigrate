# WinMigrate

Migrate your Windows user profile — data, settings and installed software — to a
new machine, as a single encrypted, integrity-checked archive.

WinMigrate is **owner-run**: you run it on your own machine, it shows you
everything it is doing, and its only output is a local file you control. It
never uploads anything, and anything Windows deliberately gates behind human
authentication — account sign-ins, licence activation, password import — it
prepares for you and hands over, with instructions. It never impersonates you.

> **Status: phases 1–2.** `scan`, `capture`, `inspect` and `restore` all work,
> with encryption, integrity checking, a space pre-check, shadow copies for
> locked files and a resumable restore. Software inventory, Office and browser
> handling are next; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

Requires Python 3.11+ and Windows.

```powershell
py -3.11 -m pip install -e .
```

If `winmigrate` is then "not recognized as a name of a cmdlet", pip installed
the package but its `Scripts` directory is not on your `PATH`. Either use the
module form, which always works:

```powershell
py -3.11 -m winmigrate scan
```

or add the directory to your user `PATH` once, then open a new terminal:

```powershell
$scripts = & py -3.11 -c "import sysconfig; print(sysconfig.get_path('scripts'))"
$userPath = [Environment]::GetEnvironmentVariable('Path','User')
[Environment]::SetEnvironmentVariable('Path', "$userPath;$scripts", 'User')
```

`py -3.11 -m pip show winmigrate` confirms the package is installed.

## Use

```powershell
winmigrate scan                      # inventory this profile and preview the plan
winmigrate scan --verbose            # include folders that are empty or absent
winmigrate scan --json               # the same plan as JSON

winmigrate capture -o mypc.dat       # write the plan into an encrypted bundle
winmigrate inspect mypc.dat          # describe a bundle without its passphrase
winmigrate restore mypc.dat -n       # show what a restore would do
winmigrate restore mypc.dat          # put it back on the new machine
```

Capture prompts for a passphrase and asks before writing. There is no
`--passphrase` flag: a passphrase on a command line lands in shell history and
in the process list, so it is either typed at the prompt or read from a file you
control with `--passphrase-file`.

Run capture from an **elevated** prompt to allow a Volume Shadow Copy, which
lets files held open by running programs be captured cleanly. Without it those
files are reported as failures rather than silently missed.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--include-regenerable` | Keep `node_modules`, build output and similar directories that otherwise rebuild themselves |
| `--no-skip-synced` | Capture cloud-synced folders too, instead of relying on signing in |
| `--files-only` | Plan a migration with no credential material at all |
| `--fast` | Do not measure the size of what is being skipped |
| `--exclude PATTERN` / `--include PATTERN` | Adjust the exclusion rules (repeatable) |
| `--log-file PATH` | Write a detailed log |

Restore flags:

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Report what would be restored, write nothing |
| `--overwrite` | Replace existing files that differ (default: keep yours) |
| `--item ID` | Restore only one item, e.g. `--item files:documents` |
| `-d`, `--destination` | Restore somewhere other than the current profile |

An interrupted restore can simply be re-run: files already present and matching
are skipped.

`scan` is read-only: it never opens a file's contents, never writes to the
profile it inspects, and cannot trigger a cloud download.

### What a scan tells you

* how much data would be captured, and from where;
* what would be skipped and **why** — already synced, regenerable build output,
  junk, online-only placeholders, unreadable;
* which cloud-sync folders were found, and which account owns each;
* what you will still have to do yourself once the restore is done.

## What it will not do

WinMigrate does **not** decrypt browsers' saved-password stores, and will not:
that routine is an infostealer payload regardless of who runs it. Instead it
detects sign-in and sync state from configuration files only, and either tells
you the passwords will sync down when you sign in, or walks you through the
browser's own export and re-import. It likewise detects and reinstalls your
Office edition without ever extracting a product key.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full boundary and the
principles the code enforces, and [docs/manifest-schema.md](docs/manifest-schema.md)
for the bundle and manifest format.

## Development

The test suite runs on any OS: the Windows layer sits behind one
`Environment` object that tests build over a fixture profile tree.

```bash
python -m pip install -e ".[dev]"
python -m pytest
WINMIGRATE_ALLOW_NON_WINDOWS=1 python -m winmigrate scan --profile-root ./fixture-profile
```

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
