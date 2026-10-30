# SPDX-License-Identifier: GPL-2.0-or-later

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lore_review_companion.forge_api import ForgeAPI
from lore_review_companion.xen_setup import main, provision_xen
from test_forge_comments import FakeResponse


class XenSetupTests(unittest.TestCase):
    def test_new_environment_uses_defaults_and_private_selected_tokens(self):
        requests = []
        mirror = {
            "id": 10,
            "mirror": True,
            "original_url": "https://github.com/xen-project/xen.git",
            "clone_url": "https://forge.test/xen-project/xen.git",
            "default_branch": "master",
        }
        review = {
            "id": 20,
            "mirror": False,
            "parent": {"id": 10},
            "clone_url": "https://forge.test/xen-review/xen.git",
        }

        def transport(request, timeout):
            path = request.full_url.removeprefix("https://forge.test/api/v1/").split(
                "?"
            )[0]
            method = request.get_method()
            body = json.loads(request.data) if request.data else None
            requests.append((method, path, body))
            if path == "user":
                return FakeResponse(200, {"login": "admin", "is_admin": True})
            if method == "GET" and path in (
                "users/review-companion",
                "orgs/xen-project",
                "orgs/xen-review",
                "repos/xen-project/xen",
            ):
                return FakeResponse(404, {})
            if path == "admin/users":
                return FakeResponse(
                    201, {"id": 5, "login": "review-companion", "email": body["email"]}
                )
            if path == "repos/migrate":
                return FakeResponse(201, mirror)
            if path == "orgs/xen-review/teams":
                return FakeResponse(
                    200, [{"id": 30, "name": "Owners", "permission": "owner"}]
                )
            if method == "GET" and path == "repos/xen-review/xen":
                return (
                    FakeResponse(404, {})
                    if not any(item[1].endswith("/forks") for item in requests)
                    else FakeResponse(200, {**review, "permissions": {"push": True}})
                )
            if path.endswith("/forks"):
                return FakeResponse(201, review)
            if path.endswith("/tokens"):
                return FakeResponse(
                    201, {"id": len(requests), "sha1": "synthetic-token"}
                )
            if path.endswith("/capabilities"):
                return FakeResponse(
                    200,
                    {
                        "version": 1,
                        "repository_id": 20,
                        "importer_id": 5,
                        "importer_login": "review-companion",
                        "source_url": "https://lore.kernel.org/xen-devel/",
                        "operations": [
                            "pull",
                            "conversation",
                            "review",
                            "review-reply",
                        ],
                        "max_inline_lines": 50,
                        "unattended_allowed": True,
                    },
                )
            if path.endswith("/branches/master"):
                return FakeResponse(200, {"commit": {"id": "a" * 40}})
            return FakeResponse(200, {})

        def factory(url, **options):
            return ForgeAPI(url, transport=transport, **options)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("lore_review_companion.xen_setup.subprocess.run"),
            patch("lore_review_companion.xen_setup.ForgeAPI", side_effect=factory),
        ):
            root = Path(temporary)
            policy = provision_xen(
                ForgeAPI(
                    "https://forge.test/api/v1",
                    token="synthetic-admin",
                    transport=transport,
                ),
                root / "config",
                root / "state",
                email="operator@example.test",
                enable_policy=True,
            )
            self.assertTrue(policy["enabled"])
            self.assertEqual(policy["base_commit"], "a" * 40)
            for name in (
                "initial-password",
                "import-token",
                "git-token",
                "import-policy.json",
            ):
                self.assertEqual((root / "config" / name).stat().st_mode & 0o777, 0o600)
        (migration,) = [
            body for method, path, body in requests if path == "repos/migrate"
        ]
        self.assertNotIn("mirror_interval", migration)
        self.assertEqual(
            migration["clone_addr"], "https://github.com/xen-project/xen.git"
        )
        self.assertIn(("PUT", "teams/30/members/review-companion", None), requests)
        for method, path, body in requests:
            if path.endswith("/tokens"):
                self.assertEqual(
                    body["repositories"], [{"owner": "xen-review", "name": "xen"}]
                )

    def test_preview_does_not_read_admin_token_or_call_api(self):
        with patch("lore_review_companion.xen_setup.ForgeAPI") as api:
            self.assertEqual(
                main(
                    [
                        "--api-url",
                        "https://forge.test/api/v1",
                        "--admin-token-file",
                        "/missing",
                        "--email",
                        "operator@example.test",
                    ]
                ),
                0,
            )
            api.assert_not_called()
