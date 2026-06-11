"""Tests for ccc_agent.paths: lexical path helpers and alias canonicalization.

All operations are lexical (no filesystem access): policy classification runs
on BranchFS status output, which may reference paths that no longer exist.
"""

import unittest

from ccc_agent.paths import AliasMap, is_within, normalize


class TestNormalize(unittest.TestCase):
    def test_collapses_dots_and_slashes(self):
        self.assertEqual(normalize("/storage//user/./Projects/../Projects/a"),
                         "/storage/user/Projects/a")

    def test_strips_trailing_slash(self):
        self.assertEqual(normalize("/storage/user/"), "/storage/user")

    def test_root_stays_root(self):
        self.assertEqual(normalize("/"), "/")

    def test_rejects_relative(self):
        with self.assertRaises(ValueError):
            normalize("storage/user")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            normalize("")


class TestIsWithin(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(is_within("/storage/user/Projects/a", "/storage/user"))

    def test_equal(self):
        self.assertTrue(is_within("/storage/user", "/storage/user"))

    def test_sibling_prefix_does_not_match(self):
        # /storage/user2 must not match /storage/user (path boundary rule)
        self.assertFalse(is_within("/storage/user2/x", "/storage/user"))

    def test_outside(self):
        self.assertFalse(is_within("/etc/passwd", "/storage/user"))

    def test_root_prefix(self):
        self.assertTrue(is_within("/anything", "/"))


class TestAliasMap(unittest.TestCase):
    def test_home_is_storage_user_root(self):
        # CCC layout where /home/domen is the same data as /storage/user
        amap = AliasMap.for_home("domen", home_subdir="")
        self.assertEqual(amap.canonicalize("/home/domen/Projects/a/x.py"),
                         "/storage/user/Projects/a/x.py")

    def test_home_is_subdir_of_storage_user(self):
        # CCC layout where /home/domen is /storage/user/domen
        amap = AliasMap.for_home("domen", home_subdir="domen")
        self.assertEqual(amap.canonicalize("/home/domen/.bashrc"),
                         "/storage/user/domen/.bashrc")

    def test_exact_alias_root(self):
        amap = AliasMap.for_home("domen", home_subdir="")
        self.assertEqual(amap.canonicalize("/home/domen"), "/storage/user")

    def test_non_alias_path_unchanged(self):
        amap = AliasMap.for_home("domen", home_subdir="")
        self.assertEqual(amap.canonicalize("/storage/group/x"), "/storage/group/x")

    def test_sibling_user_home_not_translated(self):
        amap = AliasMap.for_home("domen", home_subdir="")
        self.assertEqual(amap.canonicalize("/home/domenX/file"), "/home/domenX/file")

    def test_longest_prefix_wins(self):
        amap = AliasMap({
            "/home/domen": "/storage/user",
            "/home/domen/scratch": "/storage/local/ssd/domen",
        })
        self.assertEqual(amap.canonicalize("/home/domen/scratch/t.bin"),
                         "/storage/local/ssd/domen/t.bin")
        self.assertEqual(amap.canonicalize("/home/domen/other"),
                         "/storage/user/other")

    def test_canonicalize_normalizes(self):
        amap = AliasMap.for_home("domen", home_subdir="")
        self.assertEqual(amap.canonicalize("/home/domen//Projects/./a"),
                         "/storage/user/Projects/a")

    def test_scopes_canonicalized_consistently(self):
        # A scope expressed via /home and a change expressed via /storage/user
        # must land on the same canonical path.
        amap = AliasMap.for_home("domen", home_subdir="")
        scope = amap.canonicalize("/home/domen/Projects/my-project-a")
        change = amap.canonicalize("/storage/user/Projects/my-project-a/src/m.py")
        self.assertTrue(is_within(change, scope))


if __name__ == "__main__":
    unittest.main()
