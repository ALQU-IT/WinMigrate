"""Named exclusion presets for the large, optional things a profile accumulates.

A real profile carries things that are genuinely the user's but that most
migrations do not want to haul across a network: a 100 GiB folder of iPhone
backups, a stack of virtual-machine disks. These are not junk -- excluding them
by default would be wrong -- so they stay in unless the user names a preset that
leaves them out.

A preset is just a named bundle of exclusion patterns, applied exactly like a
hand-typed ``--exclude``. The patterns follow the same rule as everywhere else:
no ``/`` matches a path segment anywhere, a ``/`` matches the profile-relative
path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Preset:
    """A named group of exclusion patterns."""

    name: str
    summary: str
    patterns: tuple[str, ...]


EXCLUSION_PRESETS: dict[str, Preset] = {
    "device-backups": Preset(
        name="device-backups",
        summary="iPhone/iPad backups (Apple MobileSync) -- large and re-creatable by re-syncing",
        patterns=(
            "MobileSync",                     # the backup dir wherever Apple put it
            "Apple/MobileSync",               # Apple Devices app (~/Apple/MobileSync/Backup)
            "Apple Computer/MobileSync",      # older iTunes location under AppData/Roaming
            "MobileSync/Backup",
        ),
    ),
    "vm-images": Preset(
        name="vm-images",
        summary="Virtual-machine folders and disk images (.vhdx/.vmdk/.vdi and the like)",
        patterns=(
            "VirtualBox VMs",
            "Virtual Machines",               # VMware / Hyper-V default folder
            "*.vhd",
            "*.vhdx",
            "*.vmdk",
            "*.vdi",
            "*.hdd",
            "*.qcow2",
            "*.avhdx",
        ),
    ),
    "local-media-caches": Preset(
        name="local-media-caches",
        summary="Spotify/streaming offline caches -- re-download on the new machine",
        patterns=(
            "Spotify/Storage",
            "Spotify/Data",
            "PersistentCache",
        ),
    ),
}


def list_presets() -> list[Preset]:
    """All presets, in a stable order for display."""
    return [EXCLUSION_PRESETS[name] for name in sorted(EXCLUSION_PRESETS)]


class UnknownPreset(KeyError):
    """A preset name that does not exist."""


def resolve(names: list[str]) -> tuple[str, ...]:
    """Flatten the named presets into one tuple of exclusion patterns.

    Raises :class:`UnknownPreset` (with the offending name) for a name that is
    not a preset, so the CLI can report it rather than silently doing nothing.
    """
    patterns: list[str] = []
    for name in names:
        preset = EXCLUSION_PRESETS.get(name.strip().lower())
        if preset is None:
            raise UnknownPreset(name)
        patterns.extend(preset.patterns)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            ordered.append(pattern)
    return tuple(ordered)
