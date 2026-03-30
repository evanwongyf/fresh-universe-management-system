"""
r2_storage.py — Cloudflare R2 helpers for the Fresh Universe CMS.

Reads from and writes to the same bucket/folder as the main magazine app.
Raw .py source files are stored in R2, exec()'d on load to extract dicts.
"""

import os
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import logging

logger = logging.getLogger(__name__)

R2_ENDPOINT_URL      = os.environ.get("R2_ENDPOINT_URL",      "https://3be1dee2672a8baa3bd6ab00af2c79ad.r2.cloudflarestorage.com")
R2_BUCKET_NAME       = os.environ.get("R2_BUCKET_NAME",       "projects1")
R2_PROJECT_FOLDER    = os.environ.get("R2_PROJECT_FOLDER",    "fresh-universe")
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID",     "eeee7203a776e6e54bb5b17a2c53def4")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "a9dc1e444888d6569dae622b272d593501f6537b86b005b9414008adcd37e3d2")

def _client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

def ensure_project_folder():
    s3 = _client()
    marker_key = f"{R2_PROJECT_FOLDER}/"
    try:
        s3.head_object(Bucket=R2_BUCKET_NAME, Key=marker_key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            s3.put_object(Bucket=R2_BUCKET_NAME, Key=marker_key, Body=b"")
            logger.info("Created R2 project folder: %s", marker_key)

def _list_keys(prefix):
    s3 = _client()
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/"):
                keys.append(key)
    return keys

def _fetch_source(key):
    s3 = _client()
    try:
        response = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return response["Body"].read().decode("utf-8")
    except ClientError as e:
        logger.error("Failed to fetch %s: %s", key, e)
        return None

def _exec_py_source(source, variable_name, filename="<r2>"):
    namespace = {}
    try:
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    except SyntaxError as e:
        logger.error("Syntax error in %s: %s", filename, e)
        return None
    return namespace.get(variable_name)

# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_interviews_from_r2():
    from datetime import datetime
    prefix = f"{R2_PROJECT_FOLDER}/interviews/"
    results = []
    for key in _list_keys(prefix):
        source = _fetch_source(key)
        if source is None:
            continue
        data = _exec_py_source(source, "interview", key)
        if isinstance(data, dict):
            data["_filename"] = key.split("/")[-1]  # store filename for edits/deletes
            results.append(data)
    results.sort(key=lambda x: datetime.strptime(x["date"].replace(" ", ""), "%d/%m/%y"), reverse=True)
    return results

def load_blog_posts_from_r2():
    from datetime import datetime
    prefix = f"{R2_PROJECT_FOLDER}/blog/"
    results = []
    for key in _list_keys(prefix):
        source = _fetch_source(key)
        if source is None:
            continue
        data = _exec_py_source(source, "post", key)
        if isinstance(data, dict):
            data["_filename"] = key.split("/")[-1]
            results.append(data)
    results.sort(key=lambda x: datetime.strptime(x["date"].replace(" ", ""), "%d/%m/%y"), reverse=True)
    return results

# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def upload_interview_source(filename, source):
    key = f"{R2_PROJECT_FOLDER}/interviews/{filename}"
    _client().put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=source.encode("utf-8"),
        ContentType="text/x-python",
    )
    logger.info("Uploaded interview: %s", key)

def upload_blog_post_source(filename, source):
    key = f"{R2_PROJECT_FOLDER}/blog/{filename}"
    _client().put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=source.encode("utf-8"),
        ContentType="text/x-python",
    )
    logger.info("Uploaded blog post: %s", key)

# ---------------------------------------------------------------------------
# Deleters
# ---------------------------------------------------------------------------
def delete_interview(filename):
    key = f"{R2_PROJECT_FOLDER}/interviews/{filename}"
    _client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    logger.info("Deleted interview: %s", key)

def delete_blog_post(filename):
    key = f"{R2_PROJECT_FOLDER}/blog/{filename}"
    _client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    logger.info("Deleted blog post: %s", key)
