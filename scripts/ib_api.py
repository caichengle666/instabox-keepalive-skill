"""InstaBox API 调用。凭证走保险库 surrogate 交换，不碰明文 key。"""
import json
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import (  # noqa: E402,F401
    add_surrogate_to_request,
    read_response_body,
    DynamicCredentialError,
)

BASE = "https://api.instabox.sh"
HOSTS = ["api.instabox.sh"]
LIFECYCLE = ("/suspend", "/resume", "/start", "/stop")


class ApiError(RuntimeError):
    pass


def call(method, path, cred, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if path.endswith(LIFECYCLE):
        req.add_header("Idempotency-Key", str(uuid.uuid4()))
    add_surrogate_to_request(req, cred, allowed_hosts=HOSTS)
    try:
        with urllib.request.build_opener().open(req, timeout=timeout) as resp:
            return json.loads(read_response_body(resp).decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}: {read_response_body(e).decode('utf-8', 'replace')}") from e
