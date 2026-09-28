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
#: The login programs, kept beside the install files so the command-line
#: install can put them back once there are programs for them to start. Not a
#: secret: the same record is in the plaintext listing beside every bundle.
STARTUP_FILE = "startup-entries.json"

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

    # A restore writes the login programs before any of them are installed, so
    # every one is skipped then. The window asks again after its own install;
    # the command-line install runs in another process, later, with no manifest
    # to ask from -- so the entries wait here for it.
    startup = _startup_record(manifest)
    if startup:
        (directory / STARTUP_FILE).write_text(json.dumps(startup, indent=2), encoding="utf-8")

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
        if " " not in str(beside):
            return str(beside)
        # A path with a space has to be quoted, and a quoted path is not a
        # command in PowerShell -- "C:\Users\Bob Smith\...exe" reinstall is a
        # string followed by a syntax error. The call operator makes it one.
        # PowerShell is what Windows 11 opens by default and what a right-click
        # in a folder offers; cmd.exe, which would reject the "&", is the one
        # that has to be gone looking for.
        return f'& "{beside}"'

    # No console build next to us. Naming a file that is not there is worse
    # than naming the window, which now explains itself when asked to run a
    # command it cannot.
    return Path(sys.executable).name


def helper_command(
    import_file: Path, results_file: Path, log_file: Path | None = None
) -> tuple[str, list[str]] | None:
    """What the window elevates to install the programs: this tool, not winget.

    Elevating winget itself means one Windows prompt per package, or one
    ``winget import`` that a single awkward package stops dead. Elevating the
    console build once, and letting it run :func:`install_packages`, costs one
    prompt and keeps every package's answer separate. It writes them to
    ``results_file`` as it goes, and the window reads them from there.

    The console build rather than the window's own executable because it has a
    console: the elevated helper shows, in its own window, which program it is
    installing -- nothing this tool does happens out of sight.

    A copy on a network share is copied next to the install files first. The
    elevated session is a different logon -- often a different account, the
    one whose password was just typed -- and a share that needed this user's
    credentials is not open to it. The install files are on a local disk, in
    this user's profile, which an administrator can always read.

    None when there is no console build to run, which only a partial copy of
    the package can cause.
    """
    import shutil  # noqa: PLC0415
    import sys  # noqa: PLC0415

    arguments = [
        "reinstall", str(import_file.parent), "--apps", "--yes",
        "--results", str(results_file),
    ]
    if log_file is not None:
        arguments += ["--log-file", str(log_file)]
    if not getattr(sys, "frozen", False):
        return sys.executable, ["-m", "winmigrate", *arguments]
    console_build = Path(sys.executable).with_name(CONSOLE_BUILD)
    if not console_build.is_file():
        return None
    try:
        resolved = str(console_build.resolve())
    except (OSError, RuntimeError):
        resolved = str(console_build)
    if resolved.startswith("\\\\"):
        local = import_file.parent / CONSOLE_BUILD
        try:
            shutil.copy2(console_build, local)
            log.info("copied %s to %s so the elevated helper can reach it",
                     console_build, local)
            return str(local), arguments
        except OSError:
            log.warning("could not copy %s next to the install files", console_build,
                        exc_info=True)
    return resolved, arguments


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


def _startup_record(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The Run entries record, if the bundle has one with anything in it."""
    for item in _as_list(manifest.get("items")):
        if isinstance(item, dict) and item.get("id") == "settings:startup_run":
            record = item.get("record")
            if isinstance(record, dict) and isinstance(record.get("entries"), dict) \
                    and record["entries"]:
                return record
    return None


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


@dataclass(slots=True, frozen=True)
class Package:
    """One entry of a winget export: what it is, and which catalogue it is in."""

    identifier: str
    source: str = "winget"


def packages(import_file: Path) -> list[Package]:
    """The packages a winget export asks for, each with the source it came from.

    The export is a file this tool wrote, but it is read back off a disk that
    a restore has just written to; a malformed one means an empty list and a
    page that says so, never an exception on the way to the finish line.
    """
    try:
        export = json.loads(import_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("could not read %s", import_file, exc_info=True)
        return []
    if not isinstance(export, dict):
        return []
    found: list[Package] = []
    seen: set[str] = set()
    for source in _as_list(export.get("Sources")):
        if not isinstance(source, dict):
            continue
        details = source.get("SourceDetails")
        name = details.get("Name") if isinstance(details, dict) else None
        name = name if isinstance(name, str) and name else "winget"
        for package in _as_list(source.get("Packages")):
            identifier = package.get("PackageIdentifier") if isinstance(package, dict) else None
            if isinstance(identifier, str) and identifier and identifier not in seen:
                seen.add(identifier)
                found.append(Package(identifier, name))
    return found


def package_identifiers(import_file: Path) -> list[str]:
    """The packages a winget export asks for, so a window can show the list."""
    return [package.identifier for package in packages(import_file)]


# --- installing one package at a time -------------------------------------
#
# ``winget import`` looked like the natural way to replay an export, and it
# fails as a unit. It resolves every package before installing any, and a
# single one it cannot install unattended stops the lot: on a real migration of
# ninety-odd applications, one package whose manifest insists on being told
# where to go (0x8A15005F) ended the import fourteen seconds in, with nothing
# installed and no window left open to say why. The same goes for a package
# the catalogue has since dropped, or one Windows already has.
#
# So each package is its own ``winget install``, and each answer is its own
# line in the report. One that fails costs that one program, which is the most
# any single package should be able to cost.

#: How long one package may take. Generous -- an IDE or a game launcher can
#: download gigabytes -- but finite, so one installer waiting on a dialog nobody
#: can see does not hold up the other ninety.
PACKAGE_TIMEOUT = 1800

#: Where the window's elevated helper writes its answers, one JSON line per
#: package, as it goes -- the window reads them while the helper runs.
RESULTS_FILE = "install-results.jsonl"

#: winget's documented return codes -- the ones this step answers differently.
INSTALL_LOCATION_REQUIRED = 0x8A15005F
NO_PACKAGE_FOUND = 0x8A150014
#: "Already here" in its several spellings: the same package, a newer one, or
#: one winget found and had nothing to upgrade it with. Not a failure: the
#: program is on the machine, which is the whole of what was asked.
ALREADY_HERE = frozenset({0x8A150061, 0x8A15002B, 0x8A15010D, 0x8A15010E})
#: Installed, but Windows has to restart before it is finished.
RESTART_TO_FINISH = frozenset({0x8A150109, 0x8A15010B})

#: What the installer-level failures mean, in words somebody can act on.
REASONS = {
    NO_PACKAGE_FOUND: "winget no longer has this package -- it is on the by-hand list",
    INSTALL_LOCATION_REQUIRED: "this package has to be told where to install",
    0x8A150101: "the program is running; close it and try again",
    0x8A150102: "another installation was in progress",
    0x8A150103: "a file it needs was in use",
    0x8A150104: "it needs something this machine does not have",
    0x8A150105: "the disk is full",
    0x8A150106: "there was not enough memory",
    0x8A150107: "it needs an internet connection",
    0x8A150108: "its installer failed; the publisher's support is the next step",
    0x8A15010A: "Windows has to restart before it can install",
    0x8A15010C: "its installer was cancelled",
    0x8A15010F: "a policy on this machine blocks it",
    0x8A150110: "a program it depends on did not install",
    0x8A150111: "the program is in use",
    0x8A150113: "it does not support this version of Windows",
}

#: Asked about for ``winget install``, as for the import. Every winget that has
#: ``install`` has taken this for years, but asking costs one call and a wrong
#: guess costs every package.
INSTALL_OPTIONAL_FLAGS: tuple[str, ...] = (SILENT_FLAG,)


@dataclass(slots=True)
class PackageResult:
    """What happened to one package.

    ``outcome`` is one of ``installed``, ``already``, ``not_found``,
    ``failed``, ``stopped`` or ``no_winget``.
    """

    identifier: str
    outcome: str
    detail: str = ""
    code: int | None = None

    @property
    def present(self) -> bool:
        """Is the program on this machine now?"""
        return self.outcome in {"installed", "already"}

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.identifier, "outcome": self.outcome, "detail": self.detail,
             "code": self.code},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> "PackageResult | None":
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return None
        code = data.get("code")
        return cls(
            data["id"], str(data.get("outcome") or "failed"), str(data.get("detail") or ""),
            code if isinstance(code, int) else None,
        )


def install_flags(runner=process.run) -> tuple[str, ...]:
    """Which of :data:`INSTALL_OPTIONAL_FLAGS` this machine's ``winget install`` takes."""
    result = runner(["winget", "install", "-?"], timeout=60)
    help_text = result.stdout or ""
    return tuple(flag for flag in INSTALL_OPTIONAL_FLAGS if flag in help_text)


def install_command(
    package: Package, extra: "tuple[str, ...] | list[str]" = (), location: str | None = None
) -> list[str]:
    """The command that installs one package, unattended, at today's version.

    ``--exact`` so that "Git.Git" is Git and not the first thing whose name
    contains it; ``--source`` so that a Store app is fetched from the Store.
    No ``--version``: somebody moving to a new machine wants the program back,
    not last year's build of it.
    """
    command = [
        "winget", "install",
        "--id", package.identifier,
        "--exact",
        "--source", package.source,
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ]
    if location:
        command += ["--location", location]
    command.extend(extra)
    return command


def default_location(package: Package, environ=None) -> str:
    """Where a package that insists on being told gets installed.

    Program Files, in a folder named after the package, which is where its own
    installer would have put it had it been willing to choose. The old
    machine's choice is not known -- winget's export does not record it -- and
    asking somebody in the middle of ninety installs is exactly the
    interruption this step exists to avoid. The report says where it went.
    """
    import os  # noqa: PLC0415

    environ = os.environ if environ is None else environ
    base = environ.get("ProgramFiles") or "C:\\Program Files"
    name = package.identifier.rsplit(".", 1)[-1] or package.identifier
    return f"{base}\\{name}"


def classify(package: Package, result: process.CommandResult) -> PackageResult:
    """Turn one ``winget install`` into an answer a person can read."""
    if result.error == "stopped":
        return PackageResult(package.identifier, "stopped", "stopped before it finished")
    if result.unavailable:
        return PackageResult(package.identifier, "no_winget", "winget is not on this machine")
    if result.error:
        return PackageResult(package.identifier, "failed", result.error)
    code = (result.returncode or 0) & 0xFFFFFFFF
    if code == 0:
        return PackageResult(package.identifier, "installed", code=0)
    if code in RESTART_TO_FINISH:
        return PackageResult(
            package.identifier, "installed", "restart the PC to finish installing it", code
        )
    if code in ALREADY_HERE:
        return PackageResult(package.identifier, "already", "already on this machine", code)
    if code == NO_PACKAGE_FOUND:
        return PackageResult(package.identifier, "not_found", REASONS[code], code)
    reason = REASONS.get(code) or f"winget stopped with 0x{code:08X}"
    return PackageResult(package.identifier, "failed", reason, code)


def install_packages(
    wanted: list[Package],
    runner=None,
    flags: "tuple[str, ...] | None" = None,
    on_start=None,
    on_result=None,
    cancelled=None,
    environ=None,
) -> list[PackageResult]:
    """Install each package on its own, so that one failure costs one program.

    ``runner`` runs one command and returns a :class:`process.CommandResult`;
    the window hands in one that streams winget's output to the page.
    ``on_start(index, package)`` and ``on_result(result)`` report progress,
    and ``cancelled`` is asked between packages.

    A package that insists on an install location is tried once more with one
    (see :func:`default_location`) rather than written off: the migration's
    whole point is that nobody has to sit there answering installers.
    """
    runner = runner or process.run
    if flags is None:
        flags = install_flags(runner)
    results: list[PackageResult] = []
    for index, package in enumerate(wanted):
        if cancelled is not None and cancelled():
            break
        if on_start is not None:
            on_start(index, package)
        command = install_command(package, flags)
        answer = classify(package, runner(command, timeout=PACKAGE_TIMEOUT))
        if answer.code == INSTALL_LOCATION_REQUIRED:
            location = default_location(package, environ)
            log.info("%s needs an install location; trying %s", package.identifier, location)
            retry = classify(
                package,
                runner(install_command(package, flags, location), timeout=PACKAGE_TIMEOUT),
            )
            if retry.present:
                retry.detail = f"installed into {location}"
            answer = retry
        log.info("install %s: %s%s", package.identifier, answer.outcome,
                 f" ({answer.detail})" if answer.detail else "")
        results.append(answer)
        if on_result is not None:
            on_result(answer)
        if answer.outcome in {"no_winget", "stopped"}:
            # Asking a missing winget ninety more times tells nobody anything.
            break
    return results


@dataclass(slots=True)
class InstallRun:
    """One press of Install: every package's answer, or why there were none.

    ``error`` is None when the packages were tried, else ``declined`` (Windows
    was not given permission), ``stopped``, or a sentence.
    """

    total: int
    results: list[PackageResult] = field(default_factory=list)
    error: str | None = None

    @property
    def unavailable(self) -> bool:
        return any(result.outcome == "no_winget" for result in self.results)

    @property
    def installed_any(self) -> bool:
        """Did this bring any program that was not here before?"""
        return any(result.outcome == "installed" for result in self.results)

    @property
    def counts(self) -> dict[str, int]:
        return tally(self.results)


def read_results(path: Path) -> list[PackageResult]:
    """The answers an install has written so far. A half-written line is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    results = []
    for line in text.splitlines():
        result = PackageResult.from_json(line) if line.strip() else None
        if result is not None:
            results.append(result)
    return results


def append_result(path: Path, result: PackageResult) -> None:
    """Add one answer to the results file, straight away, for whoever is watching."""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(result.to_json() + "\n")
            handle.flush()
    except OSError:
        log.warning("could not write %s", path, exc_info=True)


def tally(results: list[PackageResult]) -> dict[str, int]:
    """How many packages ended each way."""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    return counts


def run_office_install(
    setup_executable: Path, configuration: Path, runner=process.run
) -> process.CommandResult:
    return run_setup(setup_executable, configuration, runner=runner)
