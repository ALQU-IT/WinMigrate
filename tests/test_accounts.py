"""Whose profile is this -- and is it the account WinMigrate is running as?"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from winmigrate import accounts
from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
from winmigrate.platform_win import HKCU, HKU, Environment

ME = "S-1-5-21-100-200-300-500"          # the administrator this runs as
SECOND = "S-1-5-21-100-200-300-1001"     # the person at the screen
LIST = accounts.PROFILE_LIST_KEY


def machine(**extra) -> Environment:
    """A Windows machine with two profiles, and only ``second`` signed in."""
    registry = {
        f"HKLM\\{LIST}\\{ME}": {"ProfileImagePath": r"C:\Users\admin"},
        f"HKLM\\{LIST}\\{SECOND}": {"ProfileImagePath": r"%SystemDrive%\Users\second"},
        f"HKLM\\{LIST}\\S-1-5-18": {"ProfileImagePath": r"C:\Windows\system32\config"},
        f"HKU\\{SECOND}\\{accounts.VOLATILE_KEY}": {
            "USERNAME": "second", "USERDOMAIN": "WIN11", "USERPROFILE": r"C:\Users\second",
        },
        f"HKU\\{SECOND}_Classes\\x": {},
        f"HKU\\{ME}\\{accounts.VOLATILE_KEY}": {
            "USERNAME": "admin", "USERDOMAIN": "WIN11", "USERPROFILE": r"C:\Users\admin",
        },
        **extra,
    }
    return Environment(
        profile_root=Path(r"C:\Users\admin"),
        environ={"SystemDrive": "C:", "USERPROFILE": r"C:\Users\admin"},
        registry=registry,
        is_windows=True,
    )


def test_a_profile_is_matched_to_its_owner_through_the_profile_list():
    env = machine()
    assert accounts.profile_owner(Path(r"C:\Users\second"), env) == SECOND
    assert accounts.profile_owner(Path(r"c:\users\SECOND\\"), env) == SECOND
    assert accounts.profile_owner(Path(r"D:\Elsewhere"), env) is None


def test_the_signed_in_accounts_are_read_from_their_open_registries():
    found = accounts.signed_in(machine())
    assert set(found) == {ME, SECOND}
    assert found[SECOND]["profile"] == r"C:\Users\second"


def test_the_person_at_the_screen_is_offered_when_this_runs_as_someone_else():
    """Started "as administrator" by a standard user, Path.home() is the
    administrator's. The profile to offer is the one whose desktop this is."""
    env = machine()
    profile = accounts.desktop_profile(
        env, me=lambda: ME, desktop=lambda: ("WIN11", "second")
    )
    assert profile == Path(r"C:\Users\second")


def test_nothing_changes_when_this_runs_as_the_person_at_the_screen():
    env = machine()
    assert accounts.desktop_profile(
        env, me=lambda: SECOND, desktop=lambda: ("WIN11", "second")
    ) is None
    # And not knowing is the ordinary case, not a guess.
    assert accounts.desktop_profile(env, me=lambda: None, desktop=lambda: None) is None


def test_another_owners_registry_is_reached_under_hkey_users(monkeypatch):
    env = accounts.settle(machine(), SECOND, ME, loaded=True)
    assert env.user_sid == SECOND and env.other_account

    monkeypatch.setattr(Environment, "_hive", staticmethod(lambda hive: hive))
    assert env._where(HKCU, r"Control Panel\Desktop") == (
        HKU, f"{SECOND}\\Control Panel\\Desktop"
    )
    assert env._where("HKLM", "SOFTWARE") == ("HKLM", "SOFTWARE")


def test_the_same_account_is_left_as_it_was():
    env = accounts.settle(machine(), ME, ME, loaded=True)
    assert env.user_sid is None and not env.other_account
    env = accounts.settle(machine(), None, ME, loaded=False)
    assert env.user_sid is None and not env.other_account


def test_an_owner_who_is_not_signed_in_is_not_replaced_by_whoever_runs_this():
    """Their registry is not open. Reading or writing HKCU would be the
    administrator's settings, filed under somebody else's name."""
    env = Environment(Path(r"C:\Users\second"), registry=None, is_windows=True)
    accounts.settle(env, SECOND, ME, loaded=False)

    assert env.other_account
    assert env.read_registry_key(HKCU, r"Control Panel\Desktop") is None
    assert env.write_registry_value(HKCU, r"Control Panel\Desktop", "Wallpaper", "x") is False
    assert env.registry_subkeys(HKCU, "Software") == []


def test_explorer_is_not_restarted_as_somebody_else():
    """It would stop the person's desktop and start it again as the
    administrator -- the administrator's taskbar, on their screen."""
    env = Environment(Path(r"C:\Users\second"), registry=None, is_windows=True,
                      user_sid=SECOND)

    def boom(*a, **k):  # pragma: no cover -- the point is it is not called
        raise AssertionError("Explorer was restarted from another account")

    result = apply_mod._restart_explorer(env, runner=boom, starter=boom, pause=lambda s: None)
    assert result.outcome is Outcome.SKIPPED and "sign out" in result.detail


def test_the_background_goes_into_the_owners_settings_not_the_running_accounts(
    monkeypatch, tmp_path: Path
):
    image = tmp_path / "beach.jpg"
    image.write_bytes(b"\xff\xd8")
    env = Environment(tmp_path, registry=None, is_windows=True, user_sid=SECOND)
    writes: list[tuple] = []
    monkeypatch.setattr(
        Environment, "write_registry_value",
        lambda self, hive, key, name, value: writes.append((self.user_sid, key, name, value))
        or True,
    )
    # A stand-in for the Windows call, so that running this on a Windows
    # machine does not change that machine's desktop.
    calls: list[tuple] = []
    user32 = types.SimpleNamespace(
        SystemParametersInfoW=lambda *args: calls.append(args) or 1
    )
    monkeypatch.setitem(
        sys.modules, "ctypes", types.SimpleNamespace(windll=types.SimpleNamespace(user32=user32))
    )

    result = apply_mod._set_desktop_picture(env, image)

    assert writes == [(SECOND, apply_mod.DESKTOP_KEY, "Wallpaper", str(image))]
    # Shown now, but not saved into the settings of the account running this.
    assert calls and not calls[0][3] & apply_mod._SPIF_UPDATEINIFILE
    assert result.outcome is Outcome.APPLIED and "sign in" in result.detail
