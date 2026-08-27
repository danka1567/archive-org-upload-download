#!/usr/bin/env python3
"""
Upload a locally downloaded file to Internet Archive (archive.org) via S3 API.
Called by GitHub Actions AFTER yt-dlp has already downloaded the file.
Usage: python upload_to_archive.py --file ./downloads/video.mp4 [options]
"""

import os
import sys
import time
import argparse
import requests
import re
from pathlib import Path

# ================= CREDENTIALS =================
IA_ACCESS_KEY = "F5IMxFa6iJ1y0FiP"
IA_SECRET_KEY  = "QqfUXnfb0QDNOK9q"
# ================================================

IA_S3_BASE = "https://s3.us.archive.org"


def format_size(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i, size = 0, float(size_bytes)
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.2f} {units[i]}"


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80]


def guess_content_type(file_path: Path) -> str:
    return {
        ".mp4":  "video/mp4",
        ".mkv":  "video/x-matroska",
        ".avi":  "video/x-msvideo",
        ".mov":  "video/quicktime",
        ".webm": "video/webm",
        ".ts":   "video/mp2t",
        ".flv":  "video/x-flv",
        ".wmv":  "video/x-ms-wmv",
        ".m4v":  "video/x-m4v",
    }.get(file_path.suffix.lower(), "application/octet-stream")


class ProgressReader:
    """File wrapper that prints upload progress."""
    def __init__(self, file_path: Path):
        self.total = file_path.stat().st_size
        self._f = open(file_path, "rb")
        self.sent = 0
        self._last = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        if chunk:
            self.sent += len(chunk)
            now = time.time()
            if now - self._last > 0.5 or self.sent == self.total:
                pct = self.sent / self.total * 100 if self.total else 100
                spd = ""
                print(f"\r🚀 Uploading: {format_size(self.sent)} / {format_size(self.total)} ({pct:.1f}%){spd}",
                      end="", flush=True)
                self._last = now
        return chunk

    def __len__(self):
        return self.total

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()


def find_downloaded_file(downloads_dir: str) -> Path:
    """Find the most recently modified file in the downloads directory."""
    d = Path(downloads_dir)
    files = [f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        print(f"[ERROR] No files found in {downloads_dir}")
        sys.exit(1)
    chosen = max(files, key=lambda p: p.stat().st_mtime)
    print(f"[INFO] Found downloaded file: {chosen.name} ({format_size(chosen.stat().st_size)})")
    return chosen


def upload(
    file_path: Path,
    identifier: str = None,
    title: str = None,
    description: str = "",
    subject: str = "video",
    mediatype: str = "movies",
    collection: str = "opensource_movies",
    queue_derive: bool = True,
):
    if not IA_ACCESS_KEY or not IA_SECRET_KEY:
        print("[ERROR] IA_ACCESS_KEY / IA_SECRET_KEY not set.")
        sys.exit(1)

    if not identifier:
        stem = slugify(file_path.stem) or "upload"
        identifier = f"{stem}-{int(time.time())}"

    if not title:
        title = file_path.stem.replace("-", " ").replace("_", " ").title()

    upload_url = f"{IA_S3_BASE}/{identifier}/{file_path.name}"

    headers = {
        "Authorization":            f"LOW {IA_ACCESS_KEY}:{IA_SECRET_KEY}",
        "x-archive-auto-make-bucket": "1",
        "x-archive-meta-mediatype": mediatype,
        "x-archive-meta-collection": collection,
        "x-archive-meta-title":     title,
        "x-archive-meta-description": description,
        "x-archive-meta-subject":   subject,
        "x-archive-queue-derive":   "1" if queue_derive else "0",
        "Content-Type":             guess_content_type(file_path),
        "Content-Length":           str(file_path.stat().st_size),
    }

    print("\n" + "=" * 70)
    print("📤 UPLOADING TO INTERNET ARCHIVE (archive.org)")
    print(f"   Identifier : {identifier}")
    print(f"   Title      : {title}")
    print(f"   File       : {file_path.name}  ({format_size(file_path.stat().st_size)})")
    print(f"   Upload URL : {upload_url}")
    print("=" * 70)

    t0 = time.time()
    with ProgressReader(file_path) as reader:
        resp = requests.put(upload_url, data=reader, headers=headers, timeout=7200)
    elapsed = time.time() - t0
    print()  # newline after progress bar

    item_url = f"https://archive.org/details/{identifier}"

    if resp.status_code in (200, 201):
        print("\n" + "=" * 70)
        print("🎉 SUCCESS: FILE UPLOADED TO INTERNET ARCHIVE")
        print(f"   • Item Page  : {item_url}")
        print(f"   • Direct URL : https://archive.org/download/{identifier}/{file_path.name}")
        print(f"   • Duration   : {elapsed:.2f}s")
        print("=" * 70)
    else:
        print(f"\n[ERROR] Upload failed — HTTP {resp.status_code}")
        print(f"Response: {resp.text[:800]}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Upload a local file to archive.org")
    parser.add_argument("--file",        "-f", default=None,  help="Path to file to upload (auto-detected from --outdir if omitted)")
    parser.add_argument("--outdir",      "-o", default="./downloads", help="Directory to scan for downloaded file")
    parser.add_argument("--identifier",  "-i", default=None,  help="archive.org identifier (auto if blank)")
    parser.add_argument("--title",       "-t", default=None,  help="Item title")
    parser.add_argument("--description", "-d", default="",    help="Item description")
    parser.add_argument("--subject",     "-j", default="video", help="Subject tags")
    parser.add_argument("--mediatype",   "-m", default="movies", help="archive.org mediatype")
    parser.add_argument("--collection",  "-c", default="opensource_movies", help="Collection")
    parser.add_argument("--no-derive",         action="store_true", help="Skip derive process")
    args = parser.parse_args()

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[ERROR] File not found: {file_path}")
            sys.exit(1)
    else:
        file_path = find_downloaded_file(args.outdir)

    upload(
        file_path=file_path,
        identifier=args.identifier,
        title=args.title,
        description=args.description,
        subject=args.subject,
        mediatype=args.mediatype,
        collection=args.collection,
        queue_derive=not args.no_derive,
    )


if __name__ == "__main__":
    main()
