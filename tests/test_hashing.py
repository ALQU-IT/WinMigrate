from pathlib import Path

from winmigrate.util import hashing


def test_tree_digest_is_order_independent():
    pairs = [("b.txt", "22"), ("a/c.txt", "11")]
    assert hashing.tree_digest(pairs) == hashing.tree_digest(list(reversed(pairs)))


def test_tree_digest_changes_when_a_path_moves(tmp_path: Path):
    one = hashing.tree_digest([("a.txt", "11")])
    two = hashing.tree_digest([("sub/a.txt", "11")])
    assert one != two


def test_hash_tree_matches_manual_digest(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a").write_bytes(b"one")
    (tmp_path / "sub" / "b").write_bytes(b"two")
    digest, entries = hashing.hash_tree(tmp_path)
    assert dict(entries) == {
        "a": hashing.hash_bytes(b"one"),
        "sub/b": hashing.hash_bytes(b"two"),
    }
    assert digest == hashing.tree_digest(entries)
