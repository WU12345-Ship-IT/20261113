#!/usr/bin/env python3
"""GitHub Actions remote queue batch publisher.

Accepts self-contained job archives from unique queue branches, writes a single
commit for a batch, prunes expired posts, and prepares a Pages artifact. No cloud
server, shared LAN, or GitHub PAT on the Actions runner is required.
"""
import argparse
import datetime as dt
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
POSTS = ROOT / "posts"
SITE = ROOT / "_tuwen_site_artifact"
PROCESSED = ROOT / "_tuwen_processed_refs.txt"
MAX_SITE_BYTES = 800 * 1024 * 1024  # conservative Pages artifact headroom
MAX_BATCH = 70
ID = re.compile(r"^[0-9a-f]{32}$")
PIC = re.compile(r"^images/\d{2}-[0-9a-f]{12}\.(?:png|jpe?g|gif|webp|bmp)$")


def git(*args, binary=False):
    proc = subprocess.run(["git", *args], cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        raise RuntimeError(f"git command failed: {args[0]}: " + proc.stderr.decode(errors="replace")[:2000])
    return proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")


def moment(value):
    d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return d.astimezone(dt.timezone.utc)


def site_size():
    size = 0
    if not POSTS.exists():
        return 0
    for f in POSTS.rglob("*"):
        if f.is_file():
            size += f.stat().st_size
    return size


def parse_zip(blob, expected_job):
    if len(blob) > 45 * 1024 * 1024:
        raise ValueError("archive too large")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        if len(names) > 45 or len(names) != len(set(names)):
            raise ValueError("invalid archive file list")
        allowed = {"manifest.json", "index.html"}
        total = 0
        for item in z.infolist():
            name = item.filename
            if name not in allowed and not PIC.fullmatch(name):
                raise ValueError("unexpected archive path: " + name)
            if item.is_dir() or item.file_size > 25 * 1024 * 1024:
                raise ValueError("invalid file size/type")
            total += item.file_size
        if total > 45 * 1024 * 1024:
            raise ValueError("total uncompressed bytes too large")
        manifest = json.loads(z.read("manifest.json"))
        job_id = str(manifest.get("job_id", ""))
        note_id = str(manifest.get("id", ""))
        operation = str(manifest.get("operation", ""))
        if not (ID.fullmatch(job_id) and ID.fullmatch(note_id) and job_id == expected_job):
            raise ValueError("invalid IDs")
        if operation not in ("publish", "delete"):
            raise ValueError("invalid operation")
        submitted = moment(manifest["submitted_utc"])
        expires = moment(manifest["expires_utc"])
        if expires != submitted + dt.timedelta(days=30):
            raise ValueError("unexpected expiry")
        if operation == "publish" and "index.html" not in names:
            raise ValueError("missing HTML page")
        return manifest, {n: z.read(n) for n in names if n != "manifest.json"}


def prune_expired(now):
    count = 0
    if not POSTS.exists():
        return count
    for meta in POSTS.glob("*/.tuwen.json"):
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
            if moment(d["expires_utc"]) <= now:
                shutil.rmtree(meta.parent)
                count += 1
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as ex:
            print("skipping bad expiration metadata:", meta, ex)
    return count


def apply_job(manifest, files, now):
    """Returns True when accepted. Leaves pending ref intact if capacity exhausted."""
    note_id = manifest["id"]
    op = manifest["operation"]
    target = POSTS / note_id
    if op == "delete":
        if target.exists():
            shutil.rmtree(target)
        return True
    if moment(manifest["expires_utc"]) <= now:
        # Expired while queued: it should not become a live published page.
        return False
    new_size = sum(len(content) for content in files.values()) + 500
    old_size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) if target.exists() else 0
    if site_size() - old_size + new_size > MAX_SITE_BYTES:
        print("CAPACITY LIMIT reached; keeping job queued for manual migration:", manifest["job_id"])
        return False
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path, content in files.items():
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)  # original image file bytes from ZIP, no transcoding
    (target / ".tuwen.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def gather_jobs():
    remote = git("ls-remote", "--heads", "origin", "refs/heads/tuwen-queue/*")
    ids = []
    for line in remote.splitlines():
        if "\t" not in line:
            continue
        ref = line.split("\t", 1)[1]
        if ref.startswith("refs/heads/tuwen-queue/"):
            jid = ref.rsplit("/", 1)[1]
            if ID.fullmatch(jid):
                ids.append(jid)
    if not ids:
        return []
    # Fetch job branches without ever merging their unrelated base trees into main.
    git("fetch", "--depth=2", "origin", "+refs/heads/tuwen-queue/*:refs/remotes/origin/tuwen-queue/*")
    jobs = []
    for jid in ids:
        try:
            blob = git("show", "refs/remotes/origin/tuwen-queue/" + jid + ":__tuwen_queue__/job.zip", binary=True)
            manifest, files = parse_zip(blob, jid)
            jobs.append((moment(manifest["submitted_utc"]), jid, manifest, files))
        except Exception as e:
            print("Queue job is invalid/unreadable, leaving ref for investigation:", jid, e)
    jobs.sort(key=lambda x: (x[0], x[1]))
    return jobs[:MAX_BATCH]


def build_site():
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    if POSTS.exists():
        shutil.copytree(POSTS, SITE / "posts", dirs_exist_ok=True)
    for name in ("index.html", "404.html", ".nojekyll"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, SITE / name)
    if not (SITE / "index.html").exists():
        (SITE / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>图文分享</title><p>请使用具体的图文链接访问。</p>', encoding="utf-8")
    (SITE / ".nojekyll").touch()


def write_output(changed):
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-test", action="store_true")
    args = parser.parse_args()
    if args.demo_test:
        print("module loaded")
        return
    now = dt.datetime.now(dt.timezone.utc)
    POSTs = POSTS
    POSTs.mkdir(parents=True, exist_ok=True)
    expired = prune_expired(now)
    refs = []
    for _, jid, meta, files in gather_jobs():
        if apply_job(meta, files, now):
            refs.append(jid)
    PROCESSED.write_text("\n".join(refs) + ("\n" if refs else ""), encoding="ascii")
    changed = expired > 0 or len(refs) > 0
    if changed:
        build_site()
    write_output(changed)
    print(f"expired={expired}, accepted={len(refs)}, total_site_bytes={site_size()}, changed={changed}")


if __name__ == "__main__":
    main()
