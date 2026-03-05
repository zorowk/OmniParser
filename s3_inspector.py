"""
S3 Image Inspector — browse images from an S3 prefix in your browser.

Usage:
    uv run s3_inspector.py <s3_prefix> [--bucket BUCKET] [--port PORT]

Examples:
    uv run s3_inspector.py screenshots/2026-03-05/
    uv run s3_inspector.py screenshots/ --bucket my-bucket --port 9000
"""

import argparse
import json
import os
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


def list_images(s3_client, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            ext = os.path.splitext(key)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                keys.append(key)
    keys.sort()
    return keys


def fetch_image(s3_client, bucket: str, key: str) -> bytes:
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>S3 Image Inspector</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 24px; background: #16213e; border-bottom: 1px solid #0f3460;
  }
  header h1 { font-size: 16px; font-weight: 600; color: #e94560; }
  .counter { font-size: 14px; color: #8899aa; font-variant-numeric: tabular-nums; }
  .key-label {
    font-size: 13px; color: #8899aa; padding: 8px 24px;
    background: #16213e; border-bottom: 1px solid #0f3460;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .viewer {
    flex: 1; display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden; min-height: 0;
  }
  .viewer img {
    max-width: 95%; max-height: 95%; object-fit: contain;
    border-radius: 4px; box-shadow: 0 4px 24px rgba(0,0,0,0.5);
    transition: opacity 0.15s ease;
  }
  .viewer img.loading { opacity: 0.3; }
  nav {
    display: flex; align-items: center; justify-content: center; gap: 16px;
    padding: 16px; background: #16213e; border-top: 1px solid #0f3460;
  }
  button {
    padding: 8px 24px; border: 1px solid #0f3460; border-radius: 6px;
    background: #1a1a2e; color: #e0e0e0; font-size: 14px; cursor: pointer;
    transition: background 0.15s;
  }
  button:hover { background: #0f3460; }
  button:disabled { opacity: 0.3; cursor: default; background: #1a1a2e; }
  .thumb-strip {
    display: flex; gap: 4px; padding: 8px 24px; background: #111;
    overflow-x: auto; border-top: 1px solid #0f3460;
  }
  .thumb-strip img {
    height: 48px; width: 64px; object-fit: cover; border-radius: 3px;
    cursor: pointer; opacity: 0.5; border: 2px solid transparent;
    transition: opacity 0.15s, border-color 0.15s; flex-shrink: 0;
  }
  .thumb-strip img.active { opacity: 1; border-color: #e94560; }
  .thumb-strip img:hover { opacity: 0.85; }
</style>
</head>
<body>
<header>
  <h1>S3 Image Inspector</h1>
  <span class="counter" id="counter"></span>
</header>
<div class="key-label" id="keyLabel"></div>
<div class="viewer">
  <img id="mainImg" src="" alt="">
</div>
<div class="thumb-strip" id="thumbStrip"></div>
<nav>
  <button id="prevBtn" onclick="go(-1)">&#8592; Prev</button>
  <button id="nextBtn" onclick="go(1)">Next &#8594;</button>
</nav>
<script>
const IMAGES = __IMAGES_JSON__;
let idx = 0;

const mainImg = document.getElementById("mainImg");
const counter = document.getElementById("counter");
const keyLabel = document.getElementById("keyLabel");
const thumbStrip = document.getElementById("thumbStrip");

function imgUrl(i) { return "/image/" + i; }

function show(i) {
  idx = i;
  mainImg.classList.add("loading");
  mainImg.onload = () => mainImg.classList.remove("loading");
  mainImg.src = imgUrl(i);
  counter.textContent = (i + 1) + " / " + IMAGES.length;
  keyLabel.textContent = IMAGES[i];
  document.getElementById("prevBtn").disabled = (i === 0);
  document.getElementById("nextBtn").disabled = (i === IMAGES.length - 1);
  document.querySelectorAll(".thumb-strip img").forEach((el, j) => {
    el.classList.toggle("active", j === i);
    if (j === i) el.scrollIntoView({ inline: "center", behavior: "smooth" });
  });
}

function go(delta) {
  const next = idx + delta;
  if (next >= 0 && next < IMAGES.length) show(next);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") go(-1);
  if (e.key === "ArrowRight") go(1);
});

// Build thumbnail strip
IMAGES.forEach((key, i) => {
  const img = document.createElement("img");
  img.src = imgUrl(i);
  img.onclick = () => show(i);
  thumbStrip.appendChild(img);
});

show(0);
</script>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    s3_client = None
    bucket = None
    image_keys: list[str] = []

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self._serve_gallery()
        elif self.path.startswith("/image/"):
            self._serve_image()
        else:
            self.send_error(404)

    def _serve_gallery(self):
        html = HTML_TEMPLATE.replace("__IMAGES_JSON__", json.dumps(self.image_keys))
        payload = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_image(self):
        try:
            index = int(self.path.split("/image/")[1])
            key = self.image_keys[index]
        except (ValueError, IndexError):
            self.send_error(404)
            return

        ext = os.path.splitext(key)[1].lower()
        mime = MIME_TYPES.get(ext, "application/octet-stream")

        data = fetch_image(self.s3_client, self.bucket, key)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # silence request logs


def main():
    parser = argparse.ArgumentParser(description="Browse S3 images in your browser")
    parser.add_argument("prefix", help="S3 key prefix to list images from")
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET"), help="S3 bucket name (default: $S3_BUCKET)")
    parser.add_argument("--port", type=int, default=8787, help="Local port (default: 8787)")
    args = parser.parse_args()

    if not args.bucket:
        parser.error("Bucket not specified — pass --bucket or set $S3_BUCKET")

    s3 = boto3.client("s3")
    print(f"Listing images in s3://{args.bucket}/{args.prefix} …")
    keys = list_images(s3, args.bucket, args.prefix)

    if not keys:
        print("No images found at that prefix.")
        return

    print(f"Found {len(keys)} image(s). Starting server on http://localhost:{args.port}")

    RequestHandler.s3_client = s3
    RequestHandler.bucket = args.bucket
    RequestHandler.image_keys = keys

    server = HTTPServer(("127.0.0.1", args.port), RequestHandler)
    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
