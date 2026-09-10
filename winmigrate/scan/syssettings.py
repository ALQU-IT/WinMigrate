"""System settings that migrate as records or small file trees.

None of these is large, and most are not files at all -- they are registry
state the new machine re-applies (environment variables, mapped drives,
printers) or small folders worth carrying (per-user fonts, Outlook signatures
and any local .pst). Each is captured the way it actually moves:

* **Environment variables** and **mapped drives** and **printers** are recorded
  from the registry and re-established on the new machine; nothing is copied.
* **Fonts** the user installed for themselves are copied; the system fonts are
  reported, not carried.
* **Outlook** signatures are copied, and a local ``.pst`` (a real mail archive)
  is captured while a ``.ost`` (a re-downloadable cache) is not.

Environment variables can hold tokens, so that record stays in the encrypted
manifest only; the rest are non-sensitive and kept in the public sidecar for the
report.
"""

from __future__ import annotations

import logging

from ..models import (
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Severity,
)
from ..platform_win import HKCU, Environment

log = logging.getLogger(__name__)

ENVIRONMENT_KEY = r"Environment"
NETWORK_KEY = r"Network"
PRINTER_CONNECTIONS_KEY = r"Printers\Connections"
WINDOWS_DEVICE_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Windows"


def scan_system_settings(env: Environment):
    """Return the system-settings items and follow-ups for this profile."""
    items: list[Item] = []
    followups: list[Followup] = []

    for builder in (_env_vars, _mapped_drives, _printers):
        item = builder(env)
        if item is not None:
            items.append(item)

    fonts = _fonts(env)
    if fonts is not None:
        items.append(fonts)

    items.extend(_outlook(env))

    for item in items:
        if item.restore and item.restore.strategy is RestoreStrategy.GUIDED:
            followups.append(_guided_followup(item))
    return items, followups


# --- environment variables -------------------------------------------------
def _env_vars(env: Environment) -> Item | None:
    values = env.read_registry_key(HKCU, ENVIRONMENT_KEY)
    if not values:
        return None
    record = {name: str(value) for name, value in sorted(values.items())}
    return Item(
        id="settings:env_vars",
        category=Category.ENV_VARS,
        kind=Kind.RECORD,
        title=f"User environment variables ({len(record)})",
        record={"variables": record},
        # Kept out of the public sidecar: a user PATH or custom variable can hold
        # a token. It rides in the encrypted manifest instead.
        record_public=False,
        restore=RestoreSpec(
            target="HKCU\\Environment",
            strategy=RestoreStrategy.GUIDED,
            notes=["Set each with setx, or via System -> Environment Variables."],
        ),
        notes=[Note(Severity.INFO, "Re-applied on the new machine; review before setting PATH.")],
    )


# --- mapped drives ---------------------------------------------------------
def _mapped_drives(env: Environment) -> Item | None:
    drives: dict[str, str] = {}
    for letter in env.registry_subkeys(HKCU, NETWORK_KEY):
        values = env.read_registry_key(HKCU, f"{NETWORK_KEY}\\{letter}") or {}
        remote = values.get("RemotePath")
        if isinstance(remote, str) and remote:
            drives[letter.upper()] = remote
    if not drives:
        return None
    return Item(
        id="settings:mapped_drives",
        category=Category.MAPPED_DRIVES,
        kind=Kind.RECORD,
        title=f"Mapped network drives ({len(drives)})",
        record={"drives": drives},
        record_public=True,
        restore=RestoreSpec(
            target="net use",
            strategy=RestoreStrategy.GUIDED,
            notes=["Reconnect each with: net use <letter>: <path>"],
        ),
    )


# --- printers --------------------------------------------------------------
def _printers(env: Environment) -> Item | None:
    connections: list[str] = []
    for name in env.registry_subkeys(HKCU, PRINTER_CONNECTIONS_KEY):
        # Connection subkeys encode the UNC path as ",,server,printer".
        connections.append(name.replace(",", "\\").lstrip("\\"))
    default = env.read_registry_value(HKCU, WINDOWS_DEVICE_KEY, "Device")
    default_name = default.split(",", 1)[0] if isinstance(default, str) else None
    if not connections and not default_name:
        return None
    return Item(
        id="settings:printers",
        category=Category.PRINTERS,
        kind=Kind.RECORD,
        title=f"Printers ({len(connections)})",
        record={"connections": sorted(connections), "default": default_name},
        record_public=True,
        restore=RestoreSpec(
            target="Add printer",
            strategy=RestoreStrategy.GUIDED,
            notes=["Re-add network printers by their UNC path; install drivers as needed."],
        ),
    )


# --- fonts -----------------------------------------------------------------
def _fonts(env: Environment) -> Item | None:
    """Per-user installed fonts, copied. System fonts are the OS's, not carried."""
    fonts_dir = env.appdata_local() / "Microsoft" / "Windows" / "Fonts"
    if not fonts_dir.is_dir():
        return None
    return Item(
        id="settings:fonts",
        category=Category.FONTS,
        kind=Kind.TREE,
        title="Per-user fonts",
        source_path=str(fonts_dir),
        archive_path="data/fonts",
        restore=RestoreSpec(
            target="%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts",
            strategy=RestoreStrategy.MERGE,
            notes=["Per-user fonts install for you without admin rights."],
        ),
        notes=[Note(Severity.INFO, "Fonts you installed; the system fonts come with Windows.")],
    )


# --- outlook ---------------------------------------------------------------
def _outlook(env: Environment) -> list[Item]:
    items: list[Item] = []
    signatures = env.appdata_roaming() / "Microsoft" / "Signatures"
    if signatures.is_dir() and any(signatures.iterdir()):
        items.append(
            Item(
                id="settings:outlook_signatures",
                category=Category.OUTLOOK,
                kind=Kind.TREE,
                title="Outlook signatures",
                source_path=str(signatures),
                archive_path="data/outlook/signatures",
                restore=RestoreSpec(
                    target="%APPDATA%\\Microsoft\\Signatures",
                    strategy=RestoreStrategy.MERGE,
                ),
            )
        )
    items.extend(_outlook_pst(env))
    return items


def _outlook_pst(env: Environment) -> list[Item]:
    """Local .pst archives (real mail data). .ost is a cache and is skipped."""
    outlook_dir = env.appdata_local() / "Microsoft" / "Outlook"
    if not outlook_dir.is_dir():
        return []
    items: list[Item] = []
    try:
        entries = sorted(outlook_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() == ".pst":
            items.append(
                Item(
                    id=f"settings:outlook_pst:{entry.stem.lower()}",
                    category=Category.OUTLOOK,
                    kind=Kind.FILE,
                    title=f"Outlook data file — {entry.name}",
                    source_path=str(entry),
                    archive_path=f"data/outlook/{entry.name}",
                    restore=RestoreSpec(
                        target=f"%LOCALAPPDATA%\\Microsoft\\Outlook\\{entry.name}",
                        strategy=RestoreStrategy.REPLACE,
                        notes=["Re-attach in Outlook: File -> Open -> Open Outlook Data File."],
                    ),
                    notes=[Note(Severity.INFO, "A local .pst archive; .ost caches are skipped.")],
                )
            )
    return items


def _guided_followup(item: Item) -> Followup:
    steps = list(item.restore.notes) if item.restore else []
    return Followup(
        id=f"{item.id}:guided",
        title=f"Re-apply: {item.title}",
        why=f"{item.title} is recorded in the bundle and re-applied by hand on the new machine.",
        steps=steps or ["See the restored record."],
        category=item.category,
    )
