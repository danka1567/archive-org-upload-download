#!/usr/bin/env python3
"""
Ultra-Fast Downloader & Internet Archive (archive.org) Uploader
Downloads any M3U8, HLS stream, MP4, MKV, etc. at maximum speed
and uploads directly to the Internet Archive via S3-like API.
"""

import os
import sys
import time
import argparse
import requests
import subprocess
from pathlib import Path


# ================= CREDENTIALS =================
IA_ACCESS_KEY = os.getenv("IA_ACCESS_KEY", "F5IMxFa6iJ1y0FiP")
IA_SECRET_KEY = os.getenv("IA_SECRET_KEY", "QqfUXnfb0QDNOK9q")
# ================================================

IA_S3_BASE = "https://s3.us.archive.org"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable units."""
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i, size = 0, float(size_bytes)
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.2f} {units[i]}"


def slugify(text: str) -> str:
    """Create a safe Archive.org identifier from a string."""
    import re
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80]  # IA identifiers max 80 chars


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def high_speed_download(
    media_url: str,
    output_dir: str,
    custom_name: str = None,
    user_agent: str = None,
) -> Path:
    """
    Downloads M3U8 / HLS / MP4 / any stream using yt-dlp + aria2c
    16-thread multi-connection engine.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out_template = custom_name if custom_name else "%(title)s.%(ext)s"
    out_path_template = os.path.join(output_dir, out_template)

    print("\n" + "=" * 70)
    print("⚡ [1/2] STARTING ULTRA-FAST MULTI-THREADED MEDIA DOWNLOAD")
    print(f"   URL: {media_url}")
    print("=" * 70)

    # Primary engine: aria2c (16 connections) + yt-dlp (16 HLS fragments)
    cmd_aria2 = [
        "yt-dlp",
        "--downloader", "aria2c",
        "--downloader-args",
        "aria2c:-x 16 -s 16 -k 1M --max-connection-per-server=16 "
        "--min-split-size=1M --optimize-concurrent-downloads=true --file-allocation=none",
        "--concurrent-fragments", "16",
        "--hls-use-mpegts",
        "--retries", "10",
        "--fragment-retries", "10",
        "--no-check-certificates",
        "-o", out_path_template,
        media_url,
    ]
    if user_agent:
        cmd_aria2.extend(["--user-agent", user_agent])

    start_time = time.time()
    try:
        print("[Engine] Launching multi-threaded aria2c + yt-dlp pipeline...")
        subprocess.run(cmd_aria2, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[Warning] aria2c engine unavailable ({e}). Falling back to yt-dlp native...")
        cmd_fallback = [
            "yt-dlp",
            "--concurrent-fragments", "16",
            "--retries", "10",
            "--fragment-retries", "10",
            "-o", out_path_template,
            media_url,
        ]
        if user_agent:
            cmd_fallback.extend(["--user-agent", user_agent])
        subprocess.run(cmd_fallback, check=True)

    elapsed = time.time() - start_time

    files = [f for f in Path(output_dir).iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        print("[ERROR] No downloaded media file found!")
        sys.exit(1)

    downloaded_file = max(files, key=lambda p: p.stat().st_mtime)
    file_size = downloaded_file.stat().st_size

    print("\n✅ [DOWNLOAD COMPLETE]")
    print(f"   • Filename : {downloaded_file.name}")
    print(f"   • Filesize : {format_size(file_size)}")
    print(f"   • Duration : {elapsed:.2f}s")
    return downloaded_file


# ---------------------------------------------------------------------------
# Upload to Archive.org via S3-like API
# ---------------------------------------------------------------------------

class ProgressFileReader:
    """File wrapper that prints upload progress."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.total_size = file_path.stat().st_size
        self._file = open(file_path, "rb")
        self.bytes_read = 0
        self._last_print = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._file.read(size)
        if chunk:
            self.bytes_read += len(chunk)
            now = time.time()
            if now - self._last_print > 0.5 or self.bytes_read == self.total_size:
                pct = (self.bytes_read / self.total_size * 100) if self.total_size else 100
                print(
                    f"\r🚀 Uploading: {format_size(self.bytes_read)} / "
                    f"{format_size(self.total_size)} ({pct:.1f}%)",
                    end="",
                    flush=True,
                )
                self._last_print = now
        return chunk

    def __len__(self):
        return self.total_size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._file.close()


def upload_to_archive_org(
    access_key: str,
    secret_key: str,
    file_path: Path,
    identifier: str = None,
    title: str = None,
    description: str = "",
    subject: str = "video",
    mediatype: str = "movies",
    collection: str = "opensource_movies",
    queue_derive: bool = True,
) -> dict:
    """
    Upload a local file to archive.org via the S3-like REST API.

    Docs: https://archive.org/developers/ias3.html
    """
    if not access_key or not secret_key:
        print("[ERROR] IA_ACCESS_KEY and IA_SECRET_KEY must be set.")
        sys.exit(1)

    # Build a unique identifier if not provided
    if not identifier:
        stem = slugify(file_path.stem) or "upload"
        identifier = f"{stem}-{int(time.time())}"

    if not title:
        title = file_path.stem.replace("-", " ").replace("_", " ").title()

    upload_url = f"{IA_S3_BASE}/{identifier}/{file_path.name}"

    headers = {
        "Authorization": f"LOW {access_key}:{secret_key}",
        # Auto-create the item bucket
        "x-archive-auto-make-bucket": "1",
        # Metadata headers
        "x-archive-meta-mediatype": mediatype,
        "x-archive-meta-collection": collection,
        "x-archive-meta-title": title,
        "x-archive-meta-description": description,
        "x-archive-meta-subject": subject,
        # Derive (thumbnail, smaller versions, etc.)
        "x-archive-queue-derive": "1" if queue_derive else "0",
        "Content-Type": _guess_content_type(file_path),
        "Content-Length": str(file_path.stat().st_size),
    }

    print("\n" + "=" * 70)
    print("📤 [2/2] UPLOADING TO INTERNET ARCHIVE (archive.org)")
    print(f"   Identifier : {identifier}")
    print(f"   Title      : {title}")
    print(f"   File       : {file_path.name}  ({format_size(file_path.stat().st_size)})")
    print(f"   Upload URL : {upload_url}")
    print("=" * 70)

    start_time = time.time()
    with ProgressFileReader(file_path) as reader:
        resp = requests.put(upload_url, data=reader, headers=headers, timeout=7200)

    elapsed = time.time() - start_time
    print()  # newline after progress

    item_url = f"https://archive.org/details/{identifier}"

    if resp.status_code in (200, 201):
        print("\n" + "=" * 70)
        print("🎉 SUCCESS: FILE UPLOADED TO INTERNET ARCHIVE 🎉")
        print(f"   • Identifier : {identifier}")
        print(f"   • Item Page  : {item_url}")
        print(f"   • Direct URL : https://archive.org/download/{identifier}/{file_path.name}")
        print(f"   • Duration   : {elapsed:.2f}s")
        print("=" * 70 + "\n")
        return {"status": "success", "identifier": identifier, "url": item_url}
    else:
        print(f"\n[ERROR] Upload failed — HTTP {resp.status_code}")
        print(f"Response: {resp.text[:500]}")
        sys.exit(1)


def _guess_content_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".ts":  "video/mp2t",
        ".flv": "video/x-flv",
        ".wmv": "video/x-ms-wmv",
        ".m4v": "video/x-m4v",
    }.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download any media/m3u8 and upload to Internet Archive (archive.org)."
    )
    parser.add_argument("--url",        "-u", required=True,  help="Media/Stream URL (M3U8, HLS, MP4, MKV …)")
    parser.add_argument("--access-key", "-a", default=os.getenv("IA_ACCESS_KEY", ""), help="IA Access Key")
    parser.add_argument("--secret-key", "-s", default=os.getenv("IA_SECRET_KEY", ""), help="IA Secret Key")
    parser.add_argument("--identifier", "-i", default=None,   help="Archive.org item identifier (auto-generated if omitted)")
    parser.add_argument("--title",      "-t", default=None,   help="Item title (defaults to filename)")
    parser.add_argument("--description","-d", default="",     help="Item description")
    parser.add_argument("--subject",    "-j", default="video",help="Subject / tags (comma-separated)")
    parser.add_argument("--mediatype",  "-m", default="movies",help="Archive.org mediatype (default: movies)")
    parser.add_argument("--collection", "-c", default="opensource_movies", help="Collection (default: opensource_movies)")
    parser.add_argument("--no-derive",        action="store_true", help="Skip derivative processing (faster)")
    parser.add_argument("--name",       "-n", default=None,   help="Custom output filename")
    parser.add_argument("--user-agent",       default=None,   help="Custom User-Agent for download")
    parser.add_argument("--outdir",           default="./downloads", help="Local download directory")

    args = parser.parse_args()

    # Step 1 — Download
    downloaded_file = high_speed_download(
        media_url=args.url,
        output_dir=args.outdir,
        custom_name=args.name,
        user_agent=args.user_agent,
    )

    # Step 2 — Upload to archive.org
    upload_to_archive_org(
        access_key=args.access_key,
        secret_key=args.secret_key,
        file_path=downloaded_file,
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
