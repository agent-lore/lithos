"""Unit tests for the shared short-id-prefix machinery (task 83257ced)."""

from lithos.id_resolution import PrefixIndex, prefix_upper_bound


class TestPrefixUpperBound:
    def test_plain_hex_prefix(self):
        assert prefix_upper_bound("83257c") == "83257d"

    def test_dash_terminal_prefix_increments_to_dot(self):
        # '-' (0x2D) + 1 = '.' (0x2E): every id starting "83257ced-" sorts
        # inside ["83257ced-", "83257ced.") under BINARY collation.
        assert prefix_upper_bound("83257ced-") == "83257ced."

    def test_carry_past_max_codepoint(self):
        max_char = chr(0x10FFFF)
        assert prefix_upper_bound("ab" + max_char) == "ac"

    def test_all_max_codepoints_has_no_upper_bound(self):
        assert prefix_upper_bound(chr(0x10FFFF) * 3) is None

    def test_increment_skips_surrogate_block(self):
        # 0xD7FF + 1 lands in the surrogate range; the bound must jump to
        # 0xE000 so it stays encodable (and still upper-bounds the prefix).
        assert prefix_upper_bound(chr(0xD7FF)) == chr(0xE000)

    def test_bound_is_strictly_greater_than_prefixed_strings(self):
        prefix = "3223127a-"
        upper = prefix_upper_bound(prefix)
        assert upper is not None
        for suffix in ("", "0", "9e33-4c4c-aeeb-3a1adf6039ee", "zzzz"):
            assert prefix <= prefix + suffix < upper


class TestPrefixIndex:
    def test_match_returns_sorted_prefix_matches(self):
        index = PrefixIndex(["bbb-2", "aaa-1", "aaa-2", "ccc-1"])
        assert index.match("aaa", limit=10) == ["aaa-1", "aaa-2"]

    def test_match_respects_limit(self):
        index = PrefixIndex([f"aaa-{i}" for i in range(10)])
        assert len(index.match("aaa", limit=3)) == 3

    def test_match_prefix_equal_to_full_id(self):
        index = PrefixIndex(["aaa-1"])
        assert index.match("aaa-1", limit=5) == ["aaa-1"]

    def test_match_no_hits(self):
        index = PrefixIndex(["aaa-1"])
        assert index.match("zzz", limit=5) == []

    def test_add_is_idempotent(self):
        index = PrefixIndex()
        index.add("aaa-1")
        index.add("aaa-1")
        assert len(index) == 1
        assert index.match("aaa", limit=5) == ["aaa-1"]

    def test_discard_missing_is_noop(self):
        index = PrefixIndex(["aaa-1"])
        index.discard("bbb-1")
        assert index.match("aaa", limit=5) == ["aaa-1"]

    def test_discard_removes(self):
        index = PrefixIndex(["aaa-1", "aaa-2"])
        index.discard("aaa-1")
        assert index.match("aaa", limit=5) == ["aaa-2"]

    def test_rebuild_replaces_contents(self):
        index = PrefixIndex(["aaa-1"])
        index.rebuild(["bbb-1", "bbb-2"])
        assert index.match("aaa", limit=5) == []
        assert index.match("bbb", limit=5) == ["bbb-1", "bbb-2"]
