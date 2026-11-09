import io
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle
import publish
import sync_ffmpeg as mirror


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self):
        target = self.root / "ffmpeg.zip"
        with zipfile.ZipFile(target, "w") as package:
            package.writestr("bin/ffmpeg", b"binary bytes")
            package.writestr("bin/ffprobe", b"other binary bytes")
            package.writestr("LICENSE", b"original license")
        return target

    def test_bundle_is_deterministic_and_every_file_is_preserved(self):
        source = self.source()
        first, second = self.root / "first.tar.xz", self.root / "second.tar.xz"
        files = bundle.create_bundle([source], first)
        self.assertEqual(files, bundle.create_bundle([source], second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual({f["path"] for f in files}, {"ffmpeg/bin/ffmpeg", "ffmpeg/bin/ffprobe", "ffmpeg/LICENSE"})
        files[0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            bundle.verify_bundle(first, files)

    def test_rejects_symlinks_and_traversal_even_in_non_tool_files(self):
        for name, mode in [("../outside", stat.S_IFREG), ("doc", stat.S_IFLNK)]:
            source = self.root / "bad.zip"
            with zipfile.ZipFile(source, "w") as package:
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = (mode | 0o644) << 16
                package.writestr(item, b"bad")
            with self.assertRaises(ValueError):
                bundle.create_bundle([source], self.root / "bad.tar.xz")

    def test_missing_token_does_not_download_or_publish_success(self):
        with patch.dict(os.environ, {"EDGEONE_API_TOKEN": ""}), patch.object(publish, "materialize") as materialize:
            with self.assertRaisesRegex(RuntimeError, "EDGEONE_API_TOKEN"):
                publish.deploy("o/r", "tag", self.root, "ffmpeg-mirror")
            materialize.assert_not_called()

    def test_failed_cli_does_not_write_a_deployment_marker(self):
        result = types.SimpleNamespace(returncode=1, stdout="https://private.example.test",
                                       stderr="deployment failed at private.example.test with test-secret")
        output = io.StringIO()
        with patch.dict(os.environ, {"EDGEONE_API_TOKEN": "test-secret"}), \
                patch.object(publish, "materialize", return_value=self.root), \
                patch.object(publish.subprocess, "run", return_value=result), patch.object(publish, "gh") as gh, redirect_stdout(output):
            with self.assertRaises(RuntimeError) as error:
                publish.deploy("o/r", "tag", self.root, "ffmpeg-mirror")
            self.assertNotIn("private.example.test", str(error.exception) + output.getvalue())
            self.assertNotIn("test-secret", str(error.exception) + output.getvalue())
            gh.assert_not_called()
            self.assertFalse((self.root / publish.marker_name("ffmpeg-mirror")).exists())

    def test_cli_uses_china_and_environment_token_then_records_success(self):
        result = types.SimpleNamespace(returncode=0,
            stdout='log line\n{"status":"success","url":"https://example.edgeone.app","deploymentId":"d1"}\n', stderr="")
        output = io.StringIO()
        with patch.dict(os.environ, {"EDGEONE_API_TOKEN": "test-secret"}), \
                patch.object(publish, "materialize", return_value=self.root), \
                patch.object(publish.subprocess, "run", return_value=result) as run, patch.object(publish, "gh") as gh, redirect_stdout(output):
            publish.deploy("o/r", "tag", self.root, "ffmpeg-mirror")
            self.assertNotIn("test-secret", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["env"]["EDGEONE_PAGES_API_REGION"], "china")
            self.assertEqual(run.call_args.kwargs["env"]["EDGEONE_PAGES_API_TOKEN"], "test-secret")
            marker = json.loads((self.root / publish.marker_name("ffmpeg-mirror")).read_text())
            self.assertEqual(set(marker), {"releaseTag", "deployedAt"})
            self.assertNotIn("example.edgeone.app", output.getvalue() + json.dumps(marker))
            self.assertNotIn("test-secret", output.getvalue() + json.dumps(marker))
            gh.assert_called_once()

    def test_existing_release_skips_deployed_site_but_can_force_retry(self):
        def gh(repo, *args, **kwargs):
            if args[:2] == ("release", "view"):
                return '{"isDraft":false}'
            if args[:2] == ("release", "download"):
                directory = Path(args[args.index("--dir") + 1])
                (directory / publish.marker_name("ffmpeg-mirror")).write_text(json.dumps({"releaseTag": mirror.release_tag({})}))
                return ""
            self.fail("Unexpected GitHub mutation")
        with patch.object(publish, "discover", return_value={}), patch.object(publish, "gh", side_effect=gh), \
                patch.object(publish, "outputs") as outputs:
            publish.prepare("o/r", self.root, "ffmpeg-mirror")
            self.assertFalse(outputs.call_args.args[1])
            publish.prepare("o/r", self.root, "ffmpeg-mirror", force=True)
            self.assertTrue(outputs.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
