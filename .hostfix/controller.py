#!/usr/bin/env python3
"""Controlled source-to-release pipeline. No production credentials are needed."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

REPO = "xiaozhu456/pojun-hostfix"
UPSTREAM = "chu0119/Pojun"
BASE_TAG = re.compile(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)$")
HOST_TAG = re.compile(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)-hostfix\.([1-9][0-9]*)$")
WORKFLOW = ".github/workflows/hostfix.yml"
ROOT = Path(__file__).resolve().parents[1]


def run(args, cwd=None, capture=False, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else None, text=True)
    return result.stdout.strip() if capture else None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def version(value):
    match = HOST_TAG.fullmatch(value)
    if match:
        return tuple(map(int, match.groups()))
    match = BASE_TAG.fullmatch(value)
    if match:
        return (*map(int, match.groups()), 0)
    raise ValueError("not a published hostfix/upstream version: " + repr(value))


def api(path, missing=False):
    # The credential is sent only to the fixed GitHub API, never to asset URLs.
    if not path.startswith("repos/"):
        raise ValueError("unsupported API path")
    headers = {"User-Agent": "pojun-hostfix", "Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    request = urllib.request.Request("https://api.github.com/" + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        raise RuntimeError("GitHub API returned HTTP " + str(error.code)) from None


def manifest_for(release):
    asset = next((a for a in release.get("assets", []) if a["name"] == "build-info.json"), None)
    if not asset:
        return None
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/" + REPO + "/releases/download/"):
        raise ValueError("manifest is outside the release channel")
    # This is a public download; do not attach the API credential.
    with urllib.request.urlopen(url, timeout=45) as response:
        return json.load(response)


def choose_version(tag, releases, patch_hash, upstream_commit, tags=(), policy_hash=""):
    if not BASE_TAG.fullmatch(tag):
        raise ValueError("upstream must be a stable three-part tag")
    base = version(tag)[:3]
    last_revision = 0
    for release in releases:
        name = release.get("tag_name", "")
        if not HOST_TAG.fullmatch(name):
            continue
        parsed = version(name)
        if parsed[:3] == base:
            last_revision = max(last_revision, parsed[3])
        if release.get("draft") or release.get("prerelease"):
            continue
        if parsed[:3] > base:
            raise ValueError("refusing to publish an older upstream version")
        if parsed[:3] == base:
            info = release.get("_manifest")
            if info and info.get("patch_sha256") == patch_hash and info.get("upstream_commit") == upstream_commit and info.get("policy_sha256", "") == policy_hash:
                required = {"SHA256SUMS", "build-info.json", "install.sh",
                            "pojun-" + name.removeprefix("v") + "-linux-amd64.zip"}
                if not required.issubset({a["name"] for a in release.get("assets", [])}):
                    raise ValueError("an existing published release is incomplete")
                return None
    for name in tags:
        if HOST_TAG.fullmatch(name) and version(name)[:3] == base:
            last_revision = max(last_revision, version(name)[3])
    return "v" + ".".join(map(str, base)) + "-hostfix." + str(last_revision + 1)


def output(**values):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(key + "=" + str(value) + "\n")
    print(json.dumps(values), flush=True)


def prepare(destination, requested_tag=""):
    if destination.exists():
        raise ValueError("candidate directory already exists")
    upstream = api("repos/" + UPSTREAM + "/releases/" +
                   ("tags/" + requested_tag if requested_tag else "latest"))
    tag = upstream["tag_name"]
    if upstream.get("draft") or upstream.get("prerelease") or not BASE_TAG.fullmatch(tag):
        raise ValueError("upstream is not a stable release")
    patch = ROOT / ".hostfix/series.patch"
    patch_hash = sha(patch)
    policy = [WORKFLOW, ".hostfix/controller.py", ".hostfix/upgrade_smoke.py",
              ".hostfix/check_test_results.py", ".hostfix/test_controller.py"]
    policy_hash = hashlib.sha256("\n".join(name + ":" + sha(ROOT / name) for name in policy).encode()).hexdigest()
    run(["git", "fetch", "--no-tags", "https://github.com/" + UPSTREAM + ".git", "tag", tag], ROOT)
    commit = run(["git", "rev-parse", tag + "^{commit}"], ROOT, True)
    releases = []
    for page in range(1, 21):
        batch = api("repos/" + REPO + "/releases?per_page=100&page=" + str(page))
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError("release history exceeded the safety bound")
    for release in releases:
        if HOST_TAG.fullmatch(release.get("tag_name", "")) and version(release["tag_name"])[:3] == version(tag)[:3] and not release.get("draft"):
            release["_manifest"] = manifest_for(release)
    tag_lines = run(["git", "ls-remote", "--tags", "https://github.com/" + REPO + ".git",
                     "refs/tags/" + tag.removeprefix("v") + "-hostfix.*",
                     "refs/tags/v" + tag.removeprefix("v") + "-hostfix.*"], ROOT, True)
    existing_tags = [line.split("refs/tags/", 1)[1] for line in tag_lines.splitlines() if "refs/tags/" in line and not line.endswith("^{}")]
    selected = choose_version(tag, releases, patch_hash, commit, existing_tags, policy_hash)
    if selected is None:
        output(build="false", upstream=tag)
        return
    run(["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks", "--shared", str(ROOT), str(destination)])
    run(["git", "config", "core.autocrlf", "false"], destination)
    run(["git", "checkout", "--detach", commit], destination)
    # Patch application is strict. Never run an automatic conflict resolver.
    run(["git", "apply", "--check", str(patch)], destination)
    run(["git", "apply", str(patch)], destination)
    workflows = destination / ".github/workflows"
    if workflows.exists():
        shutil.rmtree(workflows)
    workflows.mkdir(parents=True)
    shutil.copyfile(ROOT / WORKFLOW, destination / WORKFLOW)
    shutil.copytree(ROOT / ".hostfix", destination / ".hostfix", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    info = {"repository": REPO, "version": selected.removeprefix("v"),
            "upstream_repository": UPSTREAM, "upstream_tag": tag,
            "upstream_commit": commit, "patch_sha256": patch_hash, "policy_sha256": policy_hash}
    (destination / ".hostfix/build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    run(["git", "config", "user.name", "pojun-hostfix[bot]"], destination)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], destination)
    run(["git", "add", "-A"], destination)
    run(["git", "diff", "--cached", "--check"], destination)
    run(["git", "commit", "-m", "hostfix: prepare " + selected], destination)
    run(["git", "branch", "-f", "hostfix-candidate", "HEAD"], destination)
    output(build="true", version=selected, upstream=tag)


def package(candidate, destination):
    destination.mkdir(parents=True, exist_ok=False)
    info = json.loads((candidate / ".hostfix/build-info.json").read_text())
    name = "pojun-" + info["version"] + "-linux-amd64.zip"
    archive = candidate / "dist" / name
    with zipfile.ZipFile(archive) as zipped:
        members = {Path(n).name for n in zipped.namelist()}
        if not {"pojun", "pojun-hostd", "pojun-maint", "start.sh", "LICENSE"}.issubset(members):
            raise ValueError("release archive is incomplete")
    shutil.copyfile(archive, destination / name)
    sums = sha(archive) + "  " + name + "\n"
    (destination / "SHA256SUMS").write_text(sums, encoding="ascii")
    info["source_commit"] = run(["git", "rev-parse", "HEAD"], candidate, True)
    info["archive_sha256"] = sha(archive)
    (destination / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(candidate / "install.sh", destination / "install.sh")
    run(["git", "bundle", "create", str(destination / "candidate.bundle"), "refs/heads/hostfix-candidate"], candidate)
    (destination / "RELEASE_NOTES.md").write_text(
        "Upstream: " + UPSTREAM + " " + info["upstream_tag"] + " (`" + info["upstream_commit"] + "`)\n\n"
        "Host tool detection, installer validation, and the hostfix update channel are included.\n\n"
        "Patch SHA256: `" + info["patch_sha256"] + "`\nSource commit: `" + info["source_commit"] + "`\n",
        encoding="utf-8")


def git_auth_env():
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("publishing requires the workflow's repository token")
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    return {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth}


def publish(artifacts, source):
    info = json.loads((artifacts / "build-info.json").read_text())
    tag = "v" + info["version"]
    if info.get("repository") != REPO or not HOST_TAG.fullmatch(tag):
        raise ValueError("invalid release channel metadata")
    archive = artifacts / ("pojun-" + info["version"] + "-linux-amd64.zip")
    if sha(archive) != info["archive_sha256"]:
        raise ValueError("archive checksum mismatch")
    expected_sums = sha(archive) + "  " + archive.name + "\n"
    if (artifacts / "SHA256SUMS").read_text() != expected_sums:
        raise ValueError("checksum manifest mismatch")
    # Never overwrite an existing release, including drafts from partial uploads.
    if api("repos/" + REPO + "/releases/tags/" + tag, missing=True) is not None:
        raise ValueError("release already exists; rerun prepare for a new revision")
    latest = api("repos/" + REPO + "/releases/latest", missing=True)
    if latest and HOST_TAG.fullmatch(latest["tag_name"]) and version(latest["tag_name"]) >= version(tag):
        raise ValueError("a newer release is already published")
    run(["git", "clone", str(artifacts / "candidate.bundle"), str(source)])
    if run(["git", "rev-parse", "HEAD"], source, True) != info["source_commit"]:
        raise ValueError("source bundle does not match the verified build")
    run(["git", "tag", tag, info["source_commit"]], source)
    run(["git", "push", "https://github.com/" + REPO + ".git", "refs/tags/" + tag], source, env=git_auth_env())
    run(["gh", "release", "create", tag, "--repo", REPO, "--draft", "--verify-tag",
         "--title", tag + " · 宿主工具修复版", "--notes-file", str(artifacts / "RELEASE_NOTES.md")])
    run(["gh", "release", "upload", tag, "--repo", REPO, str(archive),
         str(artifacts / "SHA256SUMS"), str(artifacts / "build-info.json"), str(artifacts / "install.sh")])
    run(["gh", "release", "edit", tag, "--repo", REPO, "--draft=false", "--prerelease=false", "--latest"])


def heartbeat():
    state = ROOT / ".hostfix/state.json"
    now = dt.datetime.now(dt.timezone.utc)
    previous = json.loads(state.read_text()).get("maintained_at") if state.exists() else None
    if previous and now - dt.datetime.fromisoformat(previous) < dt.timedelta(days=30):
        return
    state.write_text(json.dumps({"maintained_at": now.isoformat()}, indent=2) + "\n", encoding="utf-8")
    run(["git", "config", "user.name", "pojun-hostfix[bot]"], ROOT)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], ROOT)
    run(["git", "add", ".hostfix/state.json"], ROOT)
    run(["git", "commit", "-m", "hostfix: maintain scheduled synchronization"], ROOT)
    run(["git", "push", "origin", "HEAD:refs/heads/main"], ROOT, env=git_auth_env())


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("prepare")
    command.add_argument("destination", type=Path)
    command.add_argument("--upstream-tag", default="")
    command = sub.add_parser("package")
    command.add_argument("candidate", type=Path)
    command.add_argument("destination", type=Path)
    command = sub.add_parser("publish")
    command.add_argument("artifacts", type=Path)
    command.add_argument("source", type=Path)
    sub.add_parser("heartbeat")
    args = parser.parse_args()
    if args.command == "prepare":
        if args.upstream_tag and not BASE_TAG.fullmatch(args.upstream_tag):
            parser.error("invalid upstream tag")
        prepare(args.destination.resolve(), args.upstream_tag)
    elif args.command == "package":
        package(args.candidate.resolve(), args.destination.resolve())
    elif args.command == "publish":
        publish(args.artifacts.resolve(), args.source.resolve())
    else:
        heartbeat()


if __name__ == "__main__":
    main()
