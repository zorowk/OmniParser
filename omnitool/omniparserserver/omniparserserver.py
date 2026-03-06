"""
python -m omniparserserver --som_model_path ../../weights/icon_detect/model.pt --caption_model_name florence2 --caption_model_path ../../weights/icon_caption_florence --device cuda --BOX_TRESHOLD 0.05
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
from pathlib import PurePosixPath
from typing import Optional

import boto3
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(root_dir)
from util.omniparser import Omniparser

logger = logging.getLogger("omniparserserver")


def parse_arguments():
    parser = argparse.ArgumentParser(description="Omniparser API")
    parser.add_argument(
        "--som_model_path",
        type=str,
        default="../../weights/icon_detect/model.pt",
        help="Path to the som model",
    )
    parser.add_argument(
        "--caption_model_name",
        type=str,
        default="florence2",
        help="Name of the caption model",
    )
    parser.add_argument(
        "--caption_model_path",
        type=str,
        default="../../weights/icon_caption_florence",
        help="Path to the caption model",
    )
    parser.add_argument(
        "--device", type=str, default="cpu", help="Device to run the model"
    )
    parser.add_argument(
        "--BOX_TRESHOLD", type=float, default=0.05, help="Threshold for box detection"
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for the API")
    parser.add_argument("--port", type=int, default=8000, help="Port for the API")
    args = parser.parse_args()
    return args


args = parse_arguments()
config = vars(args)

app = FastAPI(title="OmniParser API")
omniparser = Omniparser(config)

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _download_from_s3(s3_uri: str) -> bytes:
    """Download an object from S3 given an s3://bucket/key URI."""
    bucket, key = _parse_s3_uri(s3_uri)
    resp = get_s3_client().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _image_bytes_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    path = s3_uri[len("s3://") :]
    bucket, _, key = path.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URI (missing bucket or key): {s3_uri}")
    return bucket, key


def _upload_bytes_to_s3(data: bytes, s3_uri: str, content_type: str) -> str:
    """Upload raw bytes to S3. Returns the s3:// URI."""
    bucket, key = _parse_s3_uri(s3_uri)
    get_s3_client().put_object(
        Bucket=bucket, Key=key, Body=data, ContentType=content_type
    )
    logger.info("uploaded %s to %s", content_type, s3_uri)
    return s3_uri


def _build_s3_sub_path(s3_output_dir: str, *parts: str) -> str:
    """Join an s3:// directory URI with sub-path parts (e.g. 'som_images', 'img.png')."""
    base = s3_output_dir.rstrip("/")
    return base + "/" + "/".join(parts)


def _run_parse(
    image_base64: str,
    s3_output_path: Optional[str] = None,
    image_name: Optional[str] = None,
) -> dict:
    start = time.time()
    som_image_base64, parsed_content_list = omniparser.parse(image_base64)

    if s3_output_path:
        name = image_name or "image.png"
        stem = PurePosixPath(name).stem

        som_uri = _build_s3_sub_path(s3_output_path, "som_images", name)
        som_image_s3_path = _upload_bytes_to_s3(
            base64.b64decode(som_image_base64), som_uri, "image/png"
        )
        parsed_uri = _build_s3_sub_path(
            s3_output_path, "parsed_contents", f"{stem}.json"
        )
        parsed_content_s3_path = _upload_bytes_to_s3(
            json.dumps(parsed_content_list, indent=2).encode("utf-8"),
            parsed_uri,
            "application/json",
        )
        result: dict = {
            "parsed_content_s3_path": parsed_content_s3_path,
            "som_image_s3_path": som_image_s3_path,
        }
    else:
        result: dict = {
            "parsed_content_list": parsed_content_list,
            "som_image_base64": som_image_base64,
        }

    result["latency"] = time.time() - start
    logger.info("parse completed in %.3fs", result["latency"])
    return result


# ---------------------------------------------------------------------------
# JSON body endpoint (supports s3-path or direct base64 -encoded image)
# ---------------------------------------------------------------------------


class ParseRequest(BaseModel):
    base64_image: Optional[str] = None
    s3_path: Optional[str] = None
    s3_output_path: Optional[str] = None


@app.post("/parse/")
async def parse(parse_request: ParseRequest):
    if parse_request.base64_image and parse_request.s3_path:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of base64_image or s3_path, not both.",
        )

    image_name: Optional[str] = None

    if parse_request.base64_image:
        image_b64 = parse_request.base64_image
    elif parse_request.s3_path:
        try:
            image_bytes = _download_from_s3(parse_request.s3_path)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Failed to download from S3: {e}"
            )
        image_b64 = _image_bytes_to_base64(image_bytes)
        image_name = parse_request.s3_path.rsplit("/", 1)[-1]
    else:
        raise HTTPException(
            status_code=400, detail="Provide either base64_image or s3_path."
        )

    return _run_parse(image_b64, parse_request.s3_output_path, image_name=image_name)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health/")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    uvicorn.run("omniparserserver:app", host=args.host, port=args.port, reload=True)
