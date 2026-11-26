"""Package G, golden board (docs/redesign.md PRM-5): reading and change. The critic is never pointed to the board."""
import unittest

from rt_helpers import *
from orch.runtime import golden as G

BOARD = "# Golden board\n\n<!-- a comment -->\n\n## Rules\n- Keep tests fast.\n- Ask before deleting data.\n"


class TestReading(unittest.TestCase):
    def test_PRM_5_reads_the_board_without_comments_and_title(self):
        b = G.read_board("/g.md", BOARD, GoldenBoard())
        self.assertEqual(b.path, "/g.md")
        self.assertEqual(b.chars, len("## Rules\n- Keep tests fast.\n- Ask before deleting data."))

    def test_PRM_5_a_comment_is_no_change(self):
        a = G.read_board("p", BOARD, GoldenBoard())
        b = G.read_board("p", BOARD.replace("a comment", "another one"), GoldenBoard())
        c = G.read_board("p", BOARD + "- New rule.\n", GoldenBoard())
        self.assertEqual(a.digest, b.digest)
        self.assertNotEqual(a.digest, c.digest)

    def test_PRM_5_missing_or_empty_board(self):
        for text in (None, "", "# Golden board\n\n## Boundaries\n"):
            b = G.read_board("p", text, GoldenBoard())
            self.assertEqual((b.chars, b.digest), (0, ""))

    def test_PRM_5_the_board_names_no_leak_words(self):
        b = G.read_board("p", BOARD + "- The manager decides scope.\n", GoldenBoard())
        self.assertEqual(sorted(vars(b)), ["chars", "digest", "path"])


if __name__ == "__main__":
    unittest.main()
