"""No paid/network requests: one-shot budget, encryption, and recovery guards."""
from __future__ import annotations

import copy
import base64
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import urllib.error

from research.paid_pilot import (
    MANIFEST_ID, MAX_CREDITS, REQUEST_COUNT, REPOSITORY, PILOT_BRANCH, STATE_BRANCH,
    LEDGER_NAME, GitHubStateStore, PilotError, _crypto, seal_payload, open_payload,
    run_pilot, recover_chunks, validate_run_context,
)


def context():
    return {"repository": REPOSITORY, "ref": "refs/heads/" + PILOT_BRANCH,
            "run_id": "12345", "run_attempt": "1", "source_sha": "a" * 40}


class FakeState:
    def __init__(self, *, claim_fails=False, checkpoint_fails=False):
        self.head_sha = None
        self.claim_fails = claim_fails
        self.checkpoint_fails = checkpoint_fails
        self.claims, self.checkpoints = [], []

    def claim(self, reservation):
        self.claims.append(reservation)
        if self.claim_fails:
            raise PilotError("Existing durable claim")
        self.head_sha = "1" * 40
        return self.head_sha

    def checkpoint(self, name, sealed, summary):
        if self.checkpoint_fails:
            raise PilotError("Checkpoint publication failed")
        self.checkpoints.append((name, sealed, copy.deepcopy(summary)))
        self.head_sha = f"{len(self.checkpoints) + 1:040x}"
        return self.head_sha


class FakeFetcher:
    def __init__(self, state, *, halt=False, leak=False):
        self.state, self.halt, self.leak = state, halt, leak
        self.caps = []
        self.ledger = {"manifest_id": MANIFEST_ID, "attempts": {}, "results": [], "reserved_credits": 0}

    def __call__(self, manifest, root, *, max_credits, allow_paid, api_key, downloader):
        if self.state.head_sha is None:
            raise AssertionError("Paid fetch occurred before durable claim")
        if allow_paid is not True:
            raise AssertionError("Paid fetch lacked explicit authorization")
        self.caps.append(max_credits)
        raw_dir = Path(root) / "odds_cache" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        if self.halt:
            item = manifest["requests"][0]
            self.ledger["attempts"][item["request_id"]] = {"status": "failed", "reserved_credits": 20}
            self.ledger["reserved_credits"] = 20
            self.ledger["halted"] = {"reason": "authentication_or_historical_entitlement", "http_status": 401}
        else:
            for item in manifest["requests"][len(self.ledger["attempts"]):max_credits // 20]:
                raw = json.dumps({"timestamp": item["requested_at"],
                                  "data": [{"request_id": item["request_id"],
                                            "private_bookmaker_price": -115,
                                            "marker": api_key if self.leak else "not-a-secret"}]}).encode()
                digest = hashlib.sha256(raw).hexdigest()
                path = raw_dir / digest
                path.write_bytes(raw)
                self.ledger["attempts"][item["request_id"]] = {"status": "downloaded", "reserved_credits": 20}
                self.ledger["reserved_credits"] += 20
                self.ledger["results"].append({"request_id": item["request_id"], "sha256": digest,
                                                "path": str(path), "timestamp": item["requested_at"]})
        (Path(root) / LEDGER_NAME).write_text(json.dumps(self.ledger))
        return copy.deepcopy(self.ledger)


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "optional private research dependency not installed")
class PaidPilotCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, serialization, _, rsa, _ = _crypto()
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.private_pem = key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
        cls.public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.manifest = json.loads((Path(__file__).resolve().parents[1] / "research" / "pilots" /
                                   "cfb_2025" / "manifest.json").read_text())

    def test_sealed_roundtrip_and_tampering_fail_closed(self):
        payload, metadata = b"private historical odds", {"manifest_id": MANIFEST_ID, "chunk_index": 1}
        sealed = seal_payload(payload, self.public_pem, metadata)
        self.assertNotIn(payload, sealed)
        self.assertEqual(open_payload(sealed, self.private_pem, expected_context=metadata), (payload, metadata))
        changed = json.loads(sealed)
        changed["context"]["chunk_index"] = 2
        with self.assertRaises(PilotError):
            open_payload(json.dumps(changed).encode(), self.private_pem)
        with self.assertRaises(PilotError):
            open_payload(sealed, self.private_pem, expected_context={"wrong": True})
        changed = json.loads(sealed)
        ciphertext = bytearray(base64.b64decode(changed["ciphertext"]))
        ciphertext[0] ^= 1
        changed["ciphertext"] = base64.b64encode(ciphertext).decode()
        with self.assertRaises(PilotError):
            open_payload(json.dumps(changed).encode(), self.private_pem)

    def test_full_pilot_is_capped_checkpointed_and_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            state = FakeState()
            fetcher = FakeFetcher(state)
            report = run_pilot(self.manifest, self.public_pem, root, run_context=context(),
                               api_key="private-test-api-key", state_store=state, fetcher=fetcher)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["reserved_credits"], MAX_CREDITS)
            self.assertEqual(report["successful_responses"], REQUEST_COUNT)
            self.assertEqual(fetcher.caps, list(range(500, MAX_CREDITS, 500)) + [MAX_CREDITS])
            self.assertEqual(len(state.claims), 1)
            self.assertEqual(len(state.checkpoints), 31)
            self.assertEqual(state.claims[0]["maximum_credits"], MAX_CREDITS)
            for _, sealed, summary in state.checkpoints:
                self.assertNotIn(b"private-test-api-key", sealed)
                self.assertNotIn(b"private_bookmaker_price", sealed)
                self.assertFalse(summary["raw_data_public"])
            recovered = Path(directory) / "recovered"
            recovery = recover_chunks(root / "sealed", self.private_pem, recovered)
            self.assertEqual(recovery["network_requests_made"], 0)
            self.assertEqual(recovery["successful_responses"], REQUEST_COUNT)
            ledger = json.loads((recovered / LEDGER_NAME).read_text())
            self.assertTrue(all(Path(item["path"]).is_file() for item in ledger["results"]))
            self.assertTrue(all(Path(item["path"]).is_relative_to(recovered) for item in ledger["results"]))
            # A later checkpoint is incremental, not a repeat of every raw file.
            compressed, _ = open_payload(state.checkpoints[-1][1], self.private_pem)
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(compressed)), mode="r:") as archive:
                raw_members = [item for item in archive.getmembers() if item.name.startswith("odds_cache/raw/")]
            self.assertEqual(len(raw_members), 12)

    def test_existing_claim_stops_before_any_paid_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FakeState(claim_fails=True)
            fetcher = FakeFetcher(state)
            with self.assertRaises(PilotError):
                run_pilot(self.manifest, self.public_pem, Path(directory) / "run", run_context=context(),
                          api_key="private-test-api-key", state_store=state, fetcher=fetcher)
            self.assertEqual(fetcher.caps, [])

    def test_checkpoint_failure_stops_before_next_paid_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FakeState(checkpoint_fails=True)
            fetcher = FakeFetcher(state)
            root = Path(directory) / "run"
            with self.assertRaises(PilotError):
                run_pilot(self.manifest, self.public_pem, root, run_context=context(),
                          api_key="private-test-api-key", state_store=state, fetcher=fetcher)
            self.assertEqual(fetcher.caps, [500])
            self.assertTrue((root / "sealed" / "chunk-0001.json").is_file())
            self.assertEqual(json.loads((root / "public_summary.json").read_text())["reserved_credits"], 500)

    def test_entitlement_failure_is_checkpointed_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FakeState()
            fetcher = FakeFetcher(state, halt=True)
            report = run_pilot(self.manifest, self.public_pem, Path(directory) / "run", run_context=context(),
                               api_key="private-test-api-key", state_store=state, fetcher=fetcher)
            self.assertEqual(report["status"], "halted")
            self.assertEqual(report["http_status"], 401)
            self.assertEqual(report["reserved_credits"], 20)
            self.assertEqual(fetcher.caps, [500])
            self.assertEqual(len(state.checkpoints), 1)

    def test_wrong_scope_context_or_key_cannot_claim_budget(self):
        for variant in ("scope", "run_attempt", "branch", "private_key", "missing_api_key"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                state = FakeState()
                manifest, identity = copy.deepcopy(self.manifest), context()
                public_key, api_key = self.public_pem, "private-test-api-key"
                if variant == "scope":
                    manifest["estimated_credits"] += 20
                elif variant == "run_attempt":
                    identity["run_attempt"] = "2"
                elif variant == "branch":
                    identity["ref"] = "refs/heads/main"
                elif variant == "private_key":
                    public_key = self.private_pem
                else:
                    api_key = ""
                with self.assertRaises((PilotError, ValueError)):
                    run_pilot(manifest, public_key, Path(directory) / "run", run_context=identity,
                              api_key=api_key, state_store=state, fetcher=FakeFetcher(state))
                self.assertEqual(state.claims, [])

    def test_secret_echo_is_not_exported_even_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FakeState()
            fetcher = FakeFetcher(state, leak=True)
            with self.assertRaisesRegex(PilotError, "Secret material"):
                run_pilot(self.manifest, self.public_pem, Path(directory) / "run", run_context=context(),
                          api_key="private-test-api-key", state_store=state, fetcher=fetcher)
            self.assertEqual(state.checkpoints, [])
            self.assertEqual(fetcher.caps, [500])

    def test_recovery_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            chunks = Path(directory) / "chunks"
            chunks.mkdir()
            identity = {"manifest_id": MANIFEST_ID, "run_id": "12345", "source_sha": "a" * 40,
                        "chunk_index": 1, "reserved_credits": 0, "previous_state_sha": "b" * 40}
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                info = tarfile.TarInfo("../../escape.txt")
                info.size = 4
                archive.addfile(info, io.BytesIO(b"oops"))
            sealed = seal_payload(gzip.compress(buffer.getvalue()), self.public_pem, identity)
            (chunks / "chunk-0001.json").write_bytes(sealed)
            with self.assertRaises(PilotError):
                recover_chunks(chunks, self.private_pem, Path(directory) / "out")
            self.assertFalse((Path(directory) / "escape.txt").exists())


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


class GitHubStateTests(unittest.TestCase):
    def test_claim_is_orphan_and_created_atomically(self):
        calls = []
        expected = [
            ("POST", "/git/trees", 201, {"sha": "a" * 40}),
            ("POST", "/git/commits", 201, {"sha": "b" * 40}),
            ("POST", "/git/refs", 201, {"ref": "refs/heads/" + STATE_BRANCH,
                                         "object": {"type": "commit", "sha": "b" * 40}}),
            ("GET", "/git/ref/heads/" + STATE_BRANCH, 200,
             {"ref": "refs/heads/" + STATE_BRANCH, "object": {"type": "commit", "sha": "b" * 40}}),
        ]

        def transport(request, timeout):
            method, path, status, body = expected[len(calls)]
            self.assertEqual(request.method, method)
            self.assertTrue(request.full_url.endswith(path))
            self.assertNotIn("private-test-token", request.full_url)
            calls.append((method, path, json.loads(request.data) if request.data else None))
            return Response(status, body)

        state = GitHubStateStore("private-test-token", transport=transport)
        self.assertEqual(state.claim({"maximum_credits": MAX_CREDITS}), "b" * 40)
        self.assertNotIn("base_tree", calls[0][2])
        self.assertEqual(calls[1][2]["parents"], [])
        self.assertEqual(calls[2][2]["ref"], "refs/heads/" + STATE_BRANCH)
        with self.assertRaises(PilotError):
            state.claim({"maximum_credits": MAX_CREDITS})
        self.assertEqual(len(calls), 4)

    def test_checkpoint_lease_mismatch_is_fail_closed(self):
        calls = []

        def transport(request, timeout):
            calls.append(request.method)
            return Response(200, {"ref": "refs/heads/" + STATE_BRANCH,
                                  "object": {"type": "commit", "sha": "c" * 40}})

        state = GitHubStateStore("private-test-token", transport=transport)
        state.head_sha, state.tree_sha = "b" * 40, "a" * 40
        with self.assertRaises(PilotError):
            state.checkpoint("chunk-0001.json", b"{}", {})
        self.assertEqual(calls, ["GET"])

    def test_http_failure_does_not_leak_token_or_retry(self):
        calls = []

        def transport(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError("https://example.test/?token=private-test-token", 422,
                                         "private-test-token", {}, None)

        state = GitHubStateStore("private-test-token", transport=transport)
        with self.assertRaises(PilotError) as caught:
            state.claim({"maximum_credits": MAX_CREDITS})
        self.assertIn("422", str(caught.exception))
        self.assertNotIn("private-test-token", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_run_context_disallows_other_repositories_and_reruns(self):
        validate_run_context(context())
        for field, value in (("repository", "someone/else"), ("run_attempt", "2"),
                             ("ref", "refs/heads/main"), ("run_id", "not-a-run"),
                             ("source_sha", "invalid")):
            identity = context()
            identity[field] = value
            with self.assertRaises(PilotError):
                validate_run_context(identity)


if __name__ == "__main__":
    unittest.main()
