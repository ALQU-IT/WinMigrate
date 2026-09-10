"""Shadow-copy path translation.

The snapshot lifecycle needs real Windows and administrator rights, so only the
pure translation is unit-tested here. The subprocess parts are isolated in
``winmigrate.vss`` for exactly that reason.
"""

from __future__ import annotations

import pytest

from winmigrate import vss


def test_a_path_is_mapped_into_the_snapshot_device():
    mapped = vss.map_into_snapshot(
        r"C:\Users\alice\Documents\a.txt", "C:\\", r"\Device\HarddiskVolumeShadowCopy3"
    )
    assert mapped == r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy3\Users\alice\Documents\a.txt"


def test_an_extended_length_path_maps_without_doubling_its_prefix():
    mapped = vss.map_into_snapshot(
        r"\\?\C:\Users\a\b.txt", "C:\\", r"\Device\HarddiskVolumeShadowCopy1"
    )
    assert mapped == r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\Users\a\b.txt"
    assert mapped.count("GLOBALROOT") == 1


def test_the_volume_root_itself_maps_to_the_device_root():
    mapped = vss.map_into_snapshot("C:\\", "C:\\", r"\Device\HarddiskVolumeShadowCopy2")
    assert mapped == "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy2\\"


def test_a_path_on_another_volume_is_refused():
    with pytest.raises(vss.ShadowCopyError, match="not on volume"):
        vss.map_into_snapshot(r"D:\data\a.txt", "C:\\", r"\Device\HarddiskVolumeShadowCopy1")


def test_volume_of_extracts_the_drive():
    assert vss.volume_of(r"C:\Users\alice") == "C:\\"
    assert vss.volume_of(r"\\?\D:\data\x") == "D:\\"


def test_volume_of_refuses_a_path_with_no_drive():
    with pytest.raises(vss.ShadowCopyError, match="cannot determine the volume"):
        vss.volume_of("/home/alice")


def test_the_shadow_id_is_parsed_out_of_the_create_output():
    output = "0\r\n{B1C2D3E4-1111-2222-3333-444455556666}\r\n"
    assert vss._parse_shadow_id(output) == "{B1C2D3E4-1111-2222-3333-444455556666}"


def test_output_without_an_id_yields_none_rather_than_a_wrong_guess():
    assert vss._parse_shadow_id("1\r\n\r\n") is None


def test_creating_a_snapshot_off_windows_is_refused_not_attempted():
    if vss.is_windows():
        pytest.skip("this asserts the non-Windows guard")
    with pytest.raises(vss.ShadowCopyError, match="Windows feature"):
        vss.create("C:\\")


def test_elevation_is_reported_as_false_off_windows():
    if vss.is_windows():
        pytest.skip("this asserts the non-Windows guard")
    assert vss.is_elevated() is False


def test_the_device_wmi_actually_returns_is_not_prefixed_twice():
    r"""Win32_ShadowCopy.DeviceObject comes back already prefixed.

    It is the same string vssadmin prints:
    \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy3. Prepending the prefix
    unconditionally produced \\?\GLOBALROOT\\?\GLOBALROOT\Device\..., and
    every open under it failed with "the system cannot find the path specified"
    -- on a 217 GiB profile, once per file.

    The old test passed because it fed the bare \Device\... form the code
    assumed, so the test and the bug shared an assumption and neither was
    checked against Windows. Both forms are pinned here now.
    """
    expected = r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy3\Users\a\f.txt"
    for device in (
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy3",   # what WMI returns
        r"\Device\HarddiskVolumeShadowCopy3",                 # what the docs show
        r"\\?\globalroot\Device\HarddiskVolumeShadowCopy3",   # case varies
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy3\\", # trailing separator
    ):
        mapped = vss.map_into_snapshot(r"C:\Users\a\f.txt", "C:\\", device)
        assert mapped == expected, device
        assert mapped.count("GLOBALROOT") == 1


def test_an_unreadable_snapshot_is_rejected_before_the_capture_uses_it(tmp_path):
    """A snapshot whose paths do not resolve is worse than none: every open
    fails and the capture writes almost nothing while reporting it one warning
    per file. One stat against a path known to exist catches that up front."""
    shadow = vss.ShadowCopy(
        volume="C:\\", shadow_id="{x}", device=r"\Device\HarddiskVolumeShadowCopyNope"
    )
    assert vss.usable_for(shadow, r"C:\Users\alice") is False

    # And a mapping that does resolve is accepted. usable_for only asks whether
    # the mapped path can be stat'ed, so a stand-in that maps onto a real
    # directory exercises the accepting branch without needing Windows.
    real = tmp_path / "profile"
    real.mkdir()

    class Workable:
        def map(self, path):
            return str(real)

    assert vss.usable_for(Workable(), real) is True
