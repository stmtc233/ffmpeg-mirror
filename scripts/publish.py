#!/usr/bin/env python3
"""Full packages live in GitHub Releases; only volumes go to EdgeOne."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from bundle import verify_bundle
from sync_ffmpeg import discover, digest, release_tag, split_archive, sync, verify_catalog


def project_name(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise ValueError("Invalid EdgeOne project name")
    return value


def marker_name(project):
    # Keep project identity and all deployment addresses out of public assets.
    key = hashlib.sha256(("china:" + project_name(project)).encode()).hexdigest()[:24]
    return f"deployment-{key}.json"


def gh(repo, *args, optional=False):
    result = subprocess.run(["gh", *args, "--repo", repo], text=True, capture_output=True)
    if result.returncode:
        if optional:
            return None
        raise RuntimeError(result.stderr.strip() or "GitHub operation failed")
    return result.stdout


def outputs(tag, deploy):
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as output:
            output.write(f"tag={tag}\ndeploy={'true' if deploy else 'false'}\n")
    print(f"Release: {tag}; deploy required: {deploy}", flush=True)


def prepare(repo, work, project, force=False):
    project_name(project)
    work.mkdir(parents=True, exist_ok=True)
    releases = discover()
    tag = release_tag(releases)
    existing = gh(repo, "release", "view", tag, "--json", "isDraft", optional=True)
    if existing is None or json.loads(existing)["isDraft"]:
        site, packages = work / "site", work / "packages"
        sync(site, packages, releases)
        catalog = json.loads((site / "catalog.json").read_text())
        shutil.copyfile(site / "catalog.json", packages / "catalog.json")
        sums = [f"{a['sha256']}  {a['filename']}" for r in catalog["platforms"].values() for a in r["artifacts"]]
        (packages / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        notes = work / "release-notes.md"
        notes.write_text("FFmpeg stable packages: TAR+XZ, LZMA2, 128 MiB dictionary.\n\n"
            "All original upstream files are retained. catalog.json records upstream hashes, "
            "individual file hashes, package hashes and <=10 MB volume hashes.\n")
        if existing is None:
            gh(repo, "release", "create", tag, "--draft", "--title", tag,
               "--target", os.environ.get("GITHUB_SHA", "main"), "--notes-file", str(notes))
        assets = [packages / "catalog.json", packages / "SHA256SUMS"]
        assets += [packages / a["filename"] for r in catalog["platforms"].values() for a in r["artifacts"]]
        gh(repo, "release", "upload", tag, *map(str, assets), "--clobber")
        gh(repo, "release", "edit", tag, "--draft=false", "--latest")
        outputs(tag, True)
        return
    marker_dir = work / "marker"
    marker_dir.mkdir(exist_ok=True)
    marker = marker_dir / marker_name(project)
    marker.unlink(missing_ok=True)
    gh(repo, "release", "download", tag, "--pattern", marker.name, "--dir", str(marker_dir), optional=True)
    deployed = marker.exists() and json.loads(marker.read_text()).get("releaseTag") == tag
    outputs(tag, force or not deployed)


def materialize(repo, tag, work):
    if not re.fullmatch(r"ffmpeg-[a-f0-9]{24}", tag):
        raise ValueError("Invalid release tag")
    packages, site = work / "downloaded", work / "site"
    packages.mkdir(parents=True, exist_ok=True)
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    gh(repo, "release", "download", tag, "--pattern", "catalog.json", "--dir", str(packages), "--clobber")
    catalog = json.loads((packages / "catalog.json").read_text())
    if catalog.get("schemaVersion") != 2 or catalog.get("releaseTag") != tag:
        raise ValueError("Release catalog mismatch")
    for platform, release in catalog["platforms"].items():
        for artifact in release["artifacts"]:
            filename = artifact["filename"]
            if filename != f"ffmpeg-{release['version']}-{platform}.tar.xz" or Path(filename).name != filename:
                raise ValueError("Invalid release package filename")
            target = packages / filename
            gh(repo, "release", "download", tag, "--pattern", filename, "--dir", str(packages), "--clobber")
            if target.stat().st_size != artifact["size"] or digest(target) != artifact["sha256"]:
                raise ValueError("Release package checksum mismatch")
            verify_bundle(target, artifact["files"])
            relative = f"ffmpeg/{platform}/{release['version']}/{artifact['sha256']}"
            parts = split_archive(target, site / relative, relative)
            if parts != artifact["parts"]:
                raise ValueError("Generated volumes differ from release manifest")
    shutil.copyfile(packages / "catalog.json", site / "catalog.json")
    verify_catalog(site, catalog)
    return site


def deploy(repo, tag, work, project):
    project_name(project)
    token = os.environ.get("EDGEONE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("请在仓库 Actions Secrets 中设置 EDGEONE_API_TOKEN，然后重新运行工作流。")
    site = materialize(repo, tag, work)
    config = {"headers": [
        {"source": "/catalog.json", "headers": [{"key": "Cache-Control", "value": "no-store"}]},
        {"source": "/ffmpeg/*", "headers": [{"key": "Cache-Control", "value": "public, max-age=31536000, immutable"}]},
    ]}
    (site / "edgeone.json").write_text(json.dumps(config, indent=2) + "\n")
    (site / "package.json").write_text('{"name":"ffmpeg-mirror-static","private":true}\n')
    cli = Path("node_modules/.bin/edgeone").resolve()
    env = {**os.environ, "EDGEONE_PAGES_API_TOKEN": token, "EDGEONE_PAGES_API_REGION": "china", "NO_COLOR": "1"}
    result = subprocess.run([str(cli), "makers", "deploy", ".", "--name", project,
        "--env", "production", "--site", "china", "--json", "--skip-ai-gateway-sync"],
        cwd=site, env=env, capture_output=True, text=True)
    if result.returncode:
        # Provider output can contain custom domains and signed access URLs.
        # Inspect detailed errors in the provider console, not public CI logs.
        raise RuntimeError(f"EdgeOne deployment failed (exit {result.returncode}); check the provider console.")
    final = None
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("status") == "success":
            final = value
    if not final or not final.get("url"):
        raise RuntimeError("CLI did not return a confirmed deployment URL")
    marker = work / marker_name(project)
    marker.write_text(json.dumps({"releaseTag": tag,
        "deployedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2) + "\n")
    gh(repo, "release", "upload", tag, str(marker), "--clobber")
    print("EdgeOne deployment completed.", flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as summary:
            summary.write("EdgeOne deployment completed. View access details in the provider console.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "deploy"])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), required=not bool(os.environ.get("GITHUB_REPOSITORY")))
    parser.add_argument("--work", type=Path, default=Path(".build"))
    parser.add_argument("--project", default=os.environ.get("EDGEONE_PROJECT_NAME", "ffmpeg-mirror"))
    parser.add_argument("--tag")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.repo, args.work, args.project, args.force)
    else:
        deploy(args.repo, args.tag or "", args.work, args.project)
