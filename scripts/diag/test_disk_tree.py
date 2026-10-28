#!/usr/bin/env python3
"""Tests for disk_tree.py. Run:  python3 test_disk_tree.py -v"""

import io
import os
import contextlib
import tempfile
import unittest

import disk_tree as dt


class DiskTreeTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory() 
        self.base = self._tmp.name
        # Layout:
        #   root/
        #     a.txt            (10 bytes)
        #     sub/
        #       b.bin          (1000 bytes)
        #       deep/
        #         c.dat        (1 byte)
        #     empty/
        #     link_to_sub -> sub
        #     .cache/skipme.txt (500 bytes)
        self.root = os.path.join(self.base, "root")
        os.makedirs(os.path.join(self.root, "sub", "deep"))
        os.makedirs(os.path.join(self.root, "empty"))
        os.makedirs(os.path.join(self.root, ".cache"))
        self._write("a.txt", 10)
        self._write(os.path.join("sub", "b.bin"), 1000)
        self._write(os.path.join("sub", "deep", "c.dat"), 1)
        self._write(os.path.join(".cache", "skipme.txt"), 500)
        os.symlink(os.path.join(self.root, "sub"),
                   os.path.join(self.root, "link_to_sub"))

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, n):
        with open(os.path.join(self.root, rel), "wb") as fh:
            fh.write(b"x" * n)

    def _child(self, node, name):
        return next(c for c in node.children if c.name == name)

    # --- sizes -----------------------------------------------------------

    def test_file_sizes_are_exact(self):
        tree = dt.scan(self.root)
        self.assertEqual(self._child(tree, "a.txt").size, 10)
        sub = self._child(tree, "sub")
        self.assertEqual(self._child(sub, "b.bin").size, 1000)

    def test_folder_size_is_sum_of_contents(self):
        tree = dt.scan(self.root)
        sub = self._child(tree, "sub")
        self.assertEqual(sub.size, 1001)
        link_size = os.lstat(os.path.join(self.root, "link_to_sub")).st_size
        # a.txt + sub + .cache + the symlink's own size (no exclude given)
        self.assertEqual(tree.size, 10 + 1001 + 500 + link_size)

    def test_root_total_matches_os_walk(self):
        expected = 0
        for dp, dn, fn in os.walk(self.root, followlinks=False):
            for f in fn:
                expected += os.lstat(os.path.join(dp, f)).st_size
        # os.walk lists the directory symlink in dn, not fn, so add it back
        expected += os.lstat(os.path.join(self.root, "link_to_sub")).st_size
        tree = dt.scan(self.root)
        self.assertEqual(tree.size, expected)

    def test_empty_folder_is_zero(self):
        tree = dt.scan(self.root)
        self.assertEqual(self._child(tree, "empty").size, 0)
        self.assertEqual(self._child(tree, "empty").file_count, 0)

    # --- counts ----------------------------------------------------------

    def test_counts(self):
        tree = dt.scan(self.root)
        # files: a.txt, b.bin, c.dat, skipme.txt  -> 4 regular files
        self.assertEqual(tree.file_count, 4)      # symlinks are not counted as files
        sub = self._child(tree, "sub")
        self.assertEqual(sub.file_count, 2)       # b.bin, c.dat
        self.assertEqual(sub.dir_count, 1)        # deep

    # --- symlinks --------------------------------------------------------

    def test_symlink_not_followed(self):
        tree = dt.scan(self.root)
        link = self._child(tree, "link_to_sub")
        self.assertEqual(link.kind, "link")
        self.assertEqual(link.children, [])
        # must not be counted as a 1001-byte folder again
        self.assertNotEqual(link.size, 1001)
        self.assertLess(link.size, 100)  # the link's own inode size

    # --- exclusions and depth -------------------------------------------

    def test_exclude_pattern_skips_entry(self):
        tree = dt.scan(self.root, exclude=(".cache",))
        names = [c.name for c in tree.children]
        self.assertNotIn(".cache", names)
        self.assertEqual(tree.size, 10 + 1001 + os.lstat(
            os.path.join(self.root, "link_to_sub")).st_size)

    def test_max_depth_keeps_sizes_complete(self):
        tree = dt.scan(self.root, max_depth=1)
        sub = self._child(tree, "sub")
        self.assertEqual(sub.size, 1001)           # still totalled
        self.assertEqual(sub.children, [])         # not expanded
        self.assertIn("max depth", sub.error)

    # --- errors ----------------------------------------------------------

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores permission bits")
    def test_unreadable_folder_is_reported_not_fatal(self):
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        self._write(os.path.join("locked", "x"), 5)
        os.chmod(locked, 0)
        try:
            errs = dt.collect_errors(dt.scan(self.root))
            self.assertTrue(any(p == locked for p, _ in errs))
        finally:
            os.chmod(locked, 0o755)

    # --- formatting ------------------------------------------------------

    def test_human_units(self):
        self.assertEqual(dt.human(10), "10 B")
        self.assertEqual(dt.human(1024), "1.00 KiB")
        self.assertEqual(dt.human(1024 ** 2 * 3), "3.00 MiB")

    def test_tree_contains_exact_byte_figure(self):
        tree = dt.scan(self.root)
        lines = dt.render_tree_lines(tree)
        joined = "\n".join(lines)
        self.assertIn("1,001 B", joined)
        # entries sort case-insensitively: .cache, a.txt, empty, link_to_sub, sub
        self.assertIn("└── [DIR] sub/", joined)
        self.assertTrue(any("[FILE] a.txt" in l and "10 B" in l for l in lines))

    def test_end_to_end_markdown_and_txt(self):
        out_md = os.path.join(self.base, "r.md")
        out_txt = os.path.join(self.base, "r.txt")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dt.main([self.root, "-o", out_md]), 0)
            self.assertEqual(dt.main([self.root, "-o", out_txt, "--exclude", ".cache"]), 0)
        md = open(out_md, encoding="utf-8").read()
        txt = open(out_txt, encoding="utf-8").read()
        self.assertIn("# Disk inventory", md)
        self.assertIn("```text", md)
        self.assertIn("| Total size (bytes) |", md)
        self.assertIn("Total size (bytes)", txt)
        self.assertIn("[FILE] b.bin", txt)

    def test_bad_path_returns_error_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dt.main([os.path.join(self.base, "nope")]), 2)


if __name__ == "__main__":
    unittest.main()
