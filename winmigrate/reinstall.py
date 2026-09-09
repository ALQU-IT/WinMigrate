"""Reinstalling software on the target machine, with the user in the loop.

Restore writes everything needed to reinstall -- winget's own export, an ODT
configuration, and a list of what neither can handle -- into one directory. It
does not install anything by itself.

That split is deliberate. Putting files back where they were is what the user
asked for; installing software changes the machine in ways that are slow to
undo, can require elevation, and may pull different versions than were on the
source. So it is a separate, explicit step (``winmigrate reinstall``), which
still shows what it will do and asks first.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .odt import generate_configuration, run_setup
from .scan.office import REACTIVATION_STEPS, OfficeInstallation, read_configuration
from .util import process

log = logging.getLogger(__name__)

ARTIFACTS_DIRECTORY = "WinMigrate-Reinstall"
WINGET_IMPORT_FILE = "winget-import.json"
OFFICE_CONFIG_FILE = "office-configuration.xml"
MANUAL_LIST_FILE = "reinstall-by-hand.md"

WINGET_IMPORT_TIMEOUT = 7200


@dataclass(slots=True)
class Artifacts:
    """The files restore wrote for the reinstall step."""

    directory: Path
    winget_import: Path | None = None
    office_configuration: Path | None = None
    manual_list: Path | None = None
    reinstallable_count: int = 0
    manual_count: int = 0
    component_count: int = 0
    office: OfficeInstallation | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def anything_to_do(self) -> bool:
        return bool(self.winget_import or self.office_configuration or self.manual_count)


def write_artifacts(manifest: dict[str, Any], destination: Path) -> Artifacts:
    """Write the reinstall inputs from a restored manifest. Installs nothing."""
    directory = destination / ARTIFACTS_DIRECTORY
    artifacts = Artifacts(directory=directory)

    software = _record_for(manifest, "software")
    office_record = _record_for(manifest, "office")
    if software is None and office_record is None:
        return artifacts

    directory.mkdir(parents=True, exist_ok=True)

    if software:
        export = software.get("winget_export")
        applications = software.get("applications", [])
        artifacts.reinstallable_count = sum(1 for app in applications if app.get("winget_id"))
        # Runtimes and drivers arrive with whatever needs them; listing them as
        # chores would bury the handful that genuinely need a person.
        manual = [
            app
            for app in applications
            if not app.get("winget_id") and not app.get("component")
        ]
        components = [
            app for app in applications if not app.get("winget_id") and app.get("component")
        ]
        artifacts.manual_count = len(manual)
        artifacts.component_count = len(components)
        if export:
            path = directory / WINGET_IMPORT_FILE
            path.write_text(json.dumps(export, indent=2), encoding="utf-8")
            artifacts.winget_import = path
        if manual:
            path = directory / MANUAL_LIST_FILE
            path.write_text(
                _manual_markdown(manual, artifacts.reinstallable_count, components),
                encoding="utf-8",
            )
            artifacts.manual_list = path

    if office_record and office_record.get("product_ids"):
        installation = _installation_from_record(office_record)
        artifacts.office = installation
        try:
            path = directory / OFFICE_CONFIG_FILE
            path.write_text(generate_configuration(installation), encoding="utf-8")
            artifacts.office_configuration = path
        except ValueError as exc:  # pragma: no cover -- guarded by product_ids above
            artifacts.notes.append(f"could not generate an Office configuration: {exc}")

    return artifacts


def _record_for(manifest: dict[str, Any], category: str) -> dict[str, Any] | None:
    for item in manifest.get("items", []):
        if item.get("category") == category and isinstance(item.get("record"), dict):
            return item["record"]
    return None


def _installation_from_record(record: dict[str, Any]) -> OfficeInstallation:
    installation = read_configuration(
        {
            "ProductReleaseIds": ",".join(record.get("product_ids", [])),
            "Platform": record.get("platform", ""),
            "ClientCulture": record.get("client_culture", ""),
        }
    )
    installation.channel = record.get("channel", "")
    installation.version = record.get("version", "")
    return installation


def _manual_markdown(
    applications: list[dict[str, Any]],
    reinstallable: int,
    components: list[dict[str, Any]] | None = None,
) -> str:
    components = components or []
    lines = [
        "# Reinstall by hand",
        "",
        f"winget can reinstall {reinstallable} application(s) on its own "
        "(`winmigrate reinstall --apps`).",
        f"These {len(applications)} it has no package for.",
        "",
        "Their presence here means they were found on the old machine -- not that",
        "you still need them. This is a good moment to decide.",
        "",
        "| Application | Version | Publisher |",
        "| --- | --- | --- |",
    ]
    lines.extend(_table_rows(applications))
    if components:
        lines += [
            "",
            "## Runtimes and drivers",
            "",
            f"{len(components)} further entries are redistributables, runtimes or driver",
            "packages. They are listed for completeness only: whatever needs them",
            "installs them, so there is normally nothing to do here.",
            "",
            "| Component | Version | Publisher |",
            "| --- | --- | --- |",
        ]
        lines.extend(_table_rows(components))
    return "\n".join(lines) + "\n"


def _table_rows(applications: list[dict[str, Any]]) -> list[str]:
    rows = []
    for app in sorted(applications, key=lambda item: str(item.get("name", "")).lower()):
        name = str(app.get("name", "")).replace("|", "\\|")
        version = str(app.get("version", "")).replace("|", "\\|")
        publisher = str(app.get("publisher", "")).replace("|", "\\|")
        rows.append(f"| {name} | {version} | {publisher} |")
    return rows


def office_reactivation_steps(installation: OfficeInstallation | None) -> list[str]:
    if installation is None:
        return []
    return list(REACTIVATION_STEPS.get(installation.activation_type, REACTIVATION_STEPS["unknown"]))


def run_winget_import(import_file: Path, runner=process.run) -> process.CommandResult:
    """Replay winget's export on this machine.

    ``--ignore-unavailable`` keeps one missing package from aborting the rest,
    and ``--accept-package-agreements`` is required for an unattended run --
    the user has already agreed to this step at the prompt.
    """
    return runner(
        [
            "winget",
            "import",
            "-i",
            str(import_file),
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--ignore-unavailable",
            "--disable-interactivity",
        ],
        timeout=WINGET_IMPORT_TIMEOUT,
    )


def run_office_install(
    setup_executable: Path, configuration: Path, runner=process.run
) -> process.CommandResult:
    return run_setup(setup_executable, configuration, runner=runner)
