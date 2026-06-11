"""Tests for ccc_agent.policy: change classification and commit decision."""

import unittest

from ccc_agent.paths import AliasMap
from ccc_agent.policy import (
    AUTO_COMMIT,
    NO_CHANGES,
    PENDING_REVIEW,
    ABORT,
    Change,
    PolicyConfig,
    evaluate,
    path_matches,
)

AMAP = AliasMap.for_home("domen", home_subdir="")

WORKSPACE = "/storage/user/Projects/proj-a"


def cfg(**kw):
    base = dict(mode="workspace-auto", allowed_scopes=[WORKSPACE])
    base.update(kw)
    return PolicyConfig.from_dict(base)


class TestPathMatches(unittest.TestCase):
    def test_component_pattern_matches_any_segment(self):
        self.assertTrue(path_matches(".ssh", "/home/domen/.ssh/id_rsa"))
        self.assertTrue(path_matches(".env", "/storage/user/Projects/a/.env"))
        self.assertFalse(path_matches(".env", "/storage/user/Projects/a/env"))

    def test_component_glob(self):
        self.assertTrue(path_matches("id_rsa*", "/home/d/.ssh/id_rsa.pub"))
        self.assertTrue(path_matches("*.pem", "/storage/user/certs/server.pem"))
        self.assertFalse(path_matches("*.pem", "/storage/user/certs/server.pem.txt"))

    def test_slashed_pattern_matches_anywhere(self):
        self.assertTrue(path_matches(".git/hooks", "/storage/user/p/.git/hooks"))
        # ... and everything below the matched directory
        self.assertTrue(path_matches(".git/hooks", "/storage/user/p/.git/hooks/pre-commit"))
        self.assertFalse(path_matches(".git/hooks", "/storage/user/p/.git/hooksX"))

    def test_absolute_pattern(self):
        self.assertTrue(path_matches("/storage/group/*", "/storage/group/shared.txt"))
        self.assertFalse(path_matches("/storage/group/*", "/storage/user/x"))


class TestEvaluate(unittest.TestCase):
    def test_no_changes(self):
        d = evaluate([], cfg(), AMAP)
        self.assertEqual(d.decision, NO_CHANGES)

    def test_all_in_scope_auto_commit(self):
        changes = [
            Change(op="A", path=f"{WORKSPACE}/src/new.py"),
            Change(op="M", path=f"{WORKSPACE}/README.md"),
            Change(op="D", path=f"{WORKSPACE}/old.txt"),
        ]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)
        self.assertEqual(d.out_of_scope, [])
        self.assertEqual(d.deny_matches, [])

    def test_out_of_scope_forces_review(self):
        changes = [
            Change(op="M", path=f"{WORKSPACE}/ok.py"),
            Change(op="M", path="/storage/user/.bashrc"),
        ]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(d.out_of_scope, ["/storage/user/.bashrc"])

    def test_home_alias_counts_as_in_scope(self):
        # change reported via /home alias, scope declared via /storage/user
        changes = [Change(op="A", path="/home/domen/Projects/proj-a/x.py")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)

    def test_scope_declared_via_home_alias(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/y.py")]
        d = evaluate(changes, cfg(allowed_scopes=["/home/domen/Projects/proj-a"]), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)

    def test_deny_inside_scope_forces_review(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/.env")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(len(d.deny_matches), 1)
        self.assertEqual(d.deny_matches[0].path, f"{WORKSPACE}/.env")

    def test_git_hooks_denied_by_default(self):
        changes = [Change(op="M", path=f"{WORKSPACE}/.git/hooks/pre-commit")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_normal_git_objects_allowed(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/.git/objects/ab/cdef")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)

    def test_hide_patterns_act_as_deny(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/secrets.yaml")]
        d = evaluate(changes, cfg(hide_patterns=["secrets.yaml"]), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(len(d.deny_matches), 1)

    def test_manual_mode_always_reviews(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/x.py")]
        d = evaluate(changes, cfg(mode="manual"), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_read_only_review_never_commits(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/x.py")]
        d = evaluate(changes, cfg(mode="read-only-review"), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_throwaway_aborts(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/x.py")]
        d = evaluate(changes, cfg(mode="throwaway"), AMAP)
        self.assertEqual(d.decision, ABORT)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            cfg(mode="yolo")

    def test_decision_serializable(self):
        changes = [Change(op="M", path="/storage/user/.ssh/authorized_keys")]
        d = evaluate(changes, cfg(), AMAP)
        data = d.to_dict()
        self.assertEqual(data["decision"], PENDING_REVIEW)
        self.assertEqual(data["total_changes"], 1)
        self.assertTrue(data["deny_matches"])  # .ssh is default-denied
        self.assertTrue(data["out_of_scope"])


if __name__ == "__main__":
    unittest.main()
