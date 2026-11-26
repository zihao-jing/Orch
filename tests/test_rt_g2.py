"""Package G, golden board (docs/redesign.md PRM-5): reading, change and leak check."""
import unittest

from rt_helpers import *
from orch.runtime import golden as G
from orch.runtime.golden import module

BOARD = "# Golden board\n\n<!-- a comment -->\n\n## Rules\n- Keep tests fast.\n- Ask before deleting data.\n"


class TestReading(unittest.TestCase):
    def test_PRM_5_reads_the_board_without_comments_and_title(self):
        b = G.read_board("/g.md", BOARD, GoldenBoard(), [])
        self.assertEqual((b.path, b.words), ("/g.md", []))
        self.assertEqual(b.chars, len("## Rules\n- Keep tests fast.\n- Ask before deleting data."))

    def test_PRM_5_a_comment_is_no_change(self):
        a = G.read_board("p", BOARD, GoldenBoard(), [])
        b = G.read_board("p", BOARD.replace("a comment", "another one"), GoldenBoard(), [])
        c = G.read_board("p", BOARD + "- New rule.\n", GoldenBoard(), [])
        self.assertEqual(a.digest, b.digest)
        self.assertNotEqual(a.digest, c.digest)

    def test_PRM_5_missing_or_empty_board(self):
        for text in (None, "", "# Golden board\n\n## Boundaries\n"):
            b = G.read_board("p", text, GoldenBoard(), [])
            self.assertEqual((b.chars, b.digest, b.words), (0, "", []))

    def test_PRM_5_leak_check_finds_manager_only_words_and_names(self):
        text = BOARD + "- The manager decides scope.\n- Ask Ada first.\n- Orch restarts.\n- The real user is Bo.\n"
        b = G.read_board("p", text, GoldenBoard(leak_told="old"), ["Ada", "the"])
        self.assertEqual(b.words, ["Ada", "manager", "orch", "real user"])
        self.assertEqual(b.leak_told, "old")  # kept from the last reading

    def test_PRM_5_leak_check_needs_whole_words(self):
        b = G.read_board("p", BOARD + "- Use the managerial tone.\n- see orchestra.py\n", GoldenBoard(), [])
        self.assertEqual(b.words, [])


class TestLeak(unittest.TestCase):
    def snap(self, **kw):
        return snapshot(GoldenBoard(path="/g.md", digest="d1", chars=40, words=["manager"], **kw))

    def test_PRM_5_the_manager_is_told_once_per_version(self):
        s = self.snap()
        actions = decide(module(), s, tick(s))
        tell = assert_has(self, actions, Tell, recipient="manager", key=TextKey.GOLDEN_LEAK)
        self.assertEqual(tell.message.text.values, {"path": "/g.md", "words": ["manager"]})
        self.assertEqual(assert_has(self, actions, SetGolden).golden.leak_told, "d1")
        s2 = self.snap(leak_told="d1")
        self.assertEqual(decide(module(), s2, tick(s2)), [])
        s3 = snapshot(GoldenBoard(path="/g.md", digest="d2", words=["manager"], leak_told="d1"))
        assert_has(self, decide(module(), s3, tick(s3)), Tell, key=TextKey.GOLDEN_LEAK)

    def test_PRM_5_a_clean_board_is_left_alone_and_a_fix_is_forgotten(self):
        s = snapshot(GoldenBoard(path="/g.md", digest="d2", chars=10))
        self.assertEqual(decide(module(), s, tick(s)), [])
        s = snapshot(GoldenBoard(path="/g.md", digest="d2", chars=10, leak_told="d1"))
        self.assertEqual(assert_has(self, decide(module(), s, tick(s)), SetGolden).golden.leak_told, "")


if __name__ == "__main__":
    unittest.main()
