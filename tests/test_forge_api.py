# SPDX-License-Identifier: GPL-2.0-or-later

import json
import hashlib
import io
import os
import tempfile
import threading
import unittest
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from lore_review_companion.forge_api import MAX_JSON_BYTES, ForgeAPI, ForgeAPIError, read_private_token
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.forge_comments import DeliveryUncertainError
from lore_review_companion.import_policy import read_import_policy


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or Message()
        self.closed = False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def close(self):
        self.closed = True


class GiteaAPITests(unittest.TestCase):
    def test_private_token_file_rejects_symlinks_public_permissions_and_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("synthetic-token\n")
            path.chmod(0o600)
            self.assertEqual(read_private_token(path), "synthetic-token")
            link = path.with_name("link")
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "safely"):
                read_private_token(link)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                read_private_token(path)
            path.chmod(0o600)
            for value in ("", "bad token", "x" * 4097):
                with self.subTest(length=len(value)):
                    path.write_text(value)
                    with self.assertRaises(ValueError) as caught:
                        read_private_token(path)
                    self.assertNotIn(value if value else "synthetic-token", str(caught.exception))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_import_only_client_previews_without_user_or_comment_writes(self):
        requests = []
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["conversation"],
        }

        def transport(request, _timeout):
            requests.append(request)
            return FakeResponse(200, json.dumps(capabilities).encode())

        client = ForgeImportClient(ForgeAPI("https://forge.test/api/v1/", transport=transport),
                                   "owner", "repo", source_url=capabilities["source_url"])
        result = client.import_conversation(2, {"operation_key": "reply:1", "body": "Review"})
        self.assertTrue(result.planned)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].full_url.endswith("/review-imports/capabilities"))
        capabilities["source_url"] = "https://other.test/list/"
        with self.assertRaisesRegex(ValueError, "capability"):
            client.import_conversation(2, {"operation_key": "reply:1"})
        for key in ("../escape", "bad/key", "", "x" * 129):
            with self.subTest(key=key), self.assertRaises(ValueError):
                client.lookup_operation(key)

    def test_unattended_policy_is_private_identity_bound_and_rechecked_before_writes(self):
        policy_value = {"version": 1, "enabled": True, "expires_at": "2099-01-01T00:00:00+00:00",
                        "api_url": "https://forge.test/api/v1/", "owner": "owner", "repository": "repo", "repository_id": 1,
                        "importer_id": 2, "importer_login": "review-companion", "source_url": "https://lore.kernel.org/xen-devel/",
                        "nntp_host": "nntp.lore.kernel.org", "nntp_group": "org.xenproject.lists.xen-devel",
                        "git_remote": "https://forge.test/owner/repo.git", "git_username": "git-bot", "base_commit": "a" * 40,
                        "target_branch": "staging", "message_limit": 100, "revision_limit": 8, "max_inline_lines": 50,
                        "lookback_hours": 148, "notifications": "disabled", "identity_rule": "isolated-unverified", "retry_uncertain": False}
        capabilities = {"version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
                        "source_url": policy_value["source_url"], "operations": ["pull", "conversation", "review", "review-reply"],
                        "unattended_allowed": True, "max_inline_lines": 50}
        requests = []

        def transport(request, _timeout):
            requests.append(request)
            self.assertIn("unattended=true", request.full_url)
            return FakeResponse(200, json.dumps(capabilities).encode())

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(json.dumps(policy_value))
            path.chmod(0o600)
            policy = read_import_policy(path)
            client = ForgeImportClient(ForgeAPI(policy.api_url, transport=transport), policy.owner, policy.repository,
                                       source_url=policy.source_url, unattended=True,
                                       capability_guard=lambda result: policy.assert_current(path, result))
            self.assertTrue(client.import_conversation(2, {"operation_key": "preview"}).planned)
            path.write_text(json.dumps({**policy_value, "enabled": False}))
            client.write_enabled = True
            with self.assertRaisesRegex(ValueError, "disabled"):
                client.import_conversation(2, {"operation_key": "preview"})
            path.write_text(json.dumps(policy_value))
            capabilities["repository_id"] = 99
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                client.import_conversation(2, {"operation_key": "preview"})
            capabilities["repository_id"] = 1
            for changed in ({"expires_at": "2000-01-01T00:00:00+00:00"}, {"expires_at": "2099-01-01T00:00:00"},
                            {"message_limit": True}, {"revision_limit": 33}, {"base_commit": "staging"},
                            {"notifications": "enabled"}, {"identity_rule": "adopt-existing-users"}):
                with self.subTest(changed=changed):
                    path.write_text(json.dumps({**policy_value, **changed}))
                    with self.assertRaises(ValueError):
                        read_import_policy(path)
            path.write_text(json.dumps(policy_value).replace('"enabled": true', '"enabled": false, "enabled": true'))
            with self.assertRaisesRegex(ValueError, "schema"):
                read_import_policy(path)
            path.write_text(json.dumps(policy_value))
            link = path.with_name("policy-link")
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "revoked"):
                read_import_policy(link)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                read_import_policy(path)
            path.chmod(0o600)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "revoked"):
                client.import_conversation(2, {"operation_key": "preview"})
        self.assertTrue(all(request.get_method() == "GET" for request in requests))

    def test_import_only_client_reconciles_response_loss_with_same_idempotent_post(self):
        requests, accepted = [], {}
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["conversation"],
        }
        payload = {
            "operation_key": "reply:1", "message_id": "review@example.test", "message_sha256": "a" * 64,
            "address": "reviewer@example.test", "name": "Reviewer", "body": "Review",
        }

        def transport(request, _timeout):
            requests.append(request)
            if request.full_url.endswith("/capabilities"):
                return FakeResponse(200, json.dumps(capabilities).encode())
            if request.get_method() == "POST":
                sent = json.loads(request.data)
                self.assertEqual(sent, payload)
                if not accepted:
                    accepted.update({
                        "id": 10, "repository_id": 1, "importer_id": 2, "participant_id": 3,
                        "comment_id": 20, "type": "conversation", "operation_key": sent["operation_key"],
                        "message_id": sent["message_id"], "message_sha256": sent["message_sha256"],
                        "source_url": capabilities["source_url"] + sent["message_id"] + "/",
                    })
                    raise URLError("lost response")
                return FakeResponse(200, json.dumps(accepted).encode())
            return FakeResponse(200, json.dumps(accepted).encode())

        client = ForgeImportClient(ForgeAPI("https://forge.test/api/v1/", transport=transport), "owner", "repo",
                                   source_url=capabilities["source_url"], write_enabled=True)
        result = client.import_conversation(2, payload)
        self.assertEqual(result.remote_object["comment_id"], 20)
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST", "GET", "POST"])
        self.assertEqual(client.lookup_operation("reply:1"), accepted)
        accepted["comment_id"] = False
        with self.assertRaises(DeliveryUncertainError):
            client.import_conversation(2, payload)
        capabilities["operations"] = []
        with self.assertRaisesRegex(ValueError, "not supported"):
            client.import_conversation(2, payload)

    def test_import_only_client_preserves_rejection_and_uncertain_writes(self):
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["conversation"],
        }
        writes = []
        status = 409

        def transport(request, _timeout):
            if request.full_url.endswith("/capabilities"):
                return FakeResponse(200, json.dumps(capabilities).encode())
            if request.get_method() == "GET":
                return FakeResponse(404, b"missing")
            writes.append(request)
            return FakeResponse(status, b"failure")

        client = ForgeImportClient(ForgeAPI("https://forge.test/api/v1/", transport=transport), "owner", "repo",
                                   source_url=capabilities["source_url"], write_enabled=True)
        payload = {"operation_key": "reply:1"}
        self.assertIsNone(client.lookup_operation("reply:1"))
        with self.assertRaises(ForgeAPIError) as caught:
            client.import_conversation(2, payload)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(writes), 1)
        status = 503
        with self.assertRaises(DeliveryUncertainError):
            client.import_conversation(2, payload)
        self.assertEqual(len(writes), 3)
        self.assertTrue(all("/review-imports/" in request.full_url for request in writes))

    def test_import_only_client_stops_on_revocation_after_uncertain_acceptance(self):
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["conversation"],
        }
        requests = []
        revoked = False

        def transport(request, _timeout):
            nonlocal revoked
            requests.append(request.get_method())
            if revoked:
                return FakeResponse(403, b"revoked")
            if request.get_method() == "GET":
                return FakeResponse(200, json.dumps(capabilities).encode())
            revoked = True
            raise URLError("lost response after acceptance")

        client = ForgeImportClient(ForgeAPI("https://forge.test/api/v1/", transport=transport), "owner", "repo",
                                   source_url=capabilities["source_url"], write_enabled=True)
        with self.assertRaisesRegex(DeliveryUncertainError, "capability recheck"):
            client.import_conversation(2, {"operation_key": "reply:1"})
        self.assertEqual(requests, ["GET", "POST", "GET"])

    def test_import_only_client_keeps_review_and_reply_operations_explicit(self):
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["review", "review-reply", "pull"],
        }
        requests = []
        inline_ids = [30, 31]

        def transport(request, _timeout):
            requests.append(request)
            if request.get_method() == "GET":
                return FakeResponse(200, json.dumps(capabilities).encode())
            sent = json.loads(request.data)
            is_reply = request.full_url.endswith("/replies")
            is_pull = request.full_url.endswith("/review-imports/pulls")
            return FakeResponse(200, json.dumps({
                "id": 10, "repository_id": 1, "importer_id": 2, "participant_id": 3,
                "comment_id": 20, "review_id": 21, "code_comment_ids": [] if is_reply else inline_ids,
                "type": "pull" if is_pull else "review-reply" if is_reply else "review", "operation_key": sent["operation_key"],
                "message_id": sent["message_id"], "message_sha256": sent["message_sha256"],
                "source_url": capabilities["source_url"] + sent["message_id"] + "/",
                "pull_index": 7, "issue_id": 40,
                **{field: sent.get(field) for field in ("head_commit", "base_commit", "head_branch", "base_branch")},
            }).encode())

        client = ForgeImportClient(ForgeAPI("https://forge.test/api/v1/", transport=transport), "owner", "repo",
                                   source_url=capabilities["source_url"], write_enabled=True)
        payload = {
            "operation_key": "review:1", "message_id": "review@example.test", "message_sha256": "a" * 64,
            "comments": [{"body": "first range"}, {"body": "second range"}],
        }
        result = client.import_review(2, payload)
        self.assertEqual(result.remote_object["code_comment_ids"], [30, 31])
        reply = {"operation_key": "reply:1", "message_id": "reply@example.test", "message_sha256": "b" * 64, "parent_id": 30}
        self.assertEqual(client.import_reply(2, reply).remote_object["review_id"], 21)
        self.assertTrue(requests[1].full_url.endswith("/pulls/2/reviews"))
        self.assertTrue(requests[3].full_url.endswith("/pulls/2/replies"))
        pull = {
            "operation_key": "series:1", "message_id": "cover@example.test", "message_sha256": "c" * 64,
            "head_branch": "lore-review/v1-head", "base_branch": "lore-review/v1-base",
            "head_commit": "a" * 40, "base_commit": "b" * 40,
        }
        self.assertEqual(client.import_pull(pull).remote_object["pull_index"], 7)
        self.assertTrue(requests[-1].full_url.endswith("/review-imports/pulls"))
        for invalid in ([True, 31], [30, 30], [30]):
            inline_ids = invalid
            with self.subTest(identifiers=invalid), self.assertRaises(DeliveryUncertainError):
                client.import_review(2, payload)
        with self.assertRaisesRegex(ValueError, "not supported"):
            client.import_conversation(2, {"operation_key": "conversation:1"})

    def test_import_comparison_reads_verify_exact_parent_and_diff_hash(self):
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 2, "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["review"],
        }
        comparison = {
            "head_commit": "a" * 40, "base_commit": "b" * 40, "merge_base": "b" * 40,
            "before_commit": "b" * 40, "after_commit": "c" * 40, "diff": "exact patch\n",
            "diff_sha256": hashlib.sha256(b"exact patch\n").hexdigest(),
        }
        requests = []

        def transport(request, _timeout):
            requests.append(request)
            return FakeResponse(200, json.dumps(capabilities if request.full_url.endswith("/capabilities") else comparison).encode())

        client = ForgeImportClient(ForgeAPI("https://forge.test/api/v1/", transport=transport), "owner", "repo",
                                   source_url=capabilities["source_url"])
        self.assertEqual(client.comparison(2, after_commit="c" * 40), comparison)
        comparison["diff_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "exact patch"):
            client.comparison(2, after_commit="c" * 40)
        comparison["head_commit"] = True
        with self.assertRaisesRegex(ValueError, "invalid imported PR"):
            client.comparison(2)
        self.assertTrue(all("/review-imports/" in request.full_url for request in requests))

    @patch("lore_review_companion.forge_api.build_opener")
    def test_default_transport_passes_timeout_by_keyword(self, build_opener):
        response = FakeResponse(200, b"{}")
        build_opener.return_value.open.return_value = response
        api = ForgeAPI("http://gitea.test", timeout=3.5)

        self.assertEqual(api.request_json("api/v1/version"), {})

        self.assertEqual(build_opener.return_value.open.call_args.kwargs, {"timeout": 3.5})
        self.assertTrue(response.closed)

    def test_request_sets_token_and_decodes_json(self):
        responses = [FakeResponse(200, b'{"version":"test"}')]
        captured = []

        def transport(request, timeout):
            captured.append((request, timeout))
            return responses.pop(0)

        api = ForgeAPI("https://gitea.test/", token="local-token", transport=transport)
        result = api.request_json("/api/v1/version")

        self.assertEqual(result, {"version": "test"})
        self.assertEqual(captured[0][0].get_header("Authorization"), "token local-token")
        self.assertEqual(captured[0][1], 10)

    def test_rejects_external_and_traversal_paths_before_sending_token(self):
        requests = []
        api = ForgeAPI(
            "https://gitea.test/api/v1/", token="local-token",
            transport=lambda request, _timeout: requests.append(request),
        )

        for path in (
            "https://other.test/collect", "//other.test/collect", "../users",
            "repos/%2e%2e/users", "repos/%252e%252e/users", "repos/a%2fb/issues",
            "repos/a\\b/issues", "repos/a?token=leak",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                api.request_json(path)
        self.assertEqual(requests, [])

    def test_does_not_follow_redirect_with_token(self):
        requests = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
                self.end_headers()

            def log_message(self, _format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            api = ForgeAPI(f"http://127.0.0.1:{server.server_port}", token="local-token")
            with self.assertRaises(ForgeAPIError) as raised:
                api.request_json("redirect")
            self.assertEqual(raised.exception.status, 302)
            self.assertEqual(requests, ["/redirect"])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_rejects_unsafe_base_urls(self):
        for base_url in ("ftp://gitea.test/", "https://user:token@gitea.test/", "https://gitea.test/?x=1"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                ForgeAPI(base_url)
        with self.assertRaisesRegex(ValueError, "require HTTPS"):
            ForgeAPI("http://gitea.test/", token="local-token")

    def test_paginates_until_short_page(self):
        requested = []
        pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]

        def transport(request, _timeout):
            query = request.full_url.split("?", 1)[1]
            requested.append(query)
            page = int(dict(item.split("=") for item in query.split("&"))["page"])
            return FakeResponse(200, json.dumps(pages[page - 1]).encode())

        api = ForgeAPI("http://gitea.test", transport=transport)
        result = list(api.get_pages("api/v1/repos/o/r/issues", per_page=2))

        self.assertEqual([item["id"] for item in result], [1, 2, 3])
        self.assertEqual(len(requested), 2)
        self.assertIn("limit=2", requested[0])

    def test_reads_raw_diff_with_a_size_limit(self):
        requests = []

        def transport(request, _timeout):
            requests.append(request)
            return FakeResponse(200, b"diff contents")

        api = ForgeAPI("https://gitea.test/api/v1/", transport=transport)
        self.assertEqual(api.request_bytes("repos/o/r/pulls/7.diff", max_bytes=20), b"diff contents")
        self.assertEqual(requests[0].get_header("Accept"), "text/plain")
        with self.assertRaisesRegex(ForgeAPIError, "exceeds"):
            api.request_bytes("repos/o/r/pulls/7.diff", max_bytes=4)

    def test_bounds_json_and_rejects_incomplete_pagination(self):
        api = ForgeAPI("https://gitea.test/api/v1/", transport=lambda _request, _timeout: FakeResponse(200, b" " * (MAX_JSON_BYTES + 1)))
        with self.assertRaisesRegex(ForgeAPIError, "exceeds"):
            api.request_json("user")
        api = ForgeAPI("https://gitea.test/api/v1/", transport=lambda _request, _timeout: FakeResponse(200, b"[1]"))
        with self.assertRaisesRegex(ForgeAPIError, "page limit"):
            list(api.get_pages("pulls", per_page=1, max_pages=2))

    def test_invalid_json_preserves_the_response_status(self):
        api = ForgeAPI("https://gitea.test/", transport=lambda _request, _timeout: FakeResponse(201, b"invalid"))
        with self.assertRaises(ForgeAPIError) as raised:
            api.request_json("pulls", method="POST", payload={})
        self.assertEqual(raised.exception.status, 201)

    def test_get_retries_transient_status_with_bounded_retry_after(self):
        responses = [
            FakeResponse(503, b"", {"Retry-After": "9"}),
            FakeResponse(200, b"[]"),
        ]
        delays = []

        def transport(_request, _timeout):
            return responses.pop(0)

        api = ForgeAPI(
            "http://gitea.test",
            transport=transport,
            sleeper=delays.append,
        )

        self.assertEqual(api.request_json("api/v1/version"), [])
        self.assertEqual(delays, [5])

    def test_get_retries_network_error_but_post_does_not(self):
        attempts = []

        def transport(request, _timeout):
            attempts.append(request.get_method())
            raise URLError("temporary network error")

        api = ForgeAPI("http://gitea.test", transport=transport, retry_delay=0)

        with self.assertRaisesRegex(ForgeAPIError, "temporary network error"):
            api.request_json(
                "api/v1/repos/o/r/pulls", method="POST", payload={"title": "x"}
            )
        self.assertEqual(attempts, ["POST"])

    def test_timeout_during_response_read_retries_get_but_never_post(self):
        class TimedOutResponse(FakeResponse):
            def read(self, size=-1):
                raise TimeoutError("secret response content")

        failed = TimedOutResponse(200, b"")
        responses = [failed, FakeResponse(200, b"{}")]
        requests = []

        def transport(request, _timeout):
            requests.append(request.get_method())
            return responses.pop(0)

        api = ForgeAPI("https://gitea.test", transport=transport, retry_delay=0)
        self.assertEqual(api.request_json("user"), {})
        self.assertTrue(failed.closed)
        self.assertEqual(requests, ["GET", "GET"])
        responses.append(TimedOutResponse(201, b""))
        with self.assertRaises(ForgeAPIError) as caught:
            api.request_json("comments", method="POST", payload={})
        self.assertIsNone(caught.exception.status)
        self.assertNotIn("secret response content", str(caught.exception))
        self.assertEqual(requests, ["GET", "GET", "POST"])

    def test_timeout_reading_http_error_closes_response_and_is_uncertain(self):
        body = io.BytesIO(b"")
        error = HTTPError("https://gitea.test/secret", 500, "failure", {}, body)
        api = ForgeAPI("https://gitea.test", transport=lambda *_: (_ for _ in ()).throw(error), max_retries=0)
        with patch.object(body, "read", side_effect=TimeoutError("secret")):
            with self.assertRaises(ForgeAPIError) as caught:
                api.request_json("comments", method="POST", payload={})
        self.assertTrue(body.closed)
        self.assertIsNone(caught.exception.status)
        self.assertNotIn("secret", str(caught.exception))

    def test_http_failure_reports_status_without_response_body(self):
        api = ForgeAPI(
            "http://gitea.test",
            transport=lambda _request, _timeout: FakeResponse(404, b"private response"),
            max_retries=0,
        )

        with self.assertRaises(ForgeAPIError) as raised:
            api.request_json("api/v1/missing")

        self.assertEqual(raised.exception.status, 404)
        self.assertNotIn("private response", str(raised.exception))


if __name__ == "__main__":
    unittest.main()