"""Wi-Fi profiles: opt-in, and captured as SECRET because they hold the keys."""

from __future__ import annotations

from pathlib import Path

from winmigrate.models import Category, Sensitivity
from winmigrate.platform_win import Environment
from winmigrate.scan import wifi

SHOW_PROFILES = """
Profiles on interface Wi-Fi:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
-------------
    All User Profile     : HomeNet
    All User Profile     : Cafe Free WiFi
    All User Profile     : Office-5G
"""


def test_profile_names_are_parsed_from_netsh_output():
    assert wifi.parse_profile_names(SHOW_PROFILES) == ["HomeNet", "Cafe Free WiFi", "Office-5G"]


def test_parsing_tolerates_empty_or_unexpected_output():
    assert wifi.parse_profile_names("") == []
    assert wifi.parse_profile_names("no profiles here") == []


def test_wifi_is_silent_unless_opted_in(tmp_path: Path):
    """Without --include-wifi, nothing about the network keys is even mentioned."""
    env = Environment.fixture(tmp_path, {})
    items, followups = wifi.scan_wifi(env, include_wifi=False)
    assert items == [] and followups == []


def test_the_wifi_record_is_secret_when_present(monkeypatch):
    """It must be SECRET: the captured export carries the pre-shared keys."""
    env = Environment.fixture(Path("/tmp/p"), {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(wifi, "list_profiles", lambda: (["HomeNet", "Office-5G"], None))
    items, _ = wifi.scan_wifi(env, include_wifi=True)
    assert len(items) == 1
    assert items[0].category is Category.WIFI
    assert items[0].sensitivity is Sensitivity.SECRET
    assert items[0].record["profiles"] == ["HomeNet", "Office-5G"]


def test_a_netsh_failure_becomes_a_followup_not_a_crash(monkeypatch):
    env = Environment.fixture(Path("/tmp/p"), {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(wifi, "list_profiles", lambda: ([], "access denied"))
    items, followups = wifi.scan_wifi(env, include_wifi=True)
    assert items == []
    assert followups and followups[0].id == "wifi:unavailable"


def test_files_only_reports_wifi_but_captures_none_of_it(monkeypatch):
    """files-only promises no credential material travels, and a Wi-Fi profile
    carries the network password."""
    from winmigrate.models import Action, SkipReason

    env = Environment.fixture(Path("/tmp/p"), {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(wifi, "list_profiles", lambda: (["HomeNet"], None))

    items, _ = wifi.scan_wifi(env, include_wifi=True, files_only=True)
    assert items[0].action is Action.SKIP
    assert items[0].skip_reason is SkipReason.FILES_ONLY_MODE

    items, _ = wifi.scan_wifi(env, include_wifi=True, files_only=False)
    assert items[0].action is Action.CAPTURE
