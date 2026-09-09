"""Developer and credential configuration: the profile's most sensitive files.

These are the dotfiles and small config trees that make a development machine
itself -- ``.ssh`` keys, cloud credentials, ``.gitconfig``, package-manager
tokens -- and several of them are literally secrets. Every item this module
produces is marked :class:`~winmigrate.models.Sensitivity.SECRET`, which means:

* it is written only into the encrypted payload, never the plaintext sidecar,
  which lists it as a redacted stub (id, size, count -- no path, no contents);
* ``--files-only`` drops it entirely, for a lower-sensitivity migration;
* it never reaches the log.

Nothing here reads a value out of a file. The point is to carry the files
across intact, not to interpret them -- so ``.aws/credentials`` is copied, never
parsed, and no key, token or passphrase is ever surfaced in the manifest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..models import (
    Category,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Sensitivity,
    Severity,
    SkipReason,
)
from ..platform_win import Environment

log = logging.getLogger(__name__)

SECRETS_ARCHIVE_PREFIX = "secrets"


@dataclass(frozen=True, slots=True)
class DevTarget:
    """A dev-config location, relative to the profile root."""

    slot: str                 # stable item-id suffix
    relative: str             # path under the profile, POSIX form
    kind: Kind                # TREE or FILE
    title: str
    secret: bool              # true credential material vs. mere preferences
    why: str                  # one line for the report


#: The locations WinMigrate knows about. Order is report order. ``secret`` marks
#: genuine credential material; the rest are preferences kept in the encrypted
#: payload anyway, because a dev's dotfiles are nobody else's business.
DEV_TARGETS: tuple[DevTarget, ...] = (
    DevTarget("ssh", ".ssh", Kind.TREE, "SSH keys and config", True,
              "private keys and known hosts"),
    DevTarget("aws", ".aws", Kind.TREE, "AWS credentials and config", True,
              "cloud access keys"),
    DevTarget("azure", ".azure", Kind.TREE, "Azure CLI profile", True,
              "cloud tokens and subscriptions"),
    DevTarget("gcloud", ".config/gcloud", Kind.TREE, "Google Cloud SDK config", True,
              "cloud credentials"),
    DevTarget("kube", ".kube", Kind.TREE, "Kubernetes config", True,
              "cluster credentials"),
    DevTarget("docker", ".docker/config.json", Kind.FILE, "Docker config", True,
              "registry auth"),
    DevTarget("gnupg", ".gnupg", Kind.TREE, "GnuPG keyring", True,
              "private keys"),
    DevTarget("npmrc", ".npmrc", Kind.FILE, "npm config", True,
              "may contain a registry token"),
    DevTarget("pypirc", ".pypirc", Kind.FILE, "PyPI upload config", True,
              "may contain an upload token"),
    DevTarget("netrc", ".netrc", Kind.FILE, "netrc credentials", True,
              "host logins"),
    DevTarget("git_credentials", ".git-credentials", Kind.FILE, "git stored credentials", True,
              "stored git logins"),
    DevTarget("gitconfig", ".gitconfig", Kind.FILE, "Git config", False,
              "identity and aliases"),
    DevTarget("gitignore_global", ".gitignore_global", Kind.FILE, "global gitignore", False, ""),
    DevTarget("wslconfig", ".wslconfig", Kind.FILE, "WSL global config", False, ""),
    DevTarget("condarc", ".condarc", Kind.FILE, "conda config", False, ""),
    DevTarget("gitconfig_dir", ".config/git", Kind.TREE, "Git config directory", False, ""),
)


def scan_dev_config(env: Environment, files_only: bool = False):
    """Return the dev-config items present in this profile, plus any notes.

    ``files_only`` produces stubs marked skipped rather than capturing anything,
    so the report still shows what *would* have been taken.
    """
    items: list[Item] = []
    notes: list[Note] = []
    secret_present = False

    for target in DEV_TARGETS:
        source = env.profile_root / Path(target.relative)
        exists = source.is_dir() if target.kind is Kind.TREE else source.is_file()
        if not exists:
            continue
        if target.secret:
            secret_present = True
        items.append(_item_for(target, source, files_only))

    wsl = _wsl_item(env)
    if wsl is not None:
        items.append(wsl)

    if files_only and secret_present:
        notes.append(
            Note(
                Severity.INFO,
                "files-only mode: developer credentials were found but not captured",
                "run without --files-only to include them (encrypted-only, as always)",
            )
        )
    return items, notes


def _item_for(target: DevTarget, source: Path, files_only: bool) -> Item:
    archive = f"{SECRETS_ARCHIVE_PREFIX}/{target.relative}"
    restore_target = "%USERPROFILE%\\" + target.relative.replace("/", "\\")
    item = Item(
        id=f"dev:{target.slot}",
        category=Category.DEV_CONFIG,
        kind=target.kind,
        title=target.title,
        source_path=str(source),
        archive_path=archive,
        sensitivity=Sensitivity.SECRET if target.secret else Sensitivity.NORMAL,
        restore=RestoreSpec(target=restore_target, strategy=RestoreStrategy.MERGE),
    )
    if target.why:
        item.notes.append(Note(Severity.INFO, target.why))
    if files_only and target.secret:
        item.action = _skip_files_only(item)
    return item


def _skip_files_only(item: Item):
    from ..models import Action  # noqa: PLC0415 -- avoid a top-level cycle

    item.skip_reason = SkipReason.FILES_ONLY_MODE
    return Action.SKIP


def _wsl_item(env: Environment) -> Item | None:
    """Record the installed WSL distributions, for the report only.

    WSL distros are whole virtual disks registered under the user's registry;
    they are not copied. What migrates is the *list*, so the user knows which to
    reinstall with ``wsl --install -d <name>``. Read from the registry, so it
    works against a fixture.
    """
    from ..platform_win import HKCU  # noqa: PLC0415

    lxss = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
    names: list[str] = []
    for subkey in env.registry_subkeys(HKCU, lxss):
        values = env.read_registry_key(HKCU, f"{lxss}\\{subkey}") or {}
        distro = values.get("DistributionName")
        if isinstance(distro, str) and distro.strip():
            names.append(distro.strip())
    if not names:
        return None
    return Item(
        id="dev:wsl",
        category=Category.DEV_CONFIG,
        kind=Kind.REPORT,
        title=f"WSL distributions ({len(names)})",
        record={"distributions": sorted(names)},
        record_public=True,
        restore=RestoreSpec(
            target="wsl --install",
            strategy=RestoreStrategy.MANUAL,
            notes=["Reinstall each with: wsl --install -d <name>"],
        ),
        notes=[
            Note(
                Severity.INFO,
                "WSL disks are not copied; reinstall the distributions and restore "
                "their data from your own backups.",
            )
        ],
    )
