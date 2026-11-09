"""Deterministic solid TAR+XZ packaging; original file bytes are preserved."""
import hashlib
import io
import lzma
from pathlib import PurePosixPath
import stat
import tarfile
import zipfile

RECIPE = "tar-xz-lzma2-9e-128m-v1"
MAX_FILE = 512 << 20
MAX_EXPANDED = 1 << 30


def member_path(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or not path.parts or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("Unsafe archive member path")
    return path.as_posix()


def create_bundle(archives, target):
    entries = []
    seen = set()
    total = 0
    for archive in archives:
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                if item.is_dir():
                    continue
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG) or item.file_size > MAX_FILE:
                    raise ValueError("Invalid upstream archive member")
                name = member_path(archive.stem + "/" + item.filename)
                if name in seen:
                    raise ValueError("Duplicate archive member")
                seen.add(name)
                total += item.file_size
                entries.append((name, archive, item.filename, item.file_size))
    if not entries or len(entries) > 10000 or total > MAX_EXPANDED:
        raise ValueError("Expanded bundle exceeds limits")

    def order(entry):
        tool = PurePosixPath(entry[0]).name.removesuffix(".exe")
        return ({"ffmpeg": 0, "ffprobe": 1, "ffplay": 2}.get(tool, 3), entry[0])

    files = []
    filters = [{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME, "dict_size": 128 << 20}]
    with lzma.LZMAFile(target, "wb", format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, filters=filters) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for name, archive, member, size in sorted(entries, key=order):
                with zipfile.ZipFile(archive) as package:
                    data = package.read(member)  # Also verifies original ZIP CRC.
                if len(data) != size:
                    raise ValueError("Upstream member size mismatch")
                info = tarfile.TarInfo(name)
                info.size = size
                tool = PurePosixPath(name).name.removesuffix(".exe")
                info.mode = 0o755 if tool in ("ffmpeg", "ffprobe", "ffplay") else 0o644
                # TarInfo defaults uid/gid/mtime to zero; no host-specific metadata.
                tar.addfile(info, io.BytesIO(data))
                files.append({"path": name, "size": size, "mode": info.mode,
                              "sha256": hashlib.sha256(data).hexdigest()})
    verify_bundle(target, files)
    return files


def verify_bundle(archive, files):
    expected = {item["path"]: item for item in files}
    if len(expected) != len(files) or sum(item["size"] for item in files) > MAX_EXPANDED:
        raise ValueError("Invalid bundle file manifest")
    seen = set()
    with tarfile.open(archive, "r|xz") as package:
        for item in package:
            name = member_path(item.name)
            if name not in expected or name in seen or not item.isfile():
                raise ValueError("Unexpected bundle entry")
            record = expected[name]
            if item.size != record["size"] or item.size > MAX_FILE or item.mode != record["mode"]:
                raise ValueError("Bundle member metadata mismatch")
            source = package.extractfile(item)
            digest = hashlib.sha256()
            with source:
                while block := source.read(1 << 20):
                    digest.update(block)
            if digest.hexdigest() != record["sha256"]:
                raise ValueError("Bundle member SHA-256 mismatch")
            seen.add(name)
    if seen != set(expected):
        raise ValueError("Bundle is missing files")
