#!/usr/bin/env python3
"""Build verified FFmpeg TAR+XZ packages and <=10 MB static volumes. Stdlib only."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

from bundle import RECIPE, create_bundle

PART_BYTES = 10_000_000  # Decimal MB, deliberately smaller than 10 MiB.
MAX_ARCHIVE_BYTES = 256 << 20
MAX_BINARY_BYTES = 512 << 20
HOSTS = {"www.gyan.dev", "ffmpeg.martin-riedl.de"}
VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
SHA = re.compile(r"[a-fA-F0-9]{64}")
PLATFORMS = ("windows-amd64", "darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64")


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError("Unapproved upstream URL")


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, target, limit):
    """Bound downloads and retry into a fresh file; never disable TLS checks."""
    check_url(url)
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FFmpeg-Mirror/2"})
            with urllib.request.build_opener(Redirects()).open(request, timeout=60) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise ValueError("Upstream file exceeds size limit")
                count = 0
                deadline = time.monotonic() + 600
                with target.open("wb") as output:
                    while block := response.read(min(1 << 20, limit + 1 - count)):
                        count += len(block)
                        if count > limit or time.monotonic() > deadline:
                            raise ValueError("Upstream download exceeds size/time limit")
                        output.write(block)
                if length and count != int(length):
                    raise OSError("Incomplete upstream download")
            return
        except (OSError, ValueError):
            target.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def text(url, limit=1 << 20):
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp) / "response"
        fetch(url, target, limit)
        return target.read_text(encoding="utf-8").strip()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def checksum(url):
    fields = text(url, 4096).split()
    if not fields or not SHA.fullmatch(fields[0]):
        raise ValueError("Invalid upstream SHA-256")
    return fields[0].lower()


def discover():
    """Read one Riedl catalog so ffmpeg and ffprobe always share a build."""
    version = text("https://www.gyan.dev/ffmpeg/builds/release-version", 128)
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid Gyan stable version")
    url = f"https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{version}-essentials_build.zip"
    releases = {"windows-amd64": {
        "version": version, "provider": "Gyan", "homepage": "https://www.gyan.dev/ffmpeg/builds/",
        "artifacts": [{"url": url, "tools": ["ffmpeg", "ffprobe"], "sha256": checksum(url + ".sha256")}],
    }}
    html = text("https://ffmpeg.martin-riedl.de/")
    for platform in PLATFORMS[1:]:
        system, arch = platform.split("-")
        system = "macos" if system == "darwin" else system
        matches = re.findall(r'href="(/download/' + system + '/' + arch
                             + r'/(\d+)_(\d+\.\d+(?:\.\d+)?)/ffmpeg\.zip)"', html)
        if not matches:
            raise ValueError(f"No stable build for {platform}")
        # Do not depend on the order in which the provider renders its page.
        path, _, version = max(matches, key=lambda m: (tuple(map(int, m[2].split('.'))), int(m[1])))
        artifacts = []
        for tool in ("ffmpeg", "ffprobe"):
            tool_path = path.removesuffix("ffmpeg.zip") + tool + ".zip"
            if f'href="{tool_path}"' not in html:
                raise ValueError(f"Missing matching {tool} for {platform}")
            url = "https://ffmpeg.martin-riedl.de" + tool_path
            artifacts.append({"url": url, "tools": [tool], "sha256": checksum(url + ".sha256")})
        releases[platform] = {"version": version, "provider": "Martin Riedl",
                               "homepage": "https://ffmpeg.martin-riedl.de/", "artifacts": artifacts}
    return releases


def validate_zip(archive, tools, windows):
    """Read every entry to verify CRC; never extract/execute upstream code."""
    with zipfile.ZipFile(archive) as package:
        if sum(item.file_size for item in package.infolist()) > 2 * MAX_BINARY_BYTES:
            raise ValueError("Expanded archive is too large")
        for tool in tools:
            name = tool + (".exe" if windows else "")
            entries = [item for item in package.infolist() if PurePosixPath(item.filename).name == name]
            if len(entries) != 1:
                raise ValueError(f"Archive must contain exactly one {name}")
            item = entries[0]
            mode = item.external_attr >> 16
            if item.is_dir() or stat.S_ISLNK(mode) or item.file_size > MAX_BINARY_BYTES:
                raise ValueError("Invalid executable entry")
        if package.testzip() is not None:
            raise ValueError("Invalid ZIP CRC")


def split_archive(archive, directory, relative_dir):
    directory.mkdir(parents=True, exist_ok=True)
    parts = []
    with archive.open("rb") as source:
        while block := source.read(PART_BYTES):
            name = f"{archive.name}.part{len(parts) + 1:04d}"
            (directory / name).write_bytes(block)
            parts.append({"path": f"{relative_dir}/{name}", "size": len(block),
                          "sha256": hashlib.sha256(block).hexdigest()})
    if not parts:
        raise ValueError("Empty archive")
    return parts


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts) or "\\" in relative:
        raise ValueError("Invalid relative file path")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("File escapes public directory")
    return target


def verify_artifact(root, artifact):
    combined = hashlib.sha256()
    size = 0
    parts = artifact["parts"]
    if not parts or len(parts) > 100:
        raise ValueError("Invalid part count")
    for part in parts:
        path = safe_path(root, part["path"])
        if not 0 < part["size"] <= PART_BYTES or path.stat().st_size != part["size"]:
            raise ValueError(f"Invalid part size: {path}")
        block = path.read_bytes()
        if hashlib.sha256(block).hexdigest() != part["sha256"]:
            raise ValueError(f"Invalid part checksum: {path}")
        size += len(block)
        combined.update(block)
    if size != artifact["size"] or combined.hexdigest() != artifact["sha256"]:
        raise ValueError("Reassembled archive checksum mismatch")


def verify_catalog(root, catalog):
    if catalog["schemaVersion"] != 2 or catalog["maxPartBytes"] != PART_BYTES:
        raise ValueError("Unsupported catalog schema")
    if set(catalog["platforms"]) != set(PLATFORMS):
        raise ValueError("Incomplete platform catalog")
    for release in catalog["platforms"].values():
        for artifact in release["artifacts"]:
            verify_artifact(root, artifact)
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_size > PART_BYTES:
            raise ValueError(f"Static file exceeds 10 MB: {path}")


def release_tag(releases):
    descriptor = json.dumps({"recipe": RECIPE, "platforms": releases}, sort_keys=True, separators=(",", ":"))
    return "ffmpeg-" + hashlib.sha256(descriptor.encode()).hexdigest()[:24]


def sync(root, packages=None, releases=None):
    root.mkdir(parents=True, exist_ok=True)
    packages = packages or root.parent / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    catalog_path = root / "catalog.json"
    old = json.loads(catalog_path.read_text()) if catalog_path.exists() else {}
    releases = releases or discover()
    catalog = {"schemaVersion": 2, "maxPartBytes": PART_BYTES, "compressionRecipe": RECIPE,
               "releaseTag": release_tag(releases), "platforms": {}}
    for platform, release in releases.items():
        output = {key: value for key, value in release.items() if key != "artifacts"}
        output["sources"] = release["artifacts"]
        previous = old.get("platforms", {}).get(platform, {})
        reusable = old.get("compressionRecipe") == RECIPE and previous.get("sources") == output["sources"]
        if reusable:
            try:
                for item in previous["artifacts"]:
                    verify_artifact(root, item)
                    target = packages / item["filename"]
                    if not target.exists() or digest(target) != item["sha256"]:
                        with target.open("wb") as dest:
                            for part in item["parts"]:
                                dest.write(safe_path(root, part["path"]).read_bytes())
            except (ValueError, OSError):
                reusable = False
        if reusable:
            output["artifacts"] = previous["artifacts"]
            print(f"Unchanged: {platform}", flush=True)
        else:
            with tempfile.TemporaryDirectory() as temp:
                archives = []
                for upstream in release["artifacts"]:
                    print(f"Downloading: {platform} {upstream['url']}", flush=True)
                    archive = Path(temp) / upstream["url"].rsplit("/", 1)[1]
                    fetch(upstream["url"], archive, MAX_ARCHIVE_BYTES)
                    if digest(archive) != upstream["sha256"]:
                        raise ValueError("Downloaded archive differs from upstream SHA-256")
                    validate_zip(archive, upstream["tools"], platform.startswith("windows-"))
                    archives.append(archive)
                target = packages / f"ffmpeg-{release['version']}-{platform}.tar.xz"
                print(f"Compressing and verifying: {platform}", flush=True)
                files = create_bundle(archives, target)
                sha = digest(target)
                relative = f"ffmpeg/{platform}/{release['version']}/{sha}"
                parts = split_archive(target, root / relative, relative)
                output["artifacts"] = [{"filename": target.name, "format": "tar.xz",
                    "tools": ["ffmpeg", "ffprobe"], "size": target.stat().st_size,
                    "sha256": sha, "files": files, "parts": parts}]
        catalog["platforms"][platform] = output
    verify_catalog(root, catalog)
    # Do not create daily empty commits by changing timestamps on every poll.
    comparable = {key: value for key, value in old.items() if key != "updatedAt"}
    if catalog == comparable:
        print("Already up to date.", flush=True)
        return
    catalog["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    staging = catalog_path.with_suffix(".tmp")
    staging.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    os.replace(staging, catalog_path)
    print("Published catalog for all five platforms.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("public"))
    parser.add_argument("--packages", type=Path, default=Path("packages"))
    parser.add_argument("--verify", action="store_true", help="Verify existing data without network access")
    args = parser.parse_args()
    if args.verify:
        verify_catalog(args.output, json.loads((args.output / "catalog.json").read_text()))
    else:
        sync(args.output, args.packages)


if __name__ == "__main__":
    main()
