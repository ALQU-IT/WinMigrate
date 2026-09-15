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


def stream(
    command: list[str],
    on_line,
    timeout: int = DEFAULT_TIMEOUT,
    cancelled=None,
) -> CommandResult:
    """Run a command, handing each line of its output over as it arrives.

    :func:`run` captures everything and returns at the end, which is right for
    a tool that answers in under a second and wrong for one that installs
    ninety-seven applications. A window watching that happen needs the output
    while it is happening, or it is a progress bar with nothing behind it.

    ``on_line`` is called for every complete line, and is not allowed to stop
    the run: it goes to a queue the window drains, and an exception there would
    kill the install rather than the label it failed to write. ``cancelled`` is
    asked between reads, so Stop takes effect at the next line rather than at
    the end of a two-hour import.

    Output is decoded incrementally, so a multi-byte character split across two
    reads does not become two replacement characters. Lines are split on CR as
    well as LF: a console progress indicator rewrites one line with carriage
    returns, and waiting for an LF that never comes shows nothing at all.
    """
    import codecs  # noqa: PLC0415
    import time  # noqa: PLC0415

    log.debug("streaming: %s", " ".join(command))
    try:
        process = subprocess.Popen(  # noqa: S603 -- argument list, no shell
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
    except FileNotFoundError:
        return CommandResult(command, None, error=f"{command[0]} not found on this machine")
    except (PermissionError, OSError) as exc:
        return CommandResult(command, None, error=f"{command[0]} could not be run: {exc}")

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    collected: list[str] = []
    pending = ""
    deadline = time.monotonic() + timeout
    stopped = False

    def emit(line: str) -> None:
        text = line.strip()
        if not text:
            return
        collected.append(text)
        try:
            on_line(text)
        except Exception:  # noqa: BLE001 -- a label must never end an install
            log.debug("the output handler raised; continuing", exc_info=True)

    timed_out = False
    try:
        while process.stdout is not None:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            pending = pending.replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                emit(line)
            if cancelled is not None and cancelled():
                stopped = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
    finally:
        pending += decoder.decode(b"", final=True)
        emit(pending)
        if stopped or timed_out:
            _end(process)
        else:
            # Its output ended, which is not quite the same as it having
            # exited. Waiting is the difference between reading its exit code
            # and killing a program that had finished.
            try:
                process.wait(timeout=30)
            except Exception:  # noqa: BLE001
                _end(process)

    if stopped:
        return CommandResult(command, None, "\n".join(collected), error="stopped")
    if timed_out:
        return CommandResult(
            command, None, "\n".join(collected),
            error=f"{command[0]} timed out after {timeout}s",
        )
    return CommandResult(command, process.poll(), "\n".join(collected))


def _end(process) -> None:
    """Ask the process to stop, then insist. Never raises."""
    for stop in (process.terminate, process.kill):
        try:
            stop()
            process.wait(timeout=5)
            return
        except Exception:  # noqa: BLE001 -- it may already be gone
            continue
