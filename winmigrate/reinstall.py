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
    launcher_count: int = 0
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
        # A manifest is data. It has been authenticated, so it is not arbitrary,
        # but it may come from a different version of this tool -- or from
        # someone who wrote their own -- and one odd field here must not cost
        # the user the restore report, which is where the follow-up list lives.
        applications = [
            app
            for app in _as_list(software.get("applications"))
            if isinstance(app, dict) and not app.get("shipped_with_windows")
        ]
        # Dropped here, once, rather than filtered out of each list below:
        # what came with Windows is on the new machine already, so it is
        # neither something to reinstall nor a chore to write down. The scan
        # has already taken these out of the winget import.
        artifacts.reinstallable_count = sum(1 for app in applications if app.get("winget_id"))
        # Runtimes and drivers arrive with whatever needs them; listing them as
        # chores would bury the handful that genuinely need a person.
        manual = [
            app
            for app in applications
            if not app.get("winget_id")
            and not app.get("component")
            and not app.get("covered_by_office")
            and not app.get("managed_by")
        ]
        components = [
            app
            for app in applications
            if not app.get("winget_id") and app.get("component") and not app.get("managed_by")
        ]
        # Exclude anything winget can reinstall: a launcher app carries its own
        # package and belongs in the import, not the "returns with the launcher"
        # list, even if a heuristic also tagged it managed_by.
        launcher_games = [
            app for app in applications if app.get("managed_by") and not app.get("winget_id")
        ]
        artifacts.manual_count = len(manual)
        artifacts.component_count = len(components)
        artifacts.launcher_count = len(launcher_games)
        if export:
            path = directory / WINGET_IMPORT_FILE
            path.write_text(
                json.dumps(without_versions(export), indent=2), encoding="utf-8"
            )
            artifacts.winget_import = path
        if manual or launcher_games:
            path = directory / MANUAL_LIST_FILE
            path.write_text(
                _manual_markdown(
                    manual, artifacts.reinstallable_count, components, launcher_games
                ),
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


#: The console build's name in the shipped package. The window is
#: WinMigrate.exe and cannot run commands: it is a windowed binary with no
#: console, so anything it printed would go nowhere.
CONSOLE_BUILD = "winmigrate-cli.exe"


def console_command() -> str:
    """How to name this program in an instruction somebody has to type.

    Getting this wrong is not a cosmetic matter. The restore report prints a
    command for reinstalling the software, and it printed the name of the
    running executable -- which, in the window, is WinMigrate.exe. Typing that
    with "reinstall" after it opened the wizard on its first page, offering to
    back the machine up. Somebody who came to install their programs was handed
    a backup wizard, and it looked like a working program the whole time.

    The package ships two binaries: the window, and the console build beside
    it. Only the second can run a command, so that is the one to name -- by
    full path, because the person reading this is not necessarily sitting in
    that folder.
    """
    import sys  # noqa: PLC0415

    if not getattr(sys, "frozen", False):
        return "winmigrate"
    beside = Path(sys.executable).with_name(CONSOLE_BUILD)
    if beside.is_file():
        return f'"{beside}"' if " " in str(beside) else str(beside)
    # No console build next to us. Naming a file that is not there is worse
    # than naming the window, which now explains itself when asked to run a
    # command it cannot.
    return Path(sys.executable).name


def without_versions(export: Any) -> Any:
    """The export with each package's pinned version taken out.

    ``winget import`` is also given ``--ignore-versions``, and this is the same
    fix said twice on purpose, because the failure it prevents is total:
    nothing installs, and the reason is reported per package in a way that
    reads as the packages being gone rather than as a version being stale.

    A version is only worth keeping in this file if the point is to reproduce a
    machine exactly, and it is not -- somebody moving house wants Notepad++
    back, not last year's build of it. Which version they had is still recorded
    beside every application in the manifest; it is simply no longer an
    instruction.

    Two reasons for doing it here rather than trusting the flag alone. An older
    winget may not take the flag, and would then be handed a file it cannot
    satisfy. And this file is something the user can run themselves -- the
    restore report prints the command next to it -- so it has to work on its
    own, not only when this program builds the command line around it.

    Copied, never edited in place: the manifest this came from is read again
    for the report, and quietly emptying its fields is the sort of thing that
    surfaces three screens later as an unrelated blank.
    """
    if not isinstance(export, dict):
        return export
    copy = dict(export)
    if not isinstance(copy.get("Sources"), list):
        return copy
    sources = []
    for source in copy["Sources"]:
        if not isinstance(source, dict):
            sources.append(source)
            continue
        entry = dict(source)
        packages = entry.get("Packages")
        if isinstance(packages, list):
            entry["Packages"] = [
                {key: value for key, value in package.items() if key != "Version"}
                if isinstance(package, dict)
                else package
                for package in packages
            ]
        sources.append(entry)
    copy["Sources"] = sources
    return copy


def _as_list(value: Any) -> list[Any]:
    """``value`` if it is a list, else nothing. A string is not a list of apps."""
    return value if isinstance(value, list) else []


def _record_for(manifest: dict[str, Any], category: str) -> dict[str, Any] | None:
    for item in _as_list(manifest.get("items")):
        if not isinstance(item, dict):
            continue
        if item.get("category") == category and isinstance(item.get("record"), dict):
            return item["record"]
    return None


def _installation_from_record(record: dict[str, Any]) -> OfficeInstallation:
    installation = read_configuration(
        {
            "ProductReleaseIds": ",".join(
                str(pid) for pid in _as_list(record.get("product_ids"))
            ),
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
    launcher_games: list[dict[str, Any]] | None = None,
) -> str:
    components = components or []
    launcher_games = launcher_games or []
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
    if launcher_games:
        by_launcher: dict[str, list[dict[str, Any]]] = {}
        for app in launcher_games:
            by_launcher.setdefault(str(app.get("managed_by")), []).append(app)
        lines += [
            "",
            "## Games (return through their launcher)",
            "",
            "These re-download once you sign in to the launcher -- there is nothing",
            "to install by hand. Listed so you know where they went.",
        ]
        for launcher in sorted(by_launcher):
            games = by_launcher[launcher]
            lines += ["", f"### {launcher} ({len(games)})", ""]
            for app in sorted(games, key=lambda item: str(item.get("name", "")).lower()):
                lines.append(f"- {str(app.get('name', '')).strip()}")
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    r"""One table cell: ``|`` escaped, and no newline left to break the row.

    A registry DisplayName is whatever the installer wrote there, newlines
    included, and one of those turns the rest of the table into loose text.
    """
    text = str(value if value is not None else "")
    return " ".join(text.split()).replace("|", r"\|")


def _table_rows(applications: list[dict[str, Any]]) -> list[str]:
    rows = []
    for app in sorted(applications, key=lambda item: str(item.get("name", "")).lower()):
        rows.append(
            f"| {_cell(app.get('name'))} | {_cell(app.get('version'))} "
            f"| {_cell(app.get('publisher'))} |"
        )
    return rows


def office_reactivation_steps(installation: OfficeInstallation | None) -> list[str]:
    if installation is None:
        return []
    return list(REACTIVATION_STEPS.get(installation.activation_type, REACTIVATION_STEPS["unknown"]))


#: Removes the installers' own progress windows. Worth having and not worth
#: worrying about: ``winget import`` does not offer it on every build, and what
#: keeps an install unattended is not this.
#:
#: What keeps it unattended is ``--disable-interactivity``, which is passed
#: unconditionally, together with winget's ordinary behaviour of running each
#: package with the silent switches its manifest declares. A wizard that stops
#: to ask where to install is what ``--interactive`` is for, and that is never
#: passed here. So a winget without this flag installs the same packages
#: without asking the same questions; the difference is whether progress
#: windows appear while it works.
SILENT_FLAG = "--silent"

#: Without this, an import is a list of *exact versions* to install.
#:
#: ``winget export`` writes down the version of each package that was
#: installed, and ``winget import`` then insists on that version. The community
#: repository does not keep old manifests, so within weeks the version on the
#: old machine is one nobody can install any more, and the import answers
#: "No version found matching: 8.8.1" and then "Search failed for:
#: Notepad++.Notepad++" -- for every package at once, because they all aged
#: together. The migration reports nothing installed and every reason looks
#: like a missing package.
#:
#: Some versions are unusable from the moment they are written. An entry winget
#: matched through Add/Remove Programs can carry whatever the installer put
#: there, so the export ends up asking for "Unknown", or for "< 17.14.41",
#: which is not a version at all.
#:
#: This is the documented way to say "the newest one will do", which for
#: somebody moving to a new machine is what they wanted anyway: they are not
#: trying to reproduce last year's build of Notepad++, they are trying to get
#: Notepad++ back.
IGNORE_VERSIONS_FLAG = "--ignore-versions"

#: Asked about rather than assumed, newest concern first. Anything here is
#: added only when this machine's winget says it takes it.
OPTIONAL_FLAGS: tuple[str, ...] = (IGNORE_VERSIONS_FLAG, SILENT_FLAG)

#: What it actually costs when a winget will not take one of these, so the log
#: records the consequence rather than only the refusal. A line saying an
#: option was declined reads like a fault; most of the time it is not one, and
#: somebody reading the log after a migration deserves to know which.
FLAG_CONSEQUENCES: dict[str, str] = {
    SILENT_FLAG: (
        "installers may show their own progress windows; they still will not "
        "ask questions, which is --disable-interactivity's job and is always on"
    ),
    IGNORE_VERSIONS_FLAG: (
        "no effect here -- the versions have already been left out of the "
        "import file for this reason"
    ),
}


def accepted_flags(runner=process.run) -> tuple[str, ...]:
    """Which of :data:`OPTIONAL_FLAGS` this winget's ``import`` will take.

    Asked, never assumed. Passing an option winget does not know is not a
    degraded install -- it is a usage error raised before the first package, so
    nothing installs at all, which is the worst outcome available here.

    So the help is read, once, and searched for each flag by name. Option names
    are not translated, so this works on a machine running Windows in any
    language, and a winget that is missing or broken answers nothing and the
    import runs as it always did.
    """
    result = runner(["winget", "import", "-?"], timeout=60)
    help_text = result.stdout or ""
    accepted = tuple(flag for flag in OPTIONAL_FLAGS if flag in help_text)
    log.info(
        "winget import accepts: %s", ", ".join(accepted) if accepted else "none of the extras"
    )
    for flag in OPTIONAL_FLAGS:
        if flag not in accepted:
            log.info(
                "winget import does not accept %s, so it is left off: %s",
                flag, FLAG_CONSEQUENCES.get(flag, "the import runs without it"),
            )
    return accepted


def supports_silent(runner=process.run) -> bool:
    """Does this winget's ``import`` take ``--silent``? See :data:`SILENT_FLAG`
    for what it costs when it does not, which is less than the name suggests.
    """
    return SILENT_FLAG in accepted_flags(runner)


def supports_ignore_versions(runner=process.run) -> bool:
    """Does this winget's ``import`` take ``--ignore-versions``?"""
    return IGNORE_VERSIONS_FLAG in accepted_flags(runner)


def winget_import_command(import_file: Path, extra: "tuple[str, ...] | list[str]" = ()) -> list[str]:
    """The command that replays winget's export on this machine.

    ``--ignore-unavailable`` keeps one missing package from aborting the rest,
    and ``--accept-package-agreements`` is required for an unattended run --
    the user has already agreed to this step at the prompt.

    ``extra`` is whatever of :data:`OPTIONAL_FLAGS` this machine's winget said
    it would take, which is worked out once by :func:`accepted_flags` rather
    than guessed here.

    Named separately from the running of it because it is run two ways: the
    command line waits for the whole thing, and the window streams it so a
    ninety-seven application install is something you can watch rather than a
    bar that moves once at the end.
    """
    command = [
        "winget",
        "import",
        "-i",
        str(import_file),
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--ignore-unavailable",
        "--disable-interactivity",
    ]
    command.extend(extra)
    return command


def import_command(import_file: Path, runner=process.run) -> list[str]:
    """The import command this machine's winget will actually accept."""
    return winget_import_command(import_file, accepted_flags(runner))


def run_winget_import(import_file: Path, runner=process.run) -> process.CommandResult:
    """Replay winget's export on this machine, waiting for it to finish."""
    return runner(import_command(import_file, runner), timeout=WINGET_IMPORT_TIMEOUT)


def package_identifiers(import_file: Path) -> list[str]:
    """The packages a winget export asks for, so a window can show the list.

    The export is a file this tool wrote, but it is read back off a disk that
    a restore has just written to; a malformed one means an empty list and a
    page that says so, never an exception on the way to the finish line.
    """
    try:
        export = json.loads(import_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("could not read %s", import_file, exc_info=True)
        return []
    names: list[str] = []
    for source in _as_list(export.get("Sources")):
        if not isinstance(source, dict):
            continue
        for package in _as_list(source.get("Packages")):
            identifier = package.get("PackageIdentifier") if isinstance(package, dict) else None
            if isinstance(identifier, str) and identifier:
                names.append(identifier)
    return names


def run_office_install(
    setup_executable: Path, configuration: Path, runner=process.run
) -> process.CommandResult:
    return run_setup(setup_executable, configuration, runner=runner)
