from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from guncadmirror.paths import ensure_within, release_directory, safe_component


class PathTests(unittest.TestCase):
    def test_safe_component_normalizes_and_bounds_untrusted_text(self):
        self.assertEqual(
            safe_component("  Héllo / ../../ world  ", fallback="x"),
            "Hello-..-..-world",
        )
        self.assertEqual(safe_component("..", fallback="safe"), "safe")
        self.assertEqual(safe_component("💩", fallback="safe"), "safe")
        self.assertEqual(len(safe_component("a" * 200, fallback="x")), 96)

    def test_release_directory_is_stable_and_scoped(self):
        result = release_directory(
            Path("/data"), "@channel:c", "Some Release", "a" * 96
        )
        self.assertEqual(
            result,
            Path("/data/mirror/@channel#c/Some-Release-aaaaaaaaaaaa"),
        )

    def test_ensure_within_accepts_child_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child"
            child.mkdir()
            self.assertEqual(ensure_within(root, child), child.resolve())
            with self.assertRaisesRegex(ValueError, "escapes"):
                ensure_within(root, root / ".." / "elsewhere")
