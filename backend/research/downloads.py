"""Bounded, content-addressed downloads for offline research.

Raw response bytes are immutable. Request indexes may point at a newer revision
only when refresh=True is explicitly requested. Credentials never enter paths,
metadata, or exception messages.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class DownloadRequestError(RuntimeError):
    """Sanitized transport failure with an optional numeric HTTP status."""

    def __init__(self, *, http_status=None):
        if http_status is not None and (isinstance(http_status, bool)
                                        or not isinstance(http_status, int)
                                        or not 100 <= http_status <= 599):
            raise ValueError("Download failure HTTP status must be a numeric status code")
        self.http_status = http_status
        detail = f"HTTP {http_status}" if http_status is not None else "network or timeout"
        super().__init__(f"Historical source request failed ({detail})")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_url(url: str) -> str:
    """Remove authentication values while retaining a reproducible request key."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("Research downloads require an HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Credentials in URL authority are not supported")
    sensitive = {"apikey", "api_key", "token", "access_token", "key", "authorization"}
    query = [(k, "REDACTED" if k.lower() in sensitive else v)
             for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urllib.parse.urlencode(sorted(query)), ""))


@dataclass(frozen=True)
class DownloadedResource:
    path: Path
    source_url: str
    retrieved_at: str
    sha256: str
    size_bytes: int
    cache_hit: bool

    def read_bytes(self) -> bytes:
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("Research cache content hash mismatch")
        return data

    def provenance(self) -> dict:
        return {"source_url": self.source_url, "retrieved_at": self.retrieved_at,
                "data_revision": self.sha256, "raw_size_bytes": self.size_bytes}


def _http_transport(url: str, headers: dict, timeout: float, max_bytes: int):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError("Research download exceeds configured byte limit")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Research download exceeds configured byte limit")
        return data, dict(response.headers)


class CachedDownloader:
    def __init__(self, root, *, timeout=30.0, retries=1,
                 max_bytes=100 * 1024 * 1024, transport: Callable | None = None):
        self.root = Path(root)
        self.timeout = float(timeout)
        self.retries = min(max(int(retries), 0), 2)
        self.max_bytes = int(max_bytes)
        self.transport = transport or _http_transport
        for directory in ("raw", "index", "metadata"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def cached(self, url: str) -> DownloadedResource | None:
        key = hashlib.sha256(safe_url(url).encode()).hexdigest()
        path = self.root / "index" / (key + ".json")
        if not path.exists():
            return None
        metadata = json.loads(path.read_text(encoding="utf-8"))
        result = DownloadedResource(self.root / "raw" / metadata["sha256"],
                                    metadata["source_url"], metadata["retrieved_at"],
                                    metadata["sha256"], metadata["size_bytes"], True)
        result.read_bytes()
        return result

    def fetch(self, url: str, *, headers=None, refresh=False) -> DownloadedResource:
        public_url = safe_url(url)
        if not refresh:
            existing = self.cached(url)
            if existing is not None:
                return existing
        request_headers = {"User-Agent": "SportsBettingModels-Research/1.0",
                           "Accept": "*/*", **(headers or {})}
        last_status = None
        for attempt in range(self.retries + 1):
            try:
                content, response_headers = self.transport(url, request_headers,
                                                          self.timeout, self.max_bytes)
                break
            except urllib.error.HTTPError as error:
                last_status = error.code
                if error.code not in (408, 429, 500, 502, 503, 504) or attempt == self.retries:
                    raise DownloadRequestError(http_status=error.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == self.retries:
                    raise DownloadRequestError() from None
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 2))
        else:
            raise DownloadRequestError(http_status=last_status)
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise ValueError("Research download exceeds configured byte limit")
        digest = hashlib.sha256(content).hexdigest()
        raw_path = self.root / "raw" / digest
        if not raw_path.exists():
            with raw_path.open("xb") as output:
                output.write(content)
        retrieved_at = utc_now()
        request_id = hashlib.sha256(public_url.encode()).hexdigest()
        metadata = {"source_url": public_url, "retrieved_at": retrieved_at,
                    "sha256": digest, "size_bytes": len(content),
                    "etag": response_headers.get("ETag") or response_headers.get("etag"),
                    "last_modified": response_headers.get("Last-Modified") or response_headers.get("last-modified")}
        revision_path = self.root / "metadata" / (request_id + "_" + digest + ".json")
        if not revision_path.exists():
            revision_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        index_path = self.root / "index" / (request_id + ".json")
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temporary.replace(index_path)
        return DownloadedResource(raw_path, public_url, retrieved_at, digest, len(content), False)
