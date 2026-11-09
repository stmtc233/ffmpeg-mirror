import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_ffmpeg as mirror
from restore import restore


def fixture():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bin/ffmpeg", b"test ffmpeg binary")
        archive.writestr("bin/ffprobe", b"test ffprobe binary")
        archive.writestr("LICENSE.txt", "upstream license is preserved")
    return data.getvalue()


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def artifact(self, data, chunk=10_000_000):
        archive = self.root / "source.zip"
        archive.write_bytes(data)
        with patch.object(mirror, "PART_BYTES", chunk):
            parts = mirror.split_archive(archive, self.root / "public/ffmpeg/test", "ffmpeg/test")
        return {"filename": "source.tar.xz", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "parts": parts}

    def test_decimal_ten_mb_boundary_and_exact_reassembly(self):
        data = b"x" * 10_000_000 + b"last"
        artifact = self.artifact(data)
        self.assertEqual([p["size"] for p in artifact["parts"]], [10_000_000, 4])
        mirror.verify_artifact(self.root / "public", artifact)

    def test_detects_missing_reordered_truncated_and_corrupt_parts(self):
        artifact = self.artifact(b"abcdefghi", chunk=4)
        root = self.root / "public"
        with self.subTest("reordered"):
            reversed_artifact = dict(artifact, parts=list(reversed(artifact["parts"])))
            with self.assertRaises(ValueError):
                mirror.verify_artifact(root, reversed_artifact)
        path = root / artifact["parts"][0]["path"]
        for broken in (b"badd", b"x"):
            path.write_bytes(broken)
            with self.assertRaises(ValueError):
                mirror.verify_artifact(root, artifact)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            mirror.verify_artifact(root, artifact)

    def test_rejects_oversized_part_even_with_matching_hash(self):
        artifact = self.artifact(b"12345")
        with patch.object(mirror, "PART_BYTES", 4), self.assertRaises(ValueError):
            mirror.verify_artifact(self.root / "public", artifact)

    def test_round_trip_preserves_original_zip_and_license(self):
        data = fixture()
        artifact = self.artifact(data, chunk=50)
        catalog = {"schemaVersion": 2, "maxPartBytes": mirror.PART_BYTES,
                   "platforms": {"linux-amd64": {"artifacts": [artifact]}}}
        root = self.root / "public"
        (root / "catalog.json").write_text(json.dumps(catalog))
        output = self.root / "restored"
        restore(str(root), "linux-amd64", output)
        self.assertEqual((output / "source.tar.xz").read_bytes(), data)
        with zipfile.ZipFile(output / "source.tar.xz") as package:
            self.assertEqual(package.read("LICENSE.txt"), b"upstream license is preserved")

    def test_failed_restore_keeps_existing_archive_and_removes_partial(self):
        artifact = self.artifact(fixture(), chunk=50)
        artifact["sha256"] = "0" * 64
        root = self.root / "public"
        (root / "catalog.json").write_text(json.dumps({"schemaVersion": 2,
            "maxPartBytes": mirror.PART_BYTES, "platforms": {"linux-amd64": {"artifacts": [artifact]}}}))
        output = self.root / "restored"
        output.mkdir()
        (output / "source.tar.xz").write_bytes(b"existing archive")
        with self.assertRaises(ValueError):
            restore(str(root), "linux-amd64", output)
        self.assertEqual(list(output.iterdir()), [output / "source.tar.xz"])
        self.assertEqual((output / "source.tar.xz").read_bytes(), b"existing archive")

    def test_rejects_path_escape_and_unapproved_hosts(self):
        for path in ("../outside", "/outside", "nested/../../outside", "a\\..\\outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                mirror.safe_path(self.root, path)
        for url in ("http://www.gyan.dev/test", "https://evil.example/file",
                    "https://www.gyan.dev:8443/file", "https://user@www.gyan.dev/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                mirror.check_url(url)

    def test_zip_missing_duplicate_or_symlink_tools_are_rejected(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("ffmpeg", b"one")
            package.writestr("nested/ffmpeg", b"two")
        with self.assertRaises(ValueError):
            mirror.validate_zip(archive, ["ffmpeg"], False)
        with self.assertRaises(ValueError):
            mirror.validate_zip(archive, ["ffprobe"], False)
        with zipfile.ZipFile(archive, "w") as package:
            item = zipfile.ZipInfo("ffmpeg")
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(item, "/other/file")
        with self.assertRaises(ValueError):
            mirror.validate_zip(archive, ["ffmpeg"], False)

    def test_discovery_selects_newest_matching_pair_not_page_order(self):
        html = "\n".join(f'href="/download/{system}/{arch}/{stamp}_{version}/{tool}.zip"'
            for system in ("macos", "linux") for arch in ("amd64", "arm64")
            for stamp, version in ((100, "8.0"), (200, "9.0.2")) for tool in ("ffmpeg", "ffprobe"))
        def response(url, limit=1 << 20):
            if url.endswith("release-version"):
                return "9.0.2"
            if url.endswith(".sha256"):
                return "a" * 64 + " file.zip"
            return html
        with patch.object(mirror, "text", side_effect=response):
            releases = mirror.discover()
        self.assertEqual(set(releases), set(mirror.PLATFORMS))
        for platform in mirror.PLATFORMS[1:]:
            self.assertEqual(releases[platform]["version"], "9.0.2")
            self.assertTrue(all("/200_9.0.2/" in a["url"] for a in releases[platform]["artifacts"]))

    def test_sync_is_idempotent_and_failure_never_replaces_catalog(self):
        data = fixture()
        sha = hashlib.sha256(data).hexdigest()
        releases = {p: {"version": "9.0.2", "provider": "fixture", "homepage": "https://www.gyan.dev/",
                       "artifacts": [{"url": "https://www.gyan.dev/source.zip", "sha256": sha,
                                      "tools": ["ffmpeg", "ffprobe"]}]}
                    for p in mirror.PLATFORMS}
        def download(url, target, limit):
            target.write_bytes(data)
        root = self.root / "public"
        with patch.object(mirror, "discover", return_value=releases), \
                patch.object(mirror, "fetch", side_effect=download) as fetch, \
                patch.object(mirror, "validate_zip"):
            mirror.sync(root)
            before = (root / "catalog.json").read_bytes()
            mirror.sync(root)
            self.assertEqual(fetch.call_count, 5)
            self.assertEqual((root / "catalog.json").read_bytes(), before)
            releases["linux-arm64"]["artifacts"][0]["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                mirror.sync(root)
            self.assertEqual((root / "catalog.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
