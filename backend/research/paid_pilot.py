"""One authorized CFB price pilot, with durable spend exclusion and sealed data.

This module never changes production state. Its GitHub branch contains an
up-front budget claim, encrypted append-only checkpoints, and aggregate counts.
The private decryption key never belongs in the runner environment.
"""
from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tarfile
import urllib.error
import urllib.request

from .downloads import CachedDownloader, utc_now
from .odds_history import _verify_manifest_hash, fetch_historical_requests


MANIFEST_ID = "54bbe1ee5a5bb38d29d795b7e8ee4845d84b2cab0eb8d9e5e1cc9d7d22d75e74"
MAX_CREDITS = 15240
REQUEST_COUNT = 762
BATCH_CREDITS = 500
REPOSITORY = "mmueller091/sports-betting-models"
PILOT_BRANCH = "research/cfb-2025-priced-pilot"
STATE_BRANCH = "research-state-cfb-2025-54bbe1ee5a5b"
LEDGER_NAME = "odds_fetch_ledger_" + MANIFEST_ID + ".json"
MAX_CHECKPOINT_BYTES = 256 * 1024 * 1024
ALGORITHM = "RSA-OAEP-SHA256+A256GCM"
KEY_LABEL = b"sports-betting-models/cfb-2025-paid-pilot/v1"
_HEX64 = re.compile(r"[a-f0-9]{64}")
_SHA = re.compile(r"[a-f0-9]{40}")
_CHUNK_NAME = re.compile(r"chunk-([0-9]{4})\.json")
_HALT_REASONS = {
    "authentication_or_historical_entitlement", "provider_rate_limit_or_quota",
    "provider_http_error", "network_or_timeout", "invalid_response_or_cache",
    "request_or_storage_error", "prior_failed_request_requires_review",
}


class PilotError(RuntimeError):
    """A safe, locally authored error suitable for public workflow output."""


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _crypto():
    # Optional research dependency. Importing normal replay or running the live
    # model test suite must not require cryptography to be installed.
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise PilotError("The private pilot requires its optional cryptography dependency") from None
    return hashes, serialization, padding, rsa, AESGCM


def _public_key(public_key_pem):
    _, serialization, _, rsa, _ = _crypto()
    try:
        key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError):
        raise PilotError("The pilot public key is invalid") from None
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 3072:
        raise PilotError("The pilot requires an RSA-3072 public key")
    encoded = key.public_bytes(serialization.Encoding.DER,
                               serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, hashlib.sha256(encoded).hexdigest()


def seal_payload(payload, public_key_pem, context):
    """Encrypt one compressed checkpoint. The public envelope holds no odds."""
    if not isinstance(payload, bytes) or len(payload) > MAX_CHECKPOINT_BYTES:
        raise PilotError("Checkpoint payload has an invalid size")
    hashes, _, padding, _, AESGCM = _crypto()
    key, key_id = _public_key(public_key_pem)
    header = {"version": 1, "algorithm": ALGORITHM, "key_id": key_id,
              "context": context}
    aad = _json_bytes(header)
    data_key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    wrapped = key.encrypt(data_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                               algorithm=hashes.SHA256(), label=KEY_LABEL))
    ciphertext = AESGCM(data_key).encrypt(nonce, payload, aad)
    envelope = {**header, "wrapped_key": base64.b64encode(wrapped).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode()}
    return _json_bytes(envelope) + b"\n"


def open_payload(envelope_bytes, private_key_pem, *, expected_context=None):
    """Decrypt locally; return (compressed bytes, authenticated context)."""
    hashes, serialization, padding, rsa, AESGCM = _crypto()
    try:
        envelope = json.loads(envelope_bytes)
        required = {"version", "algorithm", "key_id", "context", "wrapped_key", "nonce", "ciphertext"}
        if set(envelope) != required or envelope["version"] != 1 or envelope["algorithm"] != ALGORITHM:
            raise ValueError
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 3072:
            raise ValueError
        public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
        _, key_id = _public_key(public_pem)
        if envelope["key_id"] != key_id:
            raise ValueError
        if expected_context is not None and envelope["context"] != expected_context:
            raise ValueError
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        wrapped = base64.b64decode(envelope["wrapped_key"], validate=True)
        if len(nonce) != 12 or len(ciphertext) > MAX_CHECKPOINT_BYTES + 16 or len(wrapped) != 384:
            raise ValueError
        header = {field: envelope[field] for field in ("version", "algorithm", "key_id", "context")}
        data_key = key.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                   algorithm=hashes.SHA256(), label=KEY_LABEL))
        data = AESGCM(data_key).decrypt(nonce, ciphertext, _json_bytes(header))
        return data, envelope["context"]
    except PilotError:
        raise
    except Exception:
        # Authentication and parsing errors must not echo private material.
        raise PilotError("Encrypted checkpoint failed authentication or format validation") from None


def validate_manifest(manifest):
    if _verify_manifest_hash(manifest) != MANIFEST_ID:
        raise PilotError("This runner only permits the authorized CFB 2025 manifest")
    if (manifest.get("request_count") != REQUEST_COUNT or len(manifest["requests"]) != REQUEST_COUNT
            or manifest.get("estimated_credits") != MAX_CREDITS
            or manifest.get("regions") != ["us"] or manifest.get("markets") != ["h2h", "spreads"]
            or manifest.get("decision_minutes") != 60 or manifest.get("closing_minutes") != 5
            or any(item.get("league") != "cfb" or item.get("estimated_credits") != 20
                   for item in manifest["requests"])):
        raise PilotError("Pilot scope or credit limit does not match the authorization")


def validate_run_context(context):
    if (context.get("repository") != REPOSITORY
            or context.get("ref") != "refs/heads/" + PILOT_BRANCH
            or str(context.get("run_attempt")) != "1"
            or not re.fullmatch(r"[0-9]+", str(context.get("run_id", "")))
            or not _SHA.fullmatch(str(context.get("source_sha", "")))):
        raise PilotError("This paid pilot is restricted to its authorized repository, branch, and first run attempt")


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PilotError("GitHub returned an invalid state object identifier")
    return value


class GitHubStateStore:
    """Atomic initial claim and non-forced append-only checkpoint commits."""

    def __init__(self, token, *, transport=None):
        if not token:
            raise PilotError("GITHUB_TOKEN is required for a durable pilot budget claim")
        self._token = token
        self._transport = transport or urllib.request.urlopen
        self.head_sha = None
        self.tree_sha = None

    def _request(self, method, path, payload=None, *, expected_status=200):
        request = urllib.request.Request(
            "https://api.github.com/repos/" + REPOSITORY + path,
            data=_json_bytes(payload) if payload is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + self._token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "SportsBettingModels-CFB2025-Pilot/1.0"})
        try:
            with self._transport(request, timeout=45) as response:
                if response.status != expected_status:
                    raise PilotError("GitHub returned an unexpected state operation status")
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise PilotError("GitHub state response exceeded its safe size limit")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise PilotError("GitHub returned an invalid state response")
            return result
        except urllib.error.HTTPError as error:
            raise PilotError(f"GitHub state operation failed (HTTP {int(error.code)}); no automatic retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PilotError("GitHub state operation failed (network or timeout); no automatic retry") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise PilotError("GitHub state response was not valid JSON") from None

    def _read_head(self):
        result = self._request("GET", "/git/ref/heads/" + STATE_BRANCH)
        if result.get("ref") != "refs/heads/" + STATE_BRANCH or result.get("object", {}).get("type") != "commit":
            raise PilotError("GitHub pilot state reference did not match its requested identity")
        return _sha(result.get("object", {}).get("sha"))

    def claim(self, reservation):
        if self.head_sha is not None:
            raise PilotError("The pilot budget has already been claimed by this process")
        # No base_tree and no commit parents: this branch contains only research
        # state, never a copy of or edits to production source and live history.
        tree = self._request("POST", "/git/trees", {"tree": [
            {"path": "reservation.json", "mode": "100644", "type": "blob",
             "content": _json_bytes(reservation).decode() + "\n"}]}, expected_status=201)
        tree_sha = _sha(tree.get("sha"))
        commit = self._request("POST", "/git/commits", {
            "message": "Reserve the single authorized CFB 2025 historical odds budget",
            "tree": tree_sha, "parents": []}, expected_status=201)
        commit_sha = _sha(commit.get("sha"))
        # Create, never update or delete, the unique claim reference. Concurrent
        # jobs and retries cannot both create the same ref successfully.
        result = self._request("POST", "/git/refs", {
            "ref": "refs/heads/" + STATE_BRANCH, "sha": commit_sha}, expected_status=201)
        if result.get("ref") != "refs/heads/" + STATE_BRANCH or result.get("object", {}).get("sha") != commit_sha:
            raise PilotError("The durable pilot budget claim could not be verified")
        if self._read_head() != commit_sha:
            raise PilotError("The durable pilot budget claim moved before it was verified")
        self.head_sha, self.tree_sha = commit_sha, tree_sha
        return commit_sha

    def checkpoint(self, name, sealed, summary):
        if not self.head_sha or not _CHUNK_NAME.fullmatch(name):
            raise PilotError("A valid claimed state and checkpoint name are required")
        expected_head = self.head_sha
        if self._read_head() != expected_head:
            raise PilotError("Pilot state changed; refusing to overwrite another checkpoint")
        tree = self._request("POST", "/git/trees", {"base_tree": self.tree_sha, "tree": [
            {"path": "sealed/" + name, "mode": "100644", "type": "blob", "content": sealed.decode("utf-8")},
            {"path": "public_summary.json", "mode": "100644", "type": "blob",
             "content": _json_bytes(summary).decode() + "\n"},
        ]}, expected_status=201)
        tree_sha = _sha(tree.get("sha"))
        commit = self._request("POST", "/git/commits", {
            "message": "Save encrypted CFB historical checkpoint " + name,
            "tree": tree_sha, "parents": [expected_head]}, expected_status=201)
        commit_sha = _sha(commit.get("sha"))
        if self._read_head() != expected_head:
            raise PilotError("Pilot state changed before checkpoint publication")
        result = self._request("PATCH", "/git/refs/heads/" + STATE_BRANCH,
                               {"sha": commit_sha, "force": False})
        if result.get("object", {}).get("sha") != commit_sha or self._read_head() != commit_sha:
            raise PilotError("Encrypted checkpoint publication could not be verified")
        self.head_sha, self.tree_sha = commit_sha, tree_sha
        return commit_sha


def _allowed_cache_path(name):
    parts = PurePosixPath(name).parts
    if PurePosixPath(name).as_posix() != name or len(parts) != 3 or parts[0] != "odds_cache":
        return False
    if parts[1] == "raw":
        return bool(_HEX64.fullmatch(parts[2]))
    if parts[1] == "index":
        return bool(re.fullmatch(r"[a-f0-9]{64}\.json", parts[2]))
    if parts[1] == "metadata":
        return bool(re.fullmatch(r"[a-f0-9]{64}_[a-f0-9]{64}\.json", parts[2]))
    return False


def _validate_ledger(ledger, manifest, cap):
    requests = {item["request_id"] for item in manifest["requests"]}
    if ledger.get("manifest_id") != MANIFEST_ID or not isinstance(ledger.get("attempts"), dict):
        raise PilotError("Paid fetch returned a ledger for the wrong pilot")
    attempts = ledger["attempts"]
    if not set(attempts).issubset(requests):
        raise PilotError("Paid fetch ledger contains an unauthorized request")
    reserved = ledger.get("reserved_credits")
    if isinstance(reserved, bool) or not isinstance(reserved, int) or not 0 <= reserved <= cap:
        raise PilotError("Paid fetch ledger exceeded the current batch credit limit")
    if any(item.get("status") not in {"reserved", "cached", "downloaded", "failed"}
           or item.get("reserved_credits") not in (0, 20) for item in attempts.values()):
        raise PilotError("Paid fetch ledger has an unrecognized reservation state")
    if sum(item["reserved_credits"] for item in attempts.values()) != reserved:
        raise PilotError("Paid fetch ledger credit reservations are inconsistent")
    results = ledger.get("results")
    if not isinstance(results, list) or len({item.get("request_id") for item in results}) != len(results):
        raise PilotError("Paid fetch ledger has invalid or duplicate results")
    for result in results:
        if (result.get("request_id") not in attempts
                or attempts[result["request_id"]]["status"] not in {"cached", "downloaded"}
                or not _HEX64.fullmatch(str(result.get("sha256", "")))):
            raise PilotError("Paid fetch ledger result does not match a successful request")


def _checkpoint_payload(root, ledger, context, seen, secret_values):
    files, changed = {}, {}
    cache = root / "odds_cache"
    if cache.exists():
        for path in sorted(cache.rglob("*")):
            if path.is_symlink():
                raise PilotError("Symbolic links are forbidden in private research checkpoints")
            if path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            if not _allowed_cache_path(relative):
                raise PilotError("Unexpected cache file rejected from the private checkpoint")
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if relative.startswith("odds_cache/raw/") and digest != path.name:
                raise PilotError("Historical raw response content hash mismatch")
            if relative in seen and seen[relative] == digest:
                continue
            files[relative], changed[relative] = data, digest
    portable_ledger = copy.deepcopy(ledger)
    for result in portable_ledger["results"]:
        digest = result["sha256"]
        expected = root / "odds_cache" / "raw" / digest
        if Path(result["path"]).resolve() != expected.resolve() or not expected.is_file():
            raise PilotError("Historical response path is outside the approved cache")
        result["path"] = "odds_cache/raw/" + digest
    files[LEDGER_NAME] = _json_bytes(portable_ledger) + b"\n"
    secrets_bytes = [value.encode() for value in secret_values if value]
    if any(secret in data for data in files.values() for secret in secrets_bytes):
        raise PilotError("Secret material was rejected from the private checkpoint")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["checkpoint.json"] = _json_bytes({"context": context, "files": hashes}) + b"\n"
    if sum(len(data) for data in files.values()) > MAX_CHECKPOINT_BYTES:
        raise PilotError("Private checkpoint exceeded its safe size limit")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, 0
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(output.getvalue(), mtime=0), changed


def _summary(ledger, run_context, chunk_count):
    halted = ledger.get("halted")
    complete = len(ledger["results"]) == REQUEST_COUNT
    result = {"manifest_id": MANIFEST_ID, "run_id": str(run_context["run_id"]),
              "source_sha": run_context["source_sha"], "updated_at": utc_now(),
              "status": "halted" if halted else "complete" if complete else "in_progress",
              "planned_requests": REQUEST_COUNT, "attempted_requests": len(ledger["attempts"]),
              "successful_responses": len(ledger["results"]),
              "remaining_planned_requests": REQUEST_COUNT - len(ledger["attempts"]),
              "reserved_credits": ledger["reserved_credits"], "budget_claimed_credits": MAX_CREDITS,
              "credit_semantics": "conservative_request_reservations_not_verified_provider_charges",
              "encrypted_chunks": chunk_count, "raw_data_public": False,
              "automatic_retries": False}
    if halted:
        result["halt_reason"] = halted.get("reason") if halted.get("reason") in _HALT_REASONS else "unclassified_fetch_failure"
        status = halted.get("http_status")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            result["http_status"] = status
    return result


def run_pilot(manifest, public_key_pem, output_dir, *, run_context, api_key,
              state_store, downloader=None, fetcher=fetch_historical_requests, progress=None,
              additional_secrets=()):
    """Fetch only after a durable claim, and checkpoint before the next batch."""
    validate_manifest(manifest)
    validate_run_context(run_context)
    _, key_id = _public_key(public_key_pem)
    if not api_key:
        raise PilotError("ODDS_API_KEY is required for the approved historical price pilot")
    root = Path(output_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise PilotError("A new empty output directory is required; this one-shot runner never automatically resumes")
    root.mkdir(parents=True, exist_ok=True)
    reservation = {"manifest_id": MANIFEST_ID, "maximum_credits": MAX_CREDITS,
                   "request_count": REQUEST_COUNT, "scope": "2025_CFB_US_moneyline_spread_T60_T5",
                   "run_context": run_context, "public_key_id": key_id, "created_at": utc_now(),
                   "automatic_retries": False,
                   "recovery_policy": "Never recreate this claim or repeat unverified paid requests"}
    state_store.claim(reservation)
    # The claim remains even after failures. It is intentionally never removed.
    (root / "sealed").mkdir()
    downloader = downloader or CachedDownloader(root / "odds_cache", retries=0)
    seen, summary = {}, None
    caps = list(range(BATCH_CREDITS, MAX_CREDITS, BATCH_CREDITS)) + [MAX_CREDITS]
    for chunk_number, cap in enumerate(caps, 1):
        ledger = fetcher(manifest, root, max_credits=cap, allow_paid=True,
                         api_key=api_key, downloader=downloader)
        _validate_ledger(ledger, manifest, cap)
        context = {"manifest_id": MANIFEST_ID, "run_id": str(run_context["run_id"]),
                   "source_sha": run_context["source_sha"], "chunk_index": chunk_number,
                   "reserved_credits": ledger["reserved_credits"],
                   "previous_state_sha": state_store.head_sha}
        payload, changed = _checkpoint_payload(root, ledger, context, seen,
                                               (api_key, *additional_secrets))
        sealed = seal_payload(payload, public_key_pem, context)
        name = f"chunk-{chunk_number:04d}.json"
        with (root / "sealed" / name).open("xb") as stream:
            stream.write(sealed)
        summary = _summary(ledger, run_context, chunk_number)
        summary["latest_chunk_sha256"] = hashlib.sha256(sealed).hexdigest()
        (root / "public_summary.json").write_bytes(_json_bytes(summary) + b"\n")
        # No further paid call occurs unless this publication succeeds.
        state_store.checkpoint(name, sealed, summary)
        seen.update(changed)
        if progress is not None:
            progress(summary)
        if ledger.get("halted") or len(ledger["results"]) == REQUEST_COUNT:
            break
    return summary


def recover_chunks(chunk_dir, private_key_pem, output_dir):
    """Recover verified, allowlisted files locally and rebase ledger paths."""
    source, root = Path(chunk_dir), Path(output_dir).resolve()
    names = sorted(path for path in source.iterdir() if _CHUNK_NAME.fullmatch(path.name))
    if not names:
        raise PilotError("No encrypted pilot checkpoints were found")
    if root.exists() and any(root.iterdir()):
        raise PilotError("Recovery requires a new empty destination directory")
    root.mkdir(parents=True, exist_ok=True)
    run_id, source_sha, previous_reserved = None, None, -1
    for index, path in enumerate(names, 1):
        if path.name != f"chunk-{index:04d}.json" or path.is_symlink():
            raise PilotError("Encrypted checkpoint sequence is missing or invalid")
        compressed, context = open_payload(path.read_bytes(), private_key_pem)
        if (not isinstance(context, dict) or context.get("manifest_id") != MANIFEST_ID
                or context.get("chunk_index") != index or not _SHA.fullmatch(str(context.get("source_sha", "")))
                or not _SHA.fullmatch(str(context.get("previous_state_sha", "")))):
            raise PilotError("Recovered checkpoint has the wrong pilot identity")
        if index == 1:
            run_id, source_sha = context.get("run_id"), context["source_sha"]
        if context.get("run_id") != run_id or context["source_sha"] != source_sha:
            raise PilotError("Recovered checkpoints belong to different runs")
        reserved = context.get("reserved_credits")
        if isinstance(reserved, bool) or not isinstance(reserved, int) or not previous_reserved <= reserved <= MAX_CREDITS:
            raise PilotError("Recovered checkpoint credit reservations are inconsistent")
        previous_reserved = reserved
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
            if len(raw) > MAX_CHECKPOINT_BYTES:
                raise PilotError("Recovered checkpoint exceeded its safe size limit")
            members = {}
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
                for member in archive.getmembers():
                    if (not member.isfile() or member.name in members
                            or (member.name not in {LEDGER_NAME, "checkpoint.json"}
                                and not _allowed_cache_path(member.name))):
                        raise PilotError("Recovered checkpoint contains an unsafe or duplicate path")
                    stream = archive.extractfile(member)
                    members[member.name] = stream.read() if stream is not None else b""
            metadata = json.loads(members.pop("checkpoint.json"))
            if metadata.get("context") != context or set(metadata["files"]) != set(members):
                raise PilotError("Recovered checkpoint manifest does not match its files")
            for name, data in members.items():
                if hashlib.sha256(data).hexdigest() != metadata["files"][name]:
                    raise PilotError("Recovered checkpoint file hash mismatch")
                if name.startswith("odds_cache/raw/") and hashlib.sha256(data).hexdigest() != PurePosixPath(name).name:
                    raise PilotError("Recovered raw response hash mismatch")
            ledger = json.loads(members[LEDGER_NAME])
            if ledger.get("manifest_id") != MANIFEST_ID or ledger.get("reserved_credits") != reserved:
                raise PilotError("Recovered ledger does not match its encrypted context")
        except PilotError:
            raise
        except Exception:
            raise PilotError("Recovered checkpoint archive failed format validation") from None
        for name, data in members.items():
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    ledger_path = root / LEDGER_NAME
    ledger = json.loads(ledger_path.read_text())
    for result in ledger.get("results", []):
        digest = result.get("sha256")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise PilotError("Recovered ledger contains an invalid response hash")
        path = root / "odds_cache" / "raw" / digest
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise PilotError("Recovered ledger references missing or changed raw data")
        result["path"] = str(path)
    ledger_path.write_bytes(_json_bytes(ledger) + b"\n")
    return {"manifest_id": MANIFEST_ID, "run_id": run_id, "source_sha": source_sha,
            "encrypted_chunks_recovered": len(names), "successful_responses": len(ledger.get("results", [])),
            "reserved_credits": ledger["reserved_credits"], "network_requests_made": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Execute the single authorized paid pilot.")
    run.add_argument("--manifest", required=True)
    run.add_argument("--public-key", required=True)
    run.add_argument("--output", required=True)
    recover = sub.add_parser("recover", help="Decrypt checkpoints locally without network access.")
    recover.add_argument("--chunks", required=True)
    recover.add_argument("--private-key", required=True)
    recover.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "recover":
            report = recover_chunks(args.chunks, Path(args.private_key).read_bytes(), args.output)
            print(json.dumps(report, sort_keys=True))
            return
        context = {"repository": os.getenv("GITHUB_REPOSITORY"), "ref": os.getenv("GITHUB_REF"),
                   "run_id": os.getenv("GITHUB_RUN_ID"), "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
                   "source_sha": os.getenv("GITHUB_SHA")}
        token = os.getenv("GITHUB_TOKEN")
        report = run_pilot(json.loads(Path(args.manifest).read_text()), Path(args.public_key).read_bytes(), args.output,
                           run_context=context, api_key=os.getenv("ODDS_API_KEY"),
                           state_store=GitHubStateStore(token), additional_secrets=(token,),
                           progress=lambda item: print(json.dumps(item, sort_keys=True), flush=True))
        if report is None or report["status"] != "complete":
            raise SystemExit(1)
    except PilotError as error:
        print(json.dumps({"status": "stopped", "reason": str(error), "automatic_retry": False}), flush=True)
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"status": "stopped", "reason": "Pilot operation failed; inspect retained state before any further paid action",
                          "automatic_retry": False}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
