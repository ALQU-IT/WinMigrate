"""Keeping the machine awake while a long job runs.

A capture of a real profile runs for an hour or more. A laptop on default power
settings sleeps after thirty minutes, and Windows does not ask whether anything
important is happening -- so the capture stops partway through, the bundle is
incomplete, and there is nothing on screen in the morning to say why. That is
not a slow backup, it is a missing one, and the cost of preventing it is a
single call.

Two deliberate choices about *what* is kept awake:

* The **system** is held awake, not the display. Someone starting a two-hour
  capture and walking away should come back to a dark screen and a finished
  bundle, not a monitor that burned all evening.
* **Away mode** is requested where the machine supports it, which is what lets
  the work continue with the lid shut on a desktop-class sleep. It is refused
  on plenty of hardware, and a refusal is not an error -- the ordinary system
  request still holds.

The request is undone on the way out, including when the job fails. Leaving a
machine unable to sleep because a backup crashed would be a rude thing to do to
someone's laptop.
"""

from __future__ import annotations

import logging
import sys
from types import TracebackType

log = logging.getLogger(__name__)

#: SetThreadExecutionState flags.
ES_CONTINUOUS = 0x80000000        # keep the state until it is cleared
ES_SYSTEM_REQUIRED = 0x00000001   # do not sleep
ES_DISPLAY_REQUIRED = 0x00000002  # deliberately not used; the screen may sleep
ES_AWAYMODE_REQUIRED = 0x00000040  # keep working with the screen off, where supported


class KeepAwake:
    """Hold off sleep for the duration of a block.

    Used as a context manager. On anything that is not Windows, and on any
    Windows where the call fails, this does nothing at all and says so in the
    log -- a backup must never fail because it could not adjust a power setting.
    """

    def __init__(self, reason: str = "backup") -> None:
        self.reason = reason
        self.held = False
        self.away_mode = False

    def __enter__(self) -> "KeepAwake":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> bool:
        """Ask Windows to stay awake. True when it agreed."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes  # noqa: PLC0415

            state = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            # Away mode first: it is the better answer and is refused by
            # returning zero, which costs nothing to find out.
            if ctypes.windll.kernel32.SetThreadExecutionState(state | ES_AWAYMODE_REQUIRED):
                self.held = True
                self.away_mode = True
            elif ctypes.windll.kernel32.SetThreadExecutionState(state):
                self.held = True
            else:
                log.info("Windows refused to hold off sleep; the %s may be interrupted", self.reason)
                return False
        except Exception as exc:  # noqa: BLE001 -- never fail a backup over a power setting
            log.info("could not hold off sleep: %s", exc)
            return False
        log.info(
            "holding off sleep for the %s%s", self.reason,
            " (away mode)" if self.away_mode else "",
        )
        return True

    def release(self) -> None:
        """Let the machine sleep again. Safe to call when nothing was held."""
        if not self.held or sys.platform != "win32":
            return
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            log.debug("sleep allowed again")
        except Exception as exc:  # noqa: BLE001
            log.info("could not restore the sleep setting: %s", exc)
        finally:
            self.held = False
            self.away_mode = False
