"""Detect cloud-sync roots so their contents are not captured twice.

Content that already lives in a sync provider's folder reaches the new machine
by signing into that provider -- copying it into the bundle would double the
archive for no benefit. Detection is configuration-read only: registry values
and the clients' own config files. Nothing is read from the providers' APIs and
no network call is made.
"""

from __future__ import annotations

import configparser
import io
import logging
import os
import time
from pathlib import Path

from ..models import SyncRoot
from ..platform_win import HKCU, Environment
from ..util import paths as pathutil

log = logging.getLogger(__name__)


def _say(progress, message: str) -> None:
    """Report progress when there is somewhere to report it."""
    if progress is not None:
        progress(message)
    log.debug("%s", message)

ONEDRIVE_ACCOUNTS_KEY = r"Software\Microsoft\OneDrive\Accounts"

#: Environment variables OneDrive sets for the signed-in account(s).
ONEDRIVE_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def detect(env: Environment) -> list[SyncRoot]:
    """Return every sync root we can identify for this user."""
    roots: list[SyncRoot] = []
    roots.extend(_detect_onedrive(env))
    roots.extend(_detect_nextcloud(env))
    return _dedupe(roots)


def _dedupe(roots: list[SyncRoot]) -> list[SyncRoot]:
    seen: dict[str, SyncRoot] = {}
    for root in roots:
        key = pathutil.normalize_key(root.root)
        existing = seen.get(key)
        if existing is None:
            seen[key] = root
        elif existing.account_hint is None and root.account_hint:
            seen[key] = root
    return list(seen.values())


# --- OneDrive --------------------------------------------------------------
def _detect_onedrive(env: Environment) -> list[SyncRoot]:
    roots: list[SyncRoot] = []
    for account in env.registry_subkeys(HKCU, ONEDRIVE_ACCOUNTS_KEY):
        values = env.read_registry_key(HKCU, f"{ONEDRIVE_ACCOUNTS_KEY}\\{account}") or {}
        folder = values.get("UserFolder")
        if not isinstance(folder, str) or not folder.strip():
            continue
        path = env.resolve_path(folder)
        roots.append(
            SyncRoot(
                provider="onedrive",
                root=str(path),
                label=path.name or account,
                account_hint=_onedrive_account_hint(account, values),
            )
        )
    for name in ONEDRIVE_ENV_VARS:
        value = env.env_var(name)
        if value:
            path = env.resolve_path(value)
            roots.append(SyncRoot(provider="onedrive", root=str(path), label=path.name))
    default = env.profile_root / "OneDrive"
    if default.is_dir():
        roots.append(SyncRoot(provider="onedrive", root=str(default), label="OneDrive"))
    return roots


def _onedrive_account_hint(account: str, values: dict) -> str | None:
    """A short, non-secret hint about which account owns this folder."""
    email = values.get("UserEmail")
    kind = "personal" if account.lower().startswith("personal") else "work or school"
    if isinstance(email, str) and email:
        return f"{kind}: {email}"
    return kind


# --- Nextcloud -------------------------------------------------------------
NEXTCLOUD_CONFIG_RELATIVE = ("Nextcloud", "nextcloud.cfg")


def _detect_nextcloud(env: Environment) -> list[SyncRoot]:
    config_path = env.appdata_roaming().joinpath(*NEXTCLOUD_CONFIG_RELATIVE)
    if not config_path.is_file():
        return []
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [
        SyncRoot(
            provider=root.provider,
            root=str(env.resolve_path(root.root)),
            label=root.label,
            account_hint=root.account_hint,
        )
        for root in parse_nextcloud_config(text)
    ]


def parse_nextcloud_config(text: str) -> list[SyncRoot]:
    """Parse ``nextcloud.cfg`` for locally synced folders.

    The client stores folders as flattened keys inside ``[Accounts]``, e.g.
    ``0\\Folders\\1\\localPath=C:/Users/x/Nextcloud/``. Split out here so it can
    be unit tested without a Nextcloud install.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # keep key case; the keys carry indices
    try:
        parser.read_file(io.StringIO(text))
    except configparser.Error:
        return []

    urls: dict[str, str] = {}
    users: dict[str, str] = {}
    folders: list[tuple[str, str]] = []
    for section in parser.sections():
        for key, value in parser.items(section):
            parts = key.split("\\")
            lowered = parts[-1].lower()
            index = parts[0] if parts else "0"
            if lowered == "localpath" and value.strip():
                folders.append((index, value.strip()))
            elif lowered == "url":
                urls[index] = value.strip()
            elif lowered in {"dav_user", "user"}:
                users[index] = value.strip()

    roots: list[SyncRoot] = []
    for index, local_path in folders:
        path = Path(local_path.replace("/", "\\").rstrip("\\")) if "\\" in local_path or ":" in local_path else Path(local_path.rstrip("/"))
        hint_parts = [part for part in (users.get(index), urls.get(index)) if part]
        roots.append(
            SyncRoot(
                provider="nextcloud",
                root=str(path),
                label=path.name or "Nextcloud",
                account_hint=" @ ".join(hint_parts) or None,
            )
        )
    return roots


# --- volume ----------------------------------------------------------------
#: How long counting one sync root may take before the answer becomes "at
#: least this much".
#:
#: The figure exists only to tell the user roughly how much they are not backing
#: up. It is worth a few seconds and it is not worth ten minutes -- which is
#: what a corporate OneDrive costs, because every file is a cloud placeholder
#: and every stat goes through the Cloud Files driver rather than the disk.
MEASURE_BUDGET_SECONDS = 8.0

#: How often to say something while counting. Long enough not to flicker, short
#: enough that the line keeps moving.
_REPORT_EVERY = 2000

#: How often to look at the clock. Deliberately much finer than the reporting
#: interval: the budget exists for the case where each stat is slow, and on a
#: cloud placeholder a stat can take tens of milliseconds. Checking every 2000
#: files would mean a minute and a half before the first look at a clock set
#: for eight seconds.
_CHECK_CLOCK_EVERY = 64


def measure(
    roots: list[SyncRoot],
    measure_contents: bool = True,
    progress=None,
    budget_seconds: float = MEASURE_BUDGET_SECONDS,
) -> None:
    """Record how much data each sync root holds, within a time budget.

    Measured once, here, rather than accumulated during the profile walk: a
    sync root may be redirected into (Known Folder Move) *and* sit in the
    profile root, and counting it in both places would inflate the "skipped"
    figure the preview shows.

    Counting stops at ``budget_seconds`` per root and the root is marked as not
    fully measured. A backup tool should not spend ten minutes establishing the
    size of something it has already decided not to copy, and it certainly
    should not do it in silence -- which read, from the outside, as the program
    having hung before it printed anything at all.
    """
    for root in roots:
        if not measure_contents:
            continue
        label = root.label or root.provider
        _say(progress, f"Measuring {label}")
        total = 0
        files = 0
        complete = True
        deadline = time.monotonic() + budget_seconds
        stack = [Path(root.root)]
        seen = 0
        while stack:
            # Checked here too: a directory can be slow to open before a single
            # file in it has been looked at.
            if time.monotonic() > deadline:
                complete = False
                break
            current = stack.pop()
            try:
                entries = list(os.scandir(pathutil.extended(current)))
            except OSError:
                continue
            for entry in entries:
                seen += 1
                if seen % _CHECK_CLOCK_EVERY == 0 and time.monotonic() > deadline:
                    complete = False
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(current / entry.name)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                        if files % _REPORT_EVERY == 0:
                            _say(progress, f"Measuring {label} — {files:,} files")
                except OSError:
                    continue
            if not complete:
                break
        if not complete:
            log.info(
                "stopped measuring %s after %.0fs at %d files; reporting a floor",
                root.root, budget_seconds, files,
            )
        root.bytes_skipped = total
        root.files_skipped = files
        root.measured_fully = complete


# --- containment -----------------------------------------------------------
def find_root_for(path: str, roots: list[SyncRoot]) -> SyncRoot | None:
    """Return the sync root containing ``path``, preferring the deepest match."""
    best: SyncRoot | None = None
    for root in roots:
        if pathutil.is_within(path, root.root):
            if best is None or len(root.root) > len(best.root):
                best = root
    return best
