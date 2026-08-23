from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

from .common import QueueMutationConflict, retry, run


def _gh_error(proc: subprocess.CompletedProcess[str], what: str):
    detail = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(f"{what}: {detail[:1000]}")


def gh_get_meta(repo: str, path: str) -> dict | None:
    proc = run(["gh", "api", f"repos/{repo}/contents/{path}"], timeout=30)
    if proc.returncode == 0:
        obj = json.loads(proc.stdout)
        return obj if isinstance(obj, dict) else None
    text = proc.stderr or proc.stdout or ""
    if "HTTP 404" in text or "Not Found" in text:
        return None
    _gh_error(proc, f"GitHub GET metadata {path}")


def gh_get_text(repo: str, path: str) -> str | None:
    proc = run(
        ["gh", "api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw"],
        timeout=30,
    )
    if proc.returncode == 0:
        return proc.stdout
    text = proc.stderr or proc.stdout or ""
    if "HTTP 404" in text or "Not Found" in text:
        return None
    _gh_error(proc, f"GitHub GET {path}")


def gh_get_blob_text(repo: str, blob_sha: str, max_bytes: int = 2_000_000) -> str:
    proc = run(["gh", "api", f"repos/{repo}/git/blobs/{blob_sha}"], timeout=30)
    if proc.returncode != 0:
        _gh_error(proc, f"GitHub GET blob {blob_sha}")
    obj = json.loads(proc.stdout)
    size = int(obj.get("size") or 0)
    if size > max_bytes:
        raise ValueError(f"queue payload too large: {size} > {max_bytes} bytes")
    encoding = str(obj.get("encoding") or "")
    content = str(obj.get("content") or "")
    if encoding != "base64":
        raise RuntimeError(f"GitHub blob {blob_sha}: unsupported encoding {encoding!r}")
    raw = base64.b64decode(content.encode(), validate=False)
    if len(raw) > max_bytes:
        raise ValueError(f"queue payload too large: {len(raw)} > {max_bytes} bytes")
    return raw.decode("utf-8")


def gh_list(repo: str, path: str) -> list[dict]:
    proc = run(["gh", "api", f"repos/{repo}/contents/{path}?per_page=100"], timeout=30)
    if proc.returncode != 0:
        _gh_error(proc, f"GitHub LIST {path}")
    obj = json.loads(proc.stdout)
    if not isinstance(obj, list):
        raise RuntimeError(f"GitHub LIST {path}: expected list")
    return obj


def gh_put(repo: str, path: str, obj: Any, message: str):
    payload = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
    content = base64.b64encode(payload).decode()

    def once():
        meta = gh_get_meta(repo, path)
        args = [
            "gh", "api", "--method", "PUT", f"repos/{repo}/contents/{path}",
            "-f", f"message={message}", "-f", f"content={content}",
        ]
        if meta and meta.get("sha"):
            args += ["-f", f"sha={meta['sha']}"]
        proc = run(args, timeout=45)
        if proc.returncode != 0:
            _gh_error(proc, f"GitHub PUT {path}")
        return True

    return retry(once)


def gh_delete_immutable(repo: str, path: str, expected_sha: str, message: str):
    """Delete only if the path still references the exact blob that was selected."""
    if not expected_sha:
        raise ValueError("expected_sha is required for immutable queue deletion")

    def once():
        meta = gh_get_meta(repo, path)
        if meta is None:
            return True
        current_sha = str(meta.get("sha") or "")
        if current_sha != expected_sha:
            raise QueueMutationConflict(
                f"queue file changed before delete: {path}; selected={expected_sha} current={current_sha}"
            )
        proc = run(
            [
                "gh", "api", "--method", "DELETE", f"repos/{repo}/contents/{path}",
                "-f", f"message={message}", "-f", f"sha={expected_sha}",
            ],
            timeout=45,
        )
        if proc.returncode == 0:
            return True
        text = proc.stderr or proc.stdout or ""
        if "HTTP 404" in text or "Not Found" in text:
            return True
        # A concurrent edit may surface as a 409/422. Re-read before retrying.
        meta2 = gh_get_meta(repo, path)
        if meta2 is None:
            return True
        current2 = str(meta2.get("sha") or "")
        if current2 != expected_sha:
            raise QueueMutationConflict(
                f"queue file changed during delete: {path}; selected={expected_sha} current={current2}"
            )
        _gh_error(proc, f"GitHub DELETE {path}")

    return retry(once)
