"""Running external Windows tools, with failure treated as information.

``winget``, ``powershell`` and ``cscript`` are all optional in practice: a
machine may not have the App Installer, PowerShell may be locked down, Office
may not be installed. None of that is an error -- it just means a smaller
inventory -- so every call here returns a result object describing what
happened rather than raising, and the scan records it as a note.

Commands are always passed as argument lists, never through a shell.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120


@dataclass(slots=True)
class CommandResult:
    """The outcome of one external command."""

    command: list[str]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None       # set when the command could not run at all

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0

    @property
    def unavailable(self) -> bool:
        """True when the tool itself is missing, as opposed to failing."""
        return self.error is not None and "not found" in self.error.lower()

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.returncode:
            first = (self.stderr or self.stdout).strip().splitlines()
            return f"exit {self.returncode}: {first[0] if first else 'no output'}"
        return "ok"


def run(
    command: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    input_text: str | None = None,
) -> CommandResult:
    """Run a command, capturing output. Never raises for the command's sake."""
    log.debug("running: %s", " ".join(command))
    try:
        completed = subprocess.run(  # noqa: S603 -- argument list, no shell
            command,
            capture_output=True,
            timeout=timeout,
            input=input_text.encode("utf-8") if input_text is not None else None,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(command, None, error=f"{command[0]} not found on this machine")
    except PermissionError as exc:
        return CommandResult(command, None, error=f"{command[0]} could not be run: {exc}")
    except subprocess.TimeoutExpired:
        return CommandResult(command, None, error=f"{command[0]} timed out after {timeout}s")
    except OSError as exc:  # pragma: no cover -- platform-specific launch failures
        return CommandResult(command, None, error=f"{command[0]} could not be run: {exc}")
    return CommandResult(
        command,
        completed.returncode,
        decode(completed.stdout),
        decode(completed.stderr),
    )


#: Tried in order. Console tools on Windows are not reliably UTF-8: output can
#: arrive UTF-16 (PowerShell redirection) or in the legacy ANSI code page, and a
#: mis-decode turns a parseable table into replacement characters.
DECODINGS = ("utf-8-sig", "utf-16", "cp1252")


def decode(data: bytes | str | None) -> str:
    """Decode tool output, trying the encodings Windows actually produces."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""
    if b"\x00" in data[:64]:
        # Interleaved null bytes mean UTF-16; utf-8 would yield mojibake.
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
    for encoding in DECODINGS:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def powershell(script: str, timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
    """Run a PowerShell snippet with profiles and prompts disabled."""
    return run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout=timeout,
    )
