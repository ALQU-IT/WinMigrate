"""Scheduled tasks the user made themselves.

Windows runs hundreds of scheduled tasks and all but a handful belong to
Windows. The ones worth knowing about are the ones somebody added: a backup
script, a nightly sync, a reminder. They are invisible until they stop
happening, and when they stop happening nobody connects it to having changed
computers.

They are **reported, never re-created**. A scheduled task is a command the
machine runs on its own, unattended, possibly as a different account -- which
is a persistence mechanism as much as it is a convenience, and the one thing a
backup tool should not be able to do quietly from a file it was handed. The
task's own name and command are written down instead, so the person can
re-create the two or three that matter, having seen what they were.

The listing is a Windows-only shell-out. Parsing its output is what this module
unit-tests; the call itself is isolated for that reason.
"""

from __future__ import annotations

import csv
import io
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
from ..platform_win import Environment
from ..util import process

log = logging.getLogger(__name__)

#: Task folders that belong to Windows and its own components.
WINDOWS_OWNED = ("\\Microsoft\\", "\\Windows\\")


def scan_tasks(env: Environment):
    """Return the scheduled-task report, if the machine has any of its own."""
    if not env.is_windows:
        return [], []
    tasks, error = list_tasks()
    if error:
        log.info("could not list scheduled tasks: %s", error)
        return [], []
    if not tasks:
        return [], []

    item = Item(
        id="settings:scheduled_tasks",
        category=Category.SCHEDULED_TASKS,
        kind=Kind.REPORT,
        title=f"Scheduled tasks you made ({len(tasks)})",
        record={"tasks": tasks},
        record_public=True,
        restore=RestoreSpec(
            target="Task Scheduler",
            strategy=RestoreStrategy.GUIDED,
            notes=["Re-create the ones you still want; nothing is created for you."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "Reported, never re-created. A scheduled task is a command the "
                "machine runs on its own, unattended -- not something a backup "
                "file should be able to add to a computer quietly.",
            )
        ],
    )
    return [item], [_followup(tasks)]


def list_tasks(runner=process.run) -> tuple[list[dict[str, str]], str | None]:
    """The user's own scheduled tasks. Windows-only; returns (tasks, error)."""
    result = runner(["schtasks", "/query", "/fo", "csv", "/nh"], timeout=120)
    if not result.ok and not result.stdout.strip():
        return [], result.summary()
    return parse_tasks(result.stdout), None


def parse_tasks(output: str) -> list[dict[str, str]]:
    """Pull the user's own tasks out of ``schtasks /query /fo csv /nh``.

    Windows' own tasks live under ``\\Microsoft\\`` and are skipped: there are
    hundreds, they are identical on every machine, and listing them would bury
    the two or three that are actually somebody's.
    """
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(output or "")):
        if not row or not row[0].strip():
            continue
        name = row[0].strip().strip('"')
        if not name.startswith("\\") or name in seen:
            continue
        if any(owned.lower() in name.lower() for owned in WINDOWS_OWNED):
            continue
        seen.add(name)
        tasks.append({
            "name": name,
            "next_run": row[1].strip() if len(row) > 1 else "",
            "status": row[2].strip() if len(row) > 2 else "",
        })
    return tasks


def _followup(tasks: list[dict[str, str]]) -> Followup:
    return Followup(
        id="settings:scheduled_tasks:guided",
        title=f"Re-create the scheduled tasks you still want ({len(tasks)})",
        why=(
            "These ran on their own on the old machine and are invisible until "
            "they stop. Nothing re-creates them for you: a scheduled task is a "
            "command a computer runs unattended, and adding one quietly from a "
            "backup file is not something this tool does."
        ),
        steps=[
            "Open Task Scheduler on the old machine and export the ones you want "
            "(right-click > Export), while you still have it.",
            "Import them on the new machine with right-click > Import Task.",
            *[f"{task['name']}" for task in tasks],
        ],
        category=Category.SCHEDULED_TASKS,
    )
