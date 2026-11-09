#!/usr/bin/env python3
"""Download/concatenate TAR+XZ volumes, verifying each part and the whole package."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

from sync_ffmpeg import PART_BYTES, PLATFORMS, safe_path


def read(base, relative, limit):
    safe_path(Path("/mirror"), relative)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Remote mirror must use HTTPS")
        with urllib.request.urlopen(base.rstrip("/") + "/" + relative, timeout=60) as response:
            data = response.read(limit + 1)
    else:
        with safe_path(Path(base), relative).open("rb") as source:
            data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("File exceeds size limit")
    return data


def restore(base, platform, output):
    catalog = json.loads(read(base, "catalog.json", 2 << 20))
    if catalog["schemaVersion"] != 2 or catalog["maxPartBytes"] != PART_BYTES:
        raise ValueError("Unsupported catalog schema")
    release = catalog["platforms"][platform]
    output.mkdir(parents=True, exist_ok=True)
    for artifact in release["artifacts"]:
        name = artifact["filename"]
        if Path(name).name != name or "\\" in name or not name.endswith(".tar.xz"):
            raise ValueError("Invalid archive filename")
        if not artifact["parts"] or len(artifact["parts"]) > 100:
            raise ValueError("Invalid part count")
        target = output / name
        handle, temp = tempfile.mkstemp(prefix=".download-", dir=output)
        try:
            total = 0
            digest = hashlib.sha256()
            with os.fdopen(handle, "wb") as archive:
                for part in artifact["parts"]:
                    if not 0 < part["size"] <= PART_BYTES:
                        raise ValueError("Invalid part size")
                    block = read(base, part["path"], part["size"])
                    if len(block) != part["size"] or hashlib.sha256(block).hexdigest() != part["sha256"]:
                        raise ValueError("Part checksum mismatch")
                    archive.write(block)
                    digest.update(block)
                    total += len(block)
            if total != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
                raise ValueError("Archive checksum mismatch")
            os.replace(temp, target)
            print(f"Verified: {target}")
        finally:
            Path(temp).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="public", help="Local public directory or HTTPS mirror root")
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    args = parser.parse_args()
    restore(args.base, args.platform, args.output)
