from winmigrate.util import paths


def test_is_within_is_case_insensitive_for_windows_paths():
    assert paths.is_within(r"C:\Users\x\Documents\a.txt", r"c:\users\X\documents")
    assert paths.is_within(r"C:\Users\x", r"C:\Users\x")
    assert not paths.is_within(r"C:\Users\xy", r"C:\Users\x")


def test_is_within_does_not_match_sibling_prefixes():
    assert not paths.is_within("/home/u/Documents2", "/home/u/Documents")


def test_relative_posix_handles_windows_input_on_any_host():
    assert paths.relative_posix(r"C:\Users\x\Documents\a b", r"c:\users\X") == "Documents/a b"
    assert paths.relative_posix("/home/u/Docs/x", "/home/u") == "Docs/x"


def test_display_collapses_the_profile_root():
    assert paths.display(r"C:\Users\x\Documents", r"C:\Users\x") == "~/Documents"
    assert paths.display("/home/u", "/home/u") == "~"


def test_needs_long_path_support_at_the_classic_limit():
    assert not paths.needs_long_path_support("a" * 259)
    assert paths.needs_long_path_support("a" * 260)


def test_expand_uses_supplied_environment():
    assert paths.expand("%USERPROFILE%\\x", {"USERPROFILE": "C:\\Users\\a"}) == "C:\\Users\\a\\x"


def test_relative_within_refuses_what_relative_posix_guesses_at():
    r"""The two callers of "make this relative" want opposite things.

    Exclusion matching needs a string for every path and must not crash, so
    ``relative_posix`` falls back to stripping the drive. Deciding *where a file
    goes in the bundle* must not guess: ``D:\FFProfiles\work`` reduced to
    ``FFProfiles/work`` restores under the profile root as if it had always
    lived there.
    """
    assert paths.relative_posix(r"D:\FFProfiles\work", r"C:\Users\a") == "FFProfiles/work"
    assert paths.relative_within(r"D:\FFProfiles\work", r"C:\Users\a") is None
    assert paths.relative_within(r"C:\Users\a\Documents", r"C:\Users\a") == "Documents"
    assert paths.relative_within(r"C:\Users\a", r"C:\Users\a") == "."
    # A sibling whose name merely starts with the root's is outside it.
    assert paths.relative_within(r"C:\Users\alice2\x", r"C:\Users\alice") is None
