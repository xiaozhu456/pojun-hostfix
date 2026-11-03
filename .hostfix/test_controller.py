import importlib.util
import pathlib
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("controller", pathlib.Path(__file__).with_name("controller.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def release(tag, commit="upstream", patch_hash="patch", **extra):
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "_manifest": {"upstream_commit": commit, "patch_sha256": patch_hash},
            "assets": [{"name": n} for n in ["SHA256SUMS", "build-info.json", "install.sh",
                      "pojun-" + tag.removeprefix("v") + "-linux-amd64.zip"]], **extra}


class ControllerTests(unittest.TestCase):
    def test_order_and_same_base_revision(self):
        self.assertLess(c.version("v0.4.39-hostfix.9"), c.version("0.4.39-hostfix.10"))
        self.assertLess(c.version("0.4.39-hostfix.100"), c.version("0.4.40"))
        self.assertEqual(c.choose_version("v0.4.39", [], "p", "u"), "v0.4.39-hostfix.1")
        self.assertEqual(c.choose_version("v0.4.39", [release("v0.4.39-hostfix.2")], "new", "upstream"), "v0.4.39-hostfix.3")
        self.assertEqual(c.choose_version("v0.4.39", [], "p", "u", ["v0.4.39-hostfix.7"]), "v0.4.39-hostfix.8")

    def test_idempotent_and_no_downgrade(self):
        self.assertIsNone(c.choose_version("v0.4.39", [release("v0.4.39-hostfix.1")], "patch", "upstream"))
        with self.assertRaises(ValueError):
            c.choose_version("v0.4.39", [release("v0.4.40-hostfix.1")], "patch", "upstream")
        bad = release("v0.4.39-hostfix.1")
        bad["assets"] = []
        with self.assertRaises(ValueError):
            c.choose_version("v0.4.39", [bad], "patch", "upstream")

    def test_draft_reserves_revision_but_never_satisfies_idempotence(self):
        self.assertEqual(c.choose_version("v0.4.39", [release("v0.4.39-hostfix.1", draft=True)], "patch", "upstream"), "v0.4.39-hostfix.2")

    def test_changed_verification_policy_requires_new_revision(self):
        self.assertEqual(c.choose_version("v0.4.39", [release("v0.4.39-hostfix.1")], "patch", "upstream", policy_hash="new-policy"), "v0.4.39-hostfix.2")

    def test_skipped_database_test_is_not_a_green_release(self):
        import json
        script = pathlib.Path(__file__).with_name("check_test_results.py")
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "results.jsonl"
            for action, expected in [("skip", 1), ("pass", 0), ("fail", 1)]:
                path.write_text(json.dumps({"Action": action, "Test": "TestCoreTaskLifecyclePG"}) + "\n")
                result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True)
                self.assertEqual(result.returncode, expected)

    def test_reject_untrusted_tags(self):
        for value in ["v0.4.39;id", "v0.4.39/../../x", "v0.4.39-hostfix.0", "v0.4.39-dirty"]:
            with self.assertRaises(ValueError):
                c.version(value)
        with self.assertRaises(ValueError):
            c.choose_version("v0.4.39-hostfix.1", [], "p", "u")

    def test_bad_checksum_never_reaches_publication(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "build-info.json").write_text(json.dumps({"repository": c.REPO, "version": "0.4.39-hostfix.1", "archive_sha256": "0" * 64}))
            (root / "pojun-0.4.39-hostfix.1-linux-amd64.zip").write_bytes(b"corrupt")
            with patch.object(c, "api") as api, patch.object(c, "run") as run:
                with self.assertRaises(ValueError):
                    c.publish(root, root / "source")
                api.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
