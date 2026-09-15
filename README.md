# WinMigrate

Migrate your Windows user profile — data, settings and installed software — to a
new machine, as a single encrypted, integrity-checked archive.

WinMigrate is **owner-run**: you run it on your own machine, it shows you
everything it is doing, and its only output is a local file you control. It
never uploads anything, and anything Windows deliberately gates behind human
authentication — account sign-ins, licence activation, password import — it
prepares for you and hands over, with instructions. It never impersonates you.

> **Status: phases 1–3.** `scan`, `capture`, `inspect`, `restore` and
> `reinstall` all work: encryption, integrity checking, a space pre-check,
> shadow copies, a resumable restore, an installed-software inventory with
> winget reinstall, and Office detection with a matching ODT configuration.
> Developer credentials travel encrypted-only, games installed through a launcher
> are recognised as such, and browser profiles plus a browser-driven password
> handoff are in, along with Wi-Fi (opt-in), printers, mapped drives, environment
> variables, fonts and Outlook. Polish and an optional GUI are what's left; see
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
winmigrate reinstall <restore-folder>  # show the software reinstall plan
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
| `--exclude-preset NAME` | Apply a named preset, e.g. `device-backups`, `vm-images` (see `winmigrate presets`) |
| `--overwrite` | Replace a backup already at the output path (refused by default) |
| `--log-file PATH` | Write a detailed log |

Restore flags:

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Report what would be restored, write nothing |
| `--overwrite` | Replace existing files that differ (default: keep yours) |
| `--item ID` | Restore only these items, e.g. `--item files:documents` (settings are re-applied only if named too) |
| `-d`, `--destination` | Restore somewhere other than the current profile |
| `--no-apply-settings` | Do not re-add Wi-Fi, printers, mapped drives or environment variables |
| `--no-space-check` | Write even if the destination looks too small |

An interrupted restore can simply be re-run: files already present and matching
are skipped — matching by content, not by size.

Close the browsers and Outlook before restoring. Their profiles are the files
being put back, they rewrite them on their own schedule, and half a database
arriving underneath a running program is worse than a restore that fails: both
the window and the command line name the programs they can see in the backup.

`scan` is read-only: it never opens a file's contents, never writes to the
profile it inspects, and cannot trigger a cloud download.

### What a scan tells you

* how much data would be captured, and from where;
* what would be skipped and **why** — already synced, regenerable build output,
  junk, online-only placeholders, unreadable;
* which cloud-sync folders were found, and which account owns each;
* what you will still have to do yourself once the restore is done.

### Checking a backup

`winmigrate verify BUNDLE` reads a backup back and compares every file against
what the manifest recorded when it was written — the restore's verification
pass without the restore. `capture --verify` does it in the same run, and the
window offers it as a tick box on the page where the backup is named (on by
default). It is what earns the right to wipe the old machine: `inspect` only
checks the file arrived intact, which is a different question.

### The log

Every run writes one. The command line writes it where `--log-file` says; the
window writes it beside the program itself — the USB drive you started it from,
next to the backup — as `winmigrate-<date>-<time>.log`, and shows the name in
the side rail while it runs. It opens with where and when the run started, which
copy of the program it was, and whether it had administrator rights, then
records each phase as it happens: the scan, the shadow copy Windows can spend
minutes creating before the first byte is written, the capture, the restore.

Secrets never reach it. Passphrases stay in the window they were typed into, and
anything a call site marks as secret is dropped before any handler sees it.

## Reinstalling software

Applications are inventoried, not copied: what travels is the list, so the new
machine installs current builds from its own sources. Restore writes the inputs
into `WinMigrate-Reinstall\` and installs nothing by itself.

```powershell
winmigrate reinstall C:\restored\WinMigrate-Reinstall            # plan only
winmigrate reinstall C:\restored\WinMigrate-Reinstall --apps     # run winget import
winmigrate reinstall C:\restored\WinMigrate-Reinstall --office C:\ODT\setup.exe
```

The window does it for you. A restore that has software in it stops on a page
listing every package it would fetch, and installs none of it until you press
the button; winget's output is shown line by line while it runs, Stop takes
effect at the next package, and what is already installed stays installed. The
finished page then says how it went, with the same folder and commands for
anything left -- a restore that installed ninety-seven applications without
showing them first would be a different tool.

Anything winget has no package for is listed in `reinstall-by-hand.md` rather
than quietly dropped. Office gets a `configuration.xml` reproducing the edition,
bitness, language and channel that were detected — you supply `setup.exe` from
Microsoft's Office Deployment Tool.

## What it will not do

Your user PATH is merged rather than copied or dropped: this machine's entries
stay, in order and first, the old machine's join them where the folder actually
exists here, and the ones pointing at software that did not travel are named in
the report. Copying it wholesale would break every tool that is not in the same
place on the new machine; leaving it out means re-adding from memory the entries
you put there on purpose.

WinMigrate does **not** decrypt browsers' saved-password stores, and will not:
that routine is an infostealer payload regardless of who runs it. Instead it
detects sign-in and sync state from configuration files only, and either tells
you the passwords will sync down when you sign in, or walks you through the
browser's own export and re-import — on its own page in the window, and with
`--passwords BROWSER=CSV` or the interactive prompt on the command line. Per
browser *profile*, because that is how browsers keep passwords: a Brave with a
work profile and a personal one is offered two exports, opened in the right
profile (`--profile-directory`), and each lands in the bundle under its own
name. The button opens that profile at the browser's settings rather than at
the password page itself: Chromium ignores an internal address handed to it by
another program, and that restriction is a good one — so the address is put on
your clipboard for a single paste, and the window says which of the two
happened instead of naming a page that is not on screen. What the
browser writes is plaintext, so it is staged as encrypted-only material: inside
the bundle it is encrypted, in the plaintext sidecar it is a redacted stub, on
restore it lands in `WinMigrate-Passwords\` with an import-then-delete
instruction, and the window offers to delete the export from this machine once
the bundle holds it. It likewise detects and reinstalls your
Office edition without ever extracting a product key: the generated
configuration contains no `PIDKEY`, and the only licence detail recorded is the
last five characters that `ospp.vbs` prints itself — enough for you to recognise
which key you need, useless to anyone else.

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
