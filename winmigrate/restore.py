"""The restore stage: verify a bundle, put the files back, and say what is left.

Restore is the only stage that writes into a user profile, so it is the one that
has to be most careful:

* the bundle's ciphertext digest is checked **before** the passphrase is asked
  for, so transfer damage is caught without anyone typing a secret;
* every chunk is authenticated as it is read, so a truncated or altered bundle
  fails loudly rather than restoring a plausible-looking subset;
* each file is hashed as it is written and checked against the manifest, which
  catches corruption that happened on the source side before encryption;
* an interrupted restore can be re-run: files already present and matching are
  skipped, so progress is never thrown away.

What restore deliberately will **not** do is complete anything the OS gates
behind human authentication. Those become the follow-up list, reproduced from
the manifest, for the user to work through.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import bundle as bundle_mod
from . import keepawake
from . import manifest as manifest_mod
from .errors import IntegrityError, WinMigrateError
from .models import Followup, Note, Severity
from .util import humanize, paths as pathutil

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], None]

COPY_CHUNK = 1024 * 1024

#: Win32 device names. Reserved in every directory, with or without a suffix.
RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


class RestoreError(WinMigrateError):
    """Restore could not proceed safely."""


@dataclass(slots=True)
class RestoreOptions:
    bundle: Path
    passphrase: str
    destination: Path | None = None   # defaults to the current user's profile
    dry_run: bool = False
    overwrite: bool = False           # replace files that differ, instead of keeping them
    items: tuple[str, ...] = ()       # restore only these item ids
    #: Re-apply the settings that need no identity -- Wi-Fi, network printers,
    #: mapped drives, environment variables. On by default: re-adding eleven
    #: printers by hand is not consent, it is tedium.
    apply_settings: bool = True
    #: Check the destination has room before writing anything.
    space_check: bool = True


@dataclass(slots=True)
class RestoreReport:
    """What restore did, and what the user still has to do."""

    bundle: Path
    destination: Path | None = None
    dry_run: bool = False
    restored_files: int = 0
    restored_bytes: int = 0
    skipped_existing: int = 0
    kept_existing: int = 0
    digest_mismatches: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    followups: list[Followup] = field(default_factory=list)
    artifacts: object | None = None    # reinstall.Artifacts, when there was software
    #: apply.Result for each setting re-applied, skipped or failed.
    applied: list = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    duration_seconds: float = 0.0
    manifest: dict | None = None
    verified: bool = False

    @property
    def ok(self) -> bool:
        return not self.digest_mismatches and not self.failures


def inspect(bundle_path: os.PathLike[str] | str) -> dict:
    """Read a bundle's plaintext header. No passphrase required."""
    header, _digest, handle = bundle_mod.read_header(bundle_path)
    handle.close()
    return header


def load_sidecar(bundle_path: Path) -> dict | None:
    """Read the public sidecar manifest beside a bundle, if there is one."""
    import json  # noqa: PLC0415

    sidecar = bundle_path.with_suffix(".manifest.json")
    if not sidecar.is_file():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_sidecar(bundle_path: Path) -> tuple[bool, str | None]:
    """Check the bundle against its sidecar manifest, if one is beside it.

    Returns ``(checked, error)``. A missing sidecar is not an error -- the AEAD
    tags still authenticate everything -- but its digest lets damage be caught
    before a passphrase is entered.
    """
    sidecar = bundle_path.with_suffix(".manifest.json")
    if not sidecar.is_file():
        return False, None
    import json  # noqa: PLC0415

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        expected = data.get("bundle", {}).get("ciphertext", {}).get("sha256")
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not read {sidecar.name}: {exc}"
    if not expected:
        return False, None
    try:
        bundle_mod.verify_ciphertext(bundle_path, expected)
    except IntegrityError as exc:
        return True, str(exc)
    return True, None


def restore(options: RestoreOptions, progress: ProgressCallback | None = None) -> RestoreReport:
    """Restore a bundle onto this machine."""
    started = time.monotonic()
    bundle_path = Path(os.fspath(options.bundle))
    if not bundle_path.is_file():
        raise RestoreError(f"bundle not found: {bundle_path}")

    report = RestoreReport(bundle=bundle_path, dry_run=options.dry_run)
    checked, error = verify_sidecar(bundle_path)
    report.verified = checked
    if error:
        raise IntegrityError(error)
    if not checked:
        report.notes.append(
            Note(
                Severity.INFO,
                "no sidecar manifest beside the bundle; integrity rests on the "
                "bundle's own authentication tags",
            )
        )

    destination = Path(os.fspath(options.destination)) if options.destination else _default_profile()
    report.destination = destination
    if not options.dry_run and options.space_check:
        _check_room(bundle_path, destination, options, report)
    written: dict[str, str] = {}
    already_present: dict[str, Path] = {}
    unverifiable: set[str] = set()

    # The authoritative manifest is the last member of the stream, so selecting
    # items by id has to be resolved from the sidecar before extraction starts.
    prefixes = _selected_prefixes(options, bundle_path)

    with keepawake.KeepAwake("restore"), bundle_mod.BundleReader(
        bundle_path, options.passphrase
    ) as reader:
        for info, stream in reader.members():
            if not info.isfile() or stream is None:
                continue
            if info.name == manifest_mod.MANIFEST_ARCHIVE_NAME:
                report.manifest = _load_manifest(stream)
                continue
            if prefixes is not None and not _wanted(info.name, prefixes):
                continue
            _restore_member(
                info, stream, destination, options, report, written,
                already_present, unverifiable, progress,
            )

    if report.manifest is None:
        raise IntegrityError(
            "the bundle contains no manifest: it is incomplete or not a WinMigrate bundle"
        )
    manifest_mod.validate(report.manifest)
    _check_digests(report, written, already_present, unverifiable)
    _collect_followups(report)
    if not options.dry_run:
        _write_reinstall_artifacts(report, destination)
        if options.apply_settings:
            _apply_settings(report, destination, options.items)
            _settle_followups(report)
    report.duration_seconds = time.monotonic() - started
    return report


#: Where the Wi-Fi profiles land, matching the archive prefix capture uses.
WIFI_RESTORE_DIR = "WinMigrate-WiFi"

#: And where the desktop background lands. It stays on disk after the restore
#: rather than being set and deleted: Windows reads the file every time it
#: draws the desktop, so deleting it is deleting the background.
WALLPAPER_RESTORE_DIR = "WinMigrate-Wallpaper"

#: Categories whose files a running program keeps open, and what to call it.
#: A browser is named from the item's own title, which carries it.
HELD_OPEN: dict[str, str] = {
    "browser_profile": "",
    "browser_passwords": "",
    "outlook": "Outlook",
    "notepad": "Notepad",
}


def programs_to_close(manifest: dict[str, Any], wanted: tuple[str, ...] = ()) -> list[str]:
    """Programs whose own working files this restore would write over.

    Restoring a browser profile into a running browser is the one way this
    tool can damage something: the browser holds those files open, rewrites
    them on its own schedule, and half its database arriving underneath it is
    worse than the restore simply failing. The capture side has always said to
    close things for a cleaner copy; the restore side said nothing, and it is
    the side where it matters.

    Read from the plaintext sidecar as happily as from the manifest -- a
    redacted stub keeps its category and title, which is all this needs.
    """
    names: set[str] = set()
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("action") != "capture":
            continue
        if wanted and item.get("id") not in wanted:
            continue
        label = HELD_OPEN.get(str(item.get("category", "")))
        if label is None:
            continue
        if label:
            names.add(label)
            continue
        # A browser: its title is "Google Chrome — Person 1", and the program
        # is the half in front.
        title = str(item.get("title", "")).split("—")[0].strip()
        if title:
            names.add(title)
    return sorted(names)


#: Which record each kind of applied setting comes from, and so which follow-up
#: it answers. The follow-up's id is the record's with ``:guided`` on the end --
#: :func:`winmigrate.scan.syssettings._guided_followup` mints it that way, and a
#: test holds the two conventions together, because a seam that only a string
#: match joins is exactly where this kind of thing rots.
APPLIED_KINDS: dict[str, str] = {
    "settings:env_vars": "env",
    "settings:mapped_drives": "drive",
    "settings:printers": "printer",
}

GUIDED_SUFFIX = ":guided"


def _restored_wallpaper(record: dict[str, Any], destination: Path) -> Path | None:
    """The background image this restore just wrote, if it wrote one.

    The name comes from the record rather than from whatever is in the folder:
    a restore into a destination that already holds an older WinMigrate-Wallpaper
    would otherwise pick up the previous migration's picture.
    """
    name = str(record.get("file_name") or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    candidate = destination / WALLPAPER_RESTORE_DIR / name
    return candidate if candidate.is_file() else None


def _settle_followups(report: RestoreReport) -> None:
    """Take back the instructions the restore has just carried out.

    The follow-up list is written when the *backup* is made, and nothing at
    that point knows what the restore will manage on its own. So a machine
    whose drives had been re-mapped and whose variables had been set two
    seconds earlier was still told, under a heading reading "These need you
    rather than the tool", to go and do both by hand.

    That is worse than saying nothing. It sends someone to redo finished work,
    and in doing so it buries the one line among them that really was left
    undone. A restore report has one job -- to say what happened -- and a list
    that cannot tell "done" from "your turn" is not doing it.
    """
    from .apply import Outcome  # noqa: PLC0415 -- keeps the import off the scan path

    by_kind: dict[str, list] = {}
    for result in report.applied:
        by_kind.setdefault(result.kind, []).append(result)

    kept: list[Followup] = []
    for followup in report.followups:
        if not followup.id.endswith(GUIDED_SUFFIX):
            kept.append(followup)
            continue
        record_id = followup.id[: -len(GUIDED_SUFFIX)]
        results = by_kind.get(APPLIED_KINDS.get(record_id, ""), [])
        if not results:
            # Nothing was even attempted for it: a dry run, --no-apply-settings,
            # a record this restore did not select, or a category the tool does
            # not apply. The instruction stands.
            kept.append(followup)
            continue
        left = [r for r in results if r.outcome is not Outcome.APPLIED]
        if not left:
            log.info("%s was re-applied in full; dropping its follow-up", record_id)
            continue
        kept.append(_remaining_followup(followup, len(results) - len(left), left))
    report.followups = kept


def _remaining_followup(followup: Followup, done: int, left: list) -> Followup:
    """The same follow-up, narrowed to the part the tool could not do.

    Names the setting and never its value. An environment variable can hold a
    token -- that is why its record is encrypted-only -- and a follow-up is
    read off a screen and pasted into chat logs.

    The original by-hand steps come back only when something actually *failed*.
    When the rest were left on purpose -- a user PATH that describes where
    software lived on the old machine -- "set each with setx" is not advice
    that was merely unnecessary, it is advice that breaks the new machine.
    """
    import re  # noqa: PLC0415

    from .apply import Outcome  # noqa: PLC0415

    total = done + len(left)
    base = re.sub(r"\s*\(\d+\)\s*$", "", followup.title)
    failed = [r for r in left if r.outcome is Outcome.FAILED]
    steps = [f"{r.name} -- {r.detail}" if r.detail else r.name for r in left]
    if failed:
        steps.extend(followup.steps)
    return replace(
        followup,
        title=f"{base} -- {len(left)} of {total} still to do",
        why=(
            f"{done} of {total} were re-applied by the restore itself. "
            "These were not, for the reason against each."
            if done
            else "The restore applied none of these. The reason is against each."
        ),
        steps=steps,
    )


def _apply_settings(report: RestoreReport, destination: Path, wanted: tuple[str, ...] = ()) -> None:
    """Re-apply the settings that need no identity.

    Deliberately last: files first, then the settings that point at them. And
    deliberately forgiving -- a printer whose driver is missing is a line in the
    report, not a failed restore. The files are already on disk by this point
    and nothing here can take them away again.

    ``wanted`` is the restore's item selection, if it made one. "Put back just
    my Desktop" is a sentence about one folder, and it used to add eleven
    printers and rewrite the user's environment variables as well, because the
    records were read straight out of the manifest without asking what the
    restore had been asked for.
    """
    from . import apply as apply_mod  # noqa: PLC0415 -- keeps the import off the scan path

    manifest = report.manifest or {}
    records = {
        item.get("id"): item.get("record")
        for item in manifest.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("record"), dict)
        and (not wanted or item.get("id") in wanted)
    }

    try:
        report.applied.extend(apply_mod.apply_wifi(destination / WIFI_RESTORE_DIR))
        if "settings:printers" in records:
            report.applied.extend(apply_mod.apply_printers(records["settings:printers"]))
        if "settings:mapped_drives" in records:
            report.applied.extend(apply_mod.apply_mapped_drives(records["settings:mapped_drives"]))
        if "settings:env_vars" in records:
            report.applied.extend(apply_mod.apply_environment(records["settings:env_vars"]))
        if "settings:personalization" in records:
            report.applied.extend(
                apply_mod.apply_personalization(records["settings:personalization"])
            )
        if "settings:wallpaper" in records:
            report.applied.extend(
                apply_mod.apply_wallpaper(
                    records["settings:wallpaper"],
                    _restored_wallpaper(records["settings:wallpaper"], destination),
                )
            )
    except Exception as exc:  # noqa: BLE001 -- the restore itself already succeeded
        log.warning("could not re-apply settings", exc_info=True)
        report.notes.append(
            Note(
                Severity.WARNING,
                "some settings could not be re-applied automatically",
                f"{type(exc).__name__}: {exc}. The follow-up list has them.",
            )
        )

    # One line per setting, because this is the question people actually ask
    # afterwards -- "why is my drive not mapped?" -- and the report on screen
    # is gone by the time they ask it. Names only: a variable's name says which
    # setting this was, and its value is the part that can hold a token.
    for result in report.applied:
        log.info(
            "re-apply %s %s: %s%s",
            result.kind, result.name, result.outcome.value,
            f" ({result.detail})" if result.detail else "",
        )

    failures = [result for result in report.applied if not result.ok]
    if failures:
        report.notes.append(
            Note(
                Severity.WARNING,
                f"{len(failures)} setting(s) could not be re-applied",
                "They are listed in the report; each can be done by hand.",
            )
        )


def _default_profile() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def _load_manifest(stream) -> dict:
    import json  # noqa: PLC0415

    try:
        return json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"the bundle's manifest is unreadable: {exc}") from exc


def _restore_member(info, stream, destination: Path, options: RestoreOptions,
                    report: RestoreReport, written: dict[str, str],
                    already_present: dict[str, Path], unverifiable: set[str],
                    progress: ProgressCallback | None) -> None:
    target = _target_for(info.name, destination)
    if target is None:
        return

    if options.dry_run:
        report.restored_files += 1
        report.restored_bytes += info.size
        return

    existing = _existing_state(target, info.size)
    if existing == "differs" and not options.overwrite:
        # The user's own version was kept, so it deliberately differs from the
        # bundle and cannot be verified against it.
        unverifiable.add(info.name)
        report.kept_existing += 1
        report.notes.append(
            Note(
                Severity.WARNING,
                f"kept the existing {target.name}",
                f"{target} differs from the bundle; pass --overwrite to replace it",
            )
        )
        return

    temporary = target.with_name(target.name + ".winmigrate-part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        # Written to a temporary name first: an interrupted write must not leave
        # a half-file that the next run would mistake for finished work. When
        # something of the same length is already there, its bytes are read
        # alongside so that "already restored" is something established rather
        # than assumed -- see _CompareAlong.
        compare = _CompareAlong(target) if existing == "same" else None
        with open(pathutil.extended(temporary), "wb") as handle:
            while True:
                block = stream.read(COPY_CHUNK)
                if not block:
                    break
                digest.update(block)
                if compare is not None:
                    compare.feed(block)
                handle.write(block)
        if compare is not None:
            compare.close()
        if compare is not None and compare.identical:
            # Byte for byte what is already on disk. Nothing to write, and the
            # digest is known, so verification need not read the file again.
            temporary.unlink(missing_ok=True)
            report.skipped_existing += 1
            written[info.name] = digest.hexdigest()
            return
        if compare is not None and not options.overwrite:
            # Same size, different contents -- the case a size check calls
            # "already restored". It is the user's own file, so it is kept.
            temporary.unlink(missing_ok=True)
            unverifiable.add(info.name)
            report.kept_existing += 1
            report.notes.append(
                Note(
                    Severity.WARNING,
                    f"kept the existing {target.name}",
                    f"{target} is the same size as the backup's copy but not the same "
                    "file; pass --overwrite to replace it",
                )
            )
            return
        os.replace(pathutil.extended(temporary), pathutil.extended(target))
    except OSError as exc:
        # A part-file left in someone's Documents folder is litter, and the next
        # run would write over it anyway.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover -- nothing more to do about it
            pass
        report.failures.append((str(target), exc.strerror or str(exc)))
        unverifiable.add(info.name)
        log.warning("could not restore %s: %s", target, exc)
        return

    written[info.name] = digest.hexdigest()
    report.restored_files += 1
    report.restored_bytes += info.size
    if progress is not None:
        progress(target.name, info.size)


#: Headroom left free after a restore, matching what capture keeps. Filling a
#: Windows system disk to the last byte does not merely fail the copy: the
#: machine stops being usable while it is happening.
FREE_SPACE_MARGIN = 512 * 1024 * 1024


def _check_room(
    bundle_path: Path, destination: Path, options: RestoreOptions, report: RestoreReport
) -> None:
    """Refuse a restore that cannot fit, before a byte of it is written.

    Capture has always checked this and restore never did, which is the wrong
    way round: a capture that runs out of space wastes an hour, and a restore
    that runs out fills the disk of a machine someone is standing in front of
    -- usually the new one, mid-migration, with the old one already wiped.

    Sizes come from the plaintext sidecar, which carries them even for the
    secret items it redacts. Without a sidecar there is nothing to add up and
    the restore goes ahead: a check that cannot be made is not a reason to
    refuse.
    """
    sidecar = load_sidecar(bundle_path)
    if not sidecar:
        return
    wanted = set(options.items)
    needed = 0
    for item in sidecar.get("items", []):
        if not isinstance(item, dict) or item.get("action") != "capture":
            continue
        if wanted and item.get("id") not in wanted:
            continue
        needed += int(item.get("size_bytes") or 0)
    if needed <= 0:
        return
    target = destination if destination.is_dir() else destination.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError:  # a path this machine cannot measure is not a reason to stop
        return
    if free >= needed + FREE_SPACE_MARGIN:
        return
    raise RestoreError(
        f"not enough free space at {destination}: putting this back needs about "
        f"{humanize.bytes_(needed)} and only {humanize.bytes_(free)} is free. "
        f"Free some space, restore somewhere else, choose fewer items, or pass "
        f"--no-space-check to try anyway."
    )


def _selected_prefixes(options: RestoreOptions, bundle_path: Path) -> list[str] | None:
    """Archive-path prefixes for ``--item``, or ``None`` to restore everything.

    The sidecar is the fast path, but it redacts secret items -- a stub carries
    no ``archive_path`` -- so selecting one from the sidecar alone would find
    nothing. When that happens the authoritative manifest is read first (one
    extra decrypt pass, no files written) and the paths come from there, so any
    item can be restored on its own without weakening the sidecar's redaction.
    """
    if not options.items:
        return None

    sidecar = load_sidecar(bundle_path)
    entries: list[dict[str, Any]] = list(sidecar.get("items", [])) if sidecar else []
    prefixes = _prefixes_from(entries, options.items)
    known = {item.get("id") for item in entries}
    unresolved = [i for i in options.items if i in known and i not in prefixes]

    if sidecar is None or unresolved or not known:
        # Either no sidecar at all, or a selected item is a redacted stub.
        manifest = read_manifest(bundle_path, options.passphrase)
        entries = list(manifest.get("items", []))
        prefixes = _prefixes_from(entries, options.items)
        known = {item.get("id") for item in entries}

    unknown = sorted(set(options.items) - known)
    if unknown:
        raise RestoreError(f"no such item(s) in this bundle: {', '.join(unknown)}")
    if not prefixes:
        raise RestoreError(
            "the selected item(s) are records rather than files, so there is "
            "nothing to restore from them"
        )
    return list(prefixes.values())


def _wanted(name: str, prefixes: list[str]) -> bool:
    """Is this archive member part of one of the selected items?

    A tree's archive path is a directory and its members sit under it; a single
    file's archive path *is* the member name. Matching only ``prefix + "/"``
    therefore let every tree through and silently dropped every file -- the
    password export a user had just gone through their browser to produce
    included. The trailing slash is still what separates a real child from a
    sibling whose name merely starts the same way.
    """
    return any(name == prefix or name.startswith(f"{prefix}/") for prefix in prefixes)


def _prefixes_from(entries: list[dict[str, Any]], wanted: tuple[str, ...]) -> dict[str, str]:
    """``{item id: archive path}`` for the wanted ids that actually have one."""
    return {
        item["id"]: item["archive_path"]
        for item in entries
        if item.get("id") in wanted and item.get("archive_path")
    }


def read_manifest(bundle_path: Path, passphrase: str) -> dict[str, Any]:
    """Stream a bundle just far enough to read its manifest, writing nothing.

    The manifest is the last member of the tar, so this costs a full decrypt
    pass. It is only used when the sidecar cannot answer -- selecting a secret
    item by id -- rather than on every restore.
    """
    log.info("reading the bundle manifest to resolve the selected item(s)")
    with bundle_mod.BundleReader(bundle_path, passphrase) as reader:
        for info, stream in reader.members():
            if info.name == manifest_mod.MANIFEST_ARCHIVE_NAME and stream is not None:
                return _load_manifest(stream)
    raise IntegrityError(
        "the bundle contains no manifest: it is incomplete or not a WinMigrate bundle"
    )


#: The name this had while restore was its only caller. The GUI needs it too:
#: opening a bundle is how a wrong passphrase is caught, before anything is
#: written and before the user is asked what to put back.
_read_manifest_only = read_manifest


def _existing_state(target: Path, size: int) -> str:
    """``missing``, ``same`` (the size matches) or ``differs``.

    Only ever the first question. A file of the same length is a *candidate*
    for having been restored already; whether it actually is gets decided by
    reading it, because two files of one length are not one file. Deciding it
    here, on the size alone, meant a file edited on the new machine without
    changing its length was recorded as already restored, never written even
    with --overwrite, and then reported as a digest mismatch -- the backup
    accused of corruption for a file that had never left it.
    """
    try:
        stat_result = os.stat(pathutil.extended(target))
    except OSError:
        return "missing"
    return "same" if stat_result.st_size == size else "differs"


class _CompareAlong:
    """Reads a file alongside an incoming stream, saying whether they match.

    The bytes are read once either way: a file skipped as already-present is
    hashed from disk during verification, so comparing it now moves that read
    earlier rather than adding one -- and saves it entirely when the answer is
    yes, because the digest is then known.
    """

    def __init__(self, target: Path) -> None:
        self.identical = True
        self._handle = None
        try:
            self._handle = open(pathutil.extended(target), "rb")
        except OSError:  # unreadable: treat it as different and write ours
            self.identical = False

    def feed(self, block: bytes) -> None:
        if not self.identical or self._handle is None:
            return
        try:
            theirs = self._handle.read(len(block))
        except OSError:
            self.identical = False
            return
        if theirs != block:
            self.identical = False

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            # Anything left over means their file is longer than the member,
            # whatever the size said a moment ago.
            if self.identical and self._handle.read(1):
                self.identical = False
        except OSError:
            self.identical = False
        finally:
            self._handle.close()
            self._handle = None


def _target_for(archive_name: str, destination: Path) -> Path | None:
    """Map an archive path to a real one, refusing anything that escapes.

    A bundle is data, not a trusted instruction: a member named ``../../..`` or
    ``C:\\Windows\\System32\\...`` must not be able to write outside the
    destination just because someone handed the user a bundle.
    """
    name = archive_name.replace("\\", "/").lstrip("/")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        log.warning("refusing bundle member with a parent reference: %s", archive_name)
        return None
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "user_files":
        parts = parts[2:]
        if parts and parts[0] == "_other":
            parts = parts[1:]
    elif parts and parts[0] == "data":
        parts = parts[1:]
    elif parts and parts[0] == "secrets":
        # Encrypted-only material (dev config): the member name after "secrets/"
        # is the profile-relative path it came from, e.g. secrets/.ssh/id_rsa.
        # The tar member names live inside the ciphertext, so this placement
        # information is not exposed by the plaintext sidecar.
        parts = parts[1:]
    if not parts:
        return None
    # Every part, not just the first. Windows path joining treats a drive
    # anywhere in the sequence as a fresh start, so "data/foo/D:/evil.exe"
    # joins to "D:evil.exe" -- outside the destination, on another disk --
    # while a check of parts[0] alone sees an innocent "foo".
    if any(":" in part for part in parts):
        log.warning("refusing bundle member with a drive letter: %s", archive_name)
        return None
    # Windows resolves these to devices wherever they appear, so a member named
    # NUL would "restore" into the bit bucket and report success.
    if any(part.split(".", 1)[0].upper() in RESERVED_DEVICE_NAMES for part in parts):
        log.warning("refusing bundle member named after a device: %s", archive_name)
        return None
    target = destination.joinpath(*parts)
    # The checks above enumerate what is known to escape; this one asks the
    # question that actually matters, so a form nobody thought of still fails
    # closed rather than writing somewhere it was never meant to.
    if not pathutil.is_within(target, destination):
        log.warning("refusing bundle member that escapes the destination: %s", archive_name)
        return None
    return target


def _check_digests(
    report: RestoreReport,
    written: dict[str, str],
    already_present: dict[str, Path] | None = None,
    unverifiable: set[str] | None = None,
) -> None:
    """Compare what is now on disk against the digests the manifest recorded.

    The check covers the item's whole file set, not just what this run wrote.
    A resumed restore skips files that are already in place, and comparing a
    partial set against a whole-tree digest would report corruption on a
    perfectly good resume -- so skipped files are hashed from disk instead.

    Files that deliberately differ (the user kept their own version) or that
    failed to write cannot be checked against the bundle. Rather than call that
    a mismatch, the item is reported as partially verified.
    """
    from .util.hashing import hash_file, tree_digest  # noqa: PLC0415

    already_present = already_present or {}
    unverifiable = unverifiable or set()
    manifest = report.manifest or {}

    for item in manifest.get("items", []):
        archive_path = item.get("archive_path")
        recorded = item.get("digest")
        if not archive_path or not recorded:
            continue

        if item.get("kind") == "file":
            # A single-file item's archive name is its path exactly; there is no
            # trailing "/". Compare the plain file digest, not a tree digest.
            if archive_path in unverifiable:
                _note_partial(report, item)
                continue
            actual = written.get(archive_path)
            if actual is None and archive_path in already_present:
                actual = _digest_on_disk(already_present[archive_path], hash_file)
            if actual is not None and actual != recorded:
                report.digest_mismatches.append(item["id"])
                log.error("digest mismatch for item %s", item["id"])
            continue

        prefix = f"{archive_path}/"
        if any(name.startswith(prefix) for name in unverifiable):
            _note_partial(report, item)
            continue

        pairs = [
            (name[len(archive_path) + 1 :], digest)
            for name, digest in written.items()
            if name.startswith(prefix)
        ]
        for name, target in already_present.items():
            if not name.startswith(prefix):
                continue
            digest = _digest_on_disk(target, hash_file)
            if digest is None:
                _note_partial(report, item)
                break
            pairs.append((name[len(archive_path) + 1 :], digest))
        else:
            if not pairs:
                continue
            if tree_digest(pairs) != recorded:
                report.digest_mismatches.append(item["id"])
                log.error("digest mismatch for item %s", item["id"])


def _digest_on_disk(target: Path, hash_file) -> str | None:
    """Hash a file that was already in place, or None if it cannot be read."""
    try:
        return hash_file(target)
    except OSError as exc:
        log.warning("could not verify %s: %s", target, exc)
        return None


def _note_partial(report: RestoreReport, item: dict[str, Any]) -> None:
    report.notes.append(
        Note(
            Severity.INFO,
            f"{item.get('title', item.get('id'))} was only partially verified",
            "some of its files were kept as yours or could not be written, so they "
            "cannot be compared against the bundle",
        )
    )


def _write_reinstall_artifacts(report: RestoreReport, destination: Path) -> None:
    """Write the reinstall inputs. Installs nothing -- that is a separate step."""
    from . import reinstall as reinstall_mod  # noqa: PLC0415 -- avoid import cycle

    try:
        artifacts = reinstall_mod.write_artifacts(report.manifest or {}, destination)
    except Exception as exc:  # noqa: BLE001 -- see below
        # Deliberately broad. By this point every file has been written and
        # verified; the reinstall inputs are a convenience on top. Letting
        # anything thrown here escape would replace the restore report -- and
        # with it the follow-up list, which is the whole point of a restore that
        # stops at what needs a person -- with a traceback.
        log.warning("could not write the reinstall files", exc_info=True)
        report.notes.append(
            Note(
                Severity.WARNING,
                "could not write the reinstall files",
                f"{type(exc).__name__}: {exc}. Everything else was restored; "
                f"run 'winmigrate inspect' to read the software list from the bundle.",
            )
        )
        return
    if artifacts.anything_to_do:
        report.artifacts = artifacts


def _collect_followups(report: RestoreReport) -> None:
    for raw in (report.manifest or {}).get("followups", []):
        report.followups.append(
            Followup(
                id=raw.get("id", ""),
                title=raw.get("title", ""),
                why=raw.get("why", ""),
                steps=list(raw.get("steps", [])),
                required=raw.get("required", True),
            )
        )
