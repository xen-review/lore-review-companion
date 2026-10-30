import json
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