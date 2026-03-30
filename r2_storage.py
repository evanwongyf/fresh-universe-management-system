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

# ---------------------------------------------------------------------------
# Issues — one .py file per issue, variable name: issue
# ---------------------------------------------------------------------------
def load_issues_from_r2():
    from datetime import datetime
    prefix = f"{R2_PROJECT_FOLDER}/issues/"
    results = []
    for key in _list_keys(prefix):
        source = _fetch_source(key)
        if source is None:
            continue
        data = _exec_py_source(source, "issue", key)
        if isinstance(data, dict):
            data["_filename"] = key.split("/")[-1]
            results.append(data)
    results.sort(key=lambda x: datetime.strptime(x["date_published"].replace(" ", ""), "%d/%m/%y"), reverse=True)
    return results

def upload_issue_source(filename, source):
    key = f"{R2_PROJECT_FOLDER}/issues/{filename}"
    _client().put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=source.encode("utf-8"), ContentType="text/x-python")
    logger.info("Uploaded issue: %s", key)

def delete_issue(filename):
    key = f"{R2_PROJECT_FOLDER}/issues/{filename}"
    _client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    logger.info("Deleted issue: %s", key)

# ---------------------------------------------------------------------------
# Staff members — one JSON-like .py file, variable name: staff_members (list)
# Stored as a single file: staff/members.py
# ---------------------------------------------------------------------------
STAFF_KEY = f"{{project}}/staff/members.py"  # formatted at call time

def _staff_key():
    return f"{R2_PROJECT_FOLDER}/staff/members.py"

def load_staff_from_r2():
    source = _fetch_source(_staff_key())
    if source is None:
        return []
    data = _exec_py_source(source, "staff_members", _staff_key())
    return data if isinstance(data, list) else []

def save_staff_to_r2(staff_members):
    import json
    lines = ["staff_members = ["]
    for m in staff_members:
        lines.append("    {")
        for k, v in m.items():
            if k.startswith("_"):
                continue
            lines.append(f"        {json.dumps(k)}: {json.dumps(v)},")
        lines.append("    },")
    lines.append("]")
    source = "\n".join(lines) + "\n"
    _client().put_object(Bucket=R2_BUCKET_NAME, Key=_staff_key(), Body=source.encode("utf-8"), ContentType="text/x-python")
    logger.info("Saved staff members to R2")

# ---------------------------------------------------------------------------
# Staff applications page — single config file
# Variable name: staff_applications (dict)
# Stored as: staff/applications.py
# ---------------------------------------------------------------------------
def _apps_key():
    return f"{R2_PROJECT_FOLDER}/staff/applications.py"

def load_staff_applications_from_r2():
    source = _fetch_source(_apps_key())
    if source is None:
        return None
    data = _exec_py_source(source, "staff_applications", _apps_key())
    return data if isinstance(data, dict) else None

def save_staff_applications_to_r2(config):
    import json
    roles = config.get("roles", [])
    role_lines = []
    for r in roles:
        role_lines.append("        {")
        role_lines.append(f"            \"name\": {json.dumps(r['name'])},")
        role_lines.append(f"            \"description\": {json.dumps(r['description'])},")
        role_lines.append("        },")
    roles_block = "\n".join(role_lines)
    source = f"""staff_applications = {{
    "is_open": {json.dumps(config.get("is_open", True))},
    "heading": {json.dumps(config.get("heading", ""))},
    "description": {json.dumps(config.get("description", ""))},
    "apply_link": {json.dumps(config.get("apply_link", ""))},
    "roles": [
{roles_block}
    ],
}}
"""
    _client().put_object(Bucket=R2_BUCKET_NAME, Key=_apps_key(), Body=source.encode("utf-8"), ContentType="text/x-python")
    logger.info("Saved staff applications config to R2")
