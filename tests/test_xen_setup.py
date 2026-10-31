# SPDX-License-Identifier: GPL-2.0-or-later

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lore_review_companion.forge_api import ForgeAPI
from lore_review_companion.nntp_source import NNTPFetch, NNTPThreadDiscovery
from lore_review_companion.import_policy import DEFAULT_POLICY_PATH
from lore_review_companion.xen_setup import (
    SAFETY,
    main,
    provision_xen,
    rebuild_development_fork,
)
from lore_review_companion.xen_import import main as import_main
from test_forge_comments import FakeResponse


class XenSetupTests(unittest.TestCase):
    def test_development_rebuild_is_scoped_and_retires_matching_state(self):
        policy = json.loads(DEFAULT_POLICY_PATH.read_text())
        for mismatch in (False, True):
            with (
                self.subTest(mismatch=mismatch),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                config, state = root / "config", root / "state"
                config.mkdir(mode=0o700)
                state.mkdir(mode=0o700)
                for name in ("import-token", "git-token"):
                    (config / name).write_text("synthetic-token")
                (state / "mail.sqlite3").write_text("retained-source")
                (state / "worker.sqlite3").write_text("old-scheduler")
                api = Mock(base_url=policy["api_url"])
                api.request_json.side_effect = [
                    {"is_admin": True},
                    {
                        "id": 1,
                        "mirror": True,
                        "original_url": "https://github.com/xen-project/xen.git",
                    },
                    {
                        "id": policy["repository_id"] + int(mismatch),
                        "mirror": False,
                        "parent": {"id": 1},
                    },
                    [
                        {"id": 40, "name": "lore-xen-import-token"},
                        {"id": 41, "name": "unrelated-token"},
                    ],
                    None,
                    None,
                    {"id": 30},
                ]
                with patch("lore_review_companion.xen_setup.subprocess.run") as stop:
                    if mismatch:
                        with self.assertRaisesRegex(ValueError, "identity mismatch"):
                            rebuild_development_fork(
                                api, config, state, DEFAULT_POLICY_PATH
                            )
                        stop.assert_not_called()
                        self.assertTrue(state.exists())
                        self.assertTrue((config / "import-token").exists())
                        self.assertEqual(api.request_json.call_count, 3)
                    else:
                        result = rebuild_development_fork(
                            api, config, state, DEFAULT_POLICY_PATH
                        )
                        self.assertEqual(result["repository_id"], 30)
                        retired = Path(result["retired_state"])
                        self.assertEqual(
                            (retired / "mail.sqlite3").read_text(), "retained-source"
                        )
                        self.assertTrue((retired / "worker.sqlite3").exists())
                        self.assertFalse(state.exists())
                        self.assertFalse((config / "import-token").exists())
                        stop.assert_called_once()
                        api.request_json.assert_any_call(
                            "repos/xen-review/xen", method="DELETE"
                        )
                        api.request_json.assert_any_call(
                            "admin/users/review-companion/tokens/40", method="DELETE"
                        )
                        self.assertNotIn(
                            (
                                ("admin/users/review-companion/tokens/41",),
                                {"method": "DELETE"},
                            ),
                            api.request_json.call_args_list,
                        )
                        api.request_json.assert_any_call(
                            "repos/xen-project/xen/forks",
                            method="POST",
                            payload={"organization": "xen-review", "name": "xen"},
                        )

    def test_on_demand_import_rejects_unapproved_remote_writes(self):
        for command in ("publish", "deliver"):
            with self.subTest(command=command), self.assertRaises(SystemExit) as error:
                import_main(
                    ["--seed", "cover@example.test", "--revision", "1", command]
                )
            self.assertEqual(error.exception.code, 2)

    def test_on_demand_recovery_fetches_the_seed_before_thread_discovery(self):
        """Fetch an unknown seed before attempting to discover its thread."""
        deployment = {
            "nntp_host": "nntp.lore.kernel.org",
            "nntp_group": "org.xenproject.lists.xen-devel",
            "source_url": "https://lore.kernel.org/xen-devel/",
        }
        events = []
        ledger_instance = Mock()
        ledger_instance.nntp_message_received.return_value = False
        fetch_result = NNTPFetch(
            "nntps://nntp.lore.kernel.org/org.xenproject.lists.xen-devel",
            1,
            0,
            128,
            0,
            False,
        )
        thread_result = NNTPThreadDiscovery(fetch_result.source, 1, 0, 64, 128)

        def fetch(ledger, **options):
            """Record fetch ordering for the recovery test."""
            events.append(("fetch", options.get("message_id")))
            return fetch_result

        def discover(ledger, message_id, **options):
            """Record thread discovery ordering for the recovery test."""
            events.append(("discover", message_id))
            return thread_result

        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            state_dir.mkdir()
            with (
                patch(
                    "lore_review_companion.xen_import.read_deployment",
                    return_value=deployment,
                ),
                patch(
                    "lore_review_companion.xen_import.Ledger",
                    return_value=ledger_instance,
                ),
                patch("lore_review_companion.xen_import.fetch_nntp", side_effect=fetch),
                patch(
                    "lore_review_companion.xen_import.discover_nntp_thread",
                    side_effect=discover,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    import_main(
                        [
                            "--config-dir",
                            str(Path(temporary) / "config"),
                            "--state-dir",
                            str(state_dir),
                            "--seed",
                            "cover@example.test",
                            "--revision",
                            "1",
                            "recover",
                        ]
                    ),
                    0,
                )

        self.assertEqual(
            events,
            [
                ("fetch", "cover@example.test"),
                ("discover", "cover@example.test"),
                ("fetch", None),
            ],
        )

    def test_on_demand_recovery_ignores_obsolete_incident_marker(self):
        """Old development marker files must never block another attempt."""
        deployment = {
            "nntp_host": "nntp.lore.kernel.org",
            "nntp_group": "org.xenproject.lists.xen-devel",
            "source_url": "https://lore.kernel.org/xen-devel/",
        }
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            state_dir.mkdir()
            incident = state_dir / "worker.sqlite3.incident"
            incident.write_text("latched\n")
            with (
                patch(
                    "lore_review_companion.xen_import.read_deployment",
                    return_value=deployment,
                ),
                patch("lore_review_companion.xen_import.Ledger", return_value=Mock()),
                patch("lore_review_companion.xen_import.fetch_nntp") as fetch,
                patch("lore_review_companion.xen_import.discover_nntp_thread"),
                patch("lore_review_companion.xen_import.asdict", return_value={}),
                redirect_stdout(io.StringIO()),
            ):
                result = import_main(
                    [
                        "--config-dir",
                        str(Path(temporary) / "config"),
                        "--state-dir",
                        str(state_dir),
                        "--seed",
                        "cover@example.test",
                        "--revision",
                        "1",
                        "recover",
                    ]
                )
                self.assertEqual(result, 0)
                self.assertTrue(incident.exists())
                fetch.assert_called_once()

    def test_on_demand_plan_has_no_write_client_and_exposes_digest(self):
        deployment = {
            "api_url": "https://forge.test/api/v1/",
            "owner": "xen-review",
            "repository": "xen",
            "repository_id": 20,
            "importer_id": 5,
            "importer_login": "review-companion",
            "source_url": "https://lore.kernel.org/xen-devel/",
            "git_username": "review-companion",
            "git_remote": "https://forge.test/xen-review/xen.git",
            "target_branch": "staging",
            "base_commit": "a" * 40,
        }
        with (
            patch(
                "lore_review_companion.xen_import.read_deployment",
                return_value=deployment,
            ),
            patch(
                "lore_review_companion.xen_import.read_private_token",
                return_value="synthetic-token",
            ),
            patch("lore_review_companion.xen_import.Ledger") as ledger,
            patch("lore_review_companion.xen_import.ForgeImportClient") as client,
            patch("lore_review_companion.xen_import.plan_imported_sync") as plan,
            patch("lore_review_companion.xen_import.asdict", return_value={}),
        ):
            client.return_value.capabilities.return_value = deployment
            self.assertEqual(
                import_main(
                    ["--seed", "cover@example.test", "--revision", "1", "plan"]
                ),
                0,
            )
            self.assertFalse(client.call_args.kwargs["write_enabled"])
            self.assertTrue(ledger.call_args.kwargs["read_only"])
            self.assertEqual(plan.call_args.kwargs["revision"], 1)

    def test_new_environment_uses_defaults_and_private_selected_tokens(self):
        requests = []
        mirror = {
            "id": 10,
            "mirror": True,
            "original_url": "https://github.com/xen-project/xen.git",
            "clone_url": "https://forge.test/xen-project/xen.git",
            "default_branch": "staging",
        }
        review = {
            "id": 20,
            "mirror": False,
            "parent": {"id": 10},
            "clone_url": "https://forge.test/xen-review/xen.git",
            "default_branch": "staging",
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
                assert body is not None
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
            if path.endswith("/branches/staging"):
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
            public_policy = json.loads(DEFAULT_POLICY_PATH.read_text())
            public_policy.update(
                {
                    "api_url": "https://forge.test/api/v1/",
                    "repository_id": 20,
                    "importer_id": 5,
                    "base_commit": "a" * 40,
                    "git_remote": "https://forge.test/xen-review/xen.git",
                }
            )
            policy_path = root / "public-policy.json"
            policy_path.write_text(json.dumps(public_policy))
            policy_path.chmod(0o644)
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
                policy_path=policy_path,
            )
            self.assertTrue(policy["enabled"])
            self.assertEqual(policy["base_commit"], "a" * 40)
            for name in (
                "initial-password",
                "import-token",
                "git-token",
            ):
                self.assertEqual((root / "config" / name).stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / "config" / "import-policy.json").exists())
            self.assertEqual(json.loads(policy_path.read_text()), public_policy)
            public_policy["base_commit"] = "b" * 40
            policy_path.write_text(json.dumps(public_policy))
            with self.assertRaisesRegex(ValueError, "base_commit"):
                provision_xen(
                    ForgeAPI(
                        "https://forge.test/api/v1",
                        token="synthetic-admin",
                        transport=transport,
                    ),
                    root / "other-config",
                    root / "other-state",
                    email="operator@example.test",
                    policy_path=policy_path,
                )
        migrations = [
            body for method, path, body in requests if path == "repos/migrate"
        ]
        self.assertEqual(len(migrations), 2)
        for migration in migrations:
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
        self.assertIn(
            (
                "PATCH",
                "repos/xen-review/xen",
                {
                    **SAFETY,
                    "has_issues": True,
                    "has_pull_requests": True,
                    "default_branch": "staging",
                },
            ),
            requests,
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

    def test_rebuild_preview_does_not_read_admin_token_or_call_api(self):
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
                        "--rebuild-development-fork",
                    ]
                ),
                0,
            )
            api.assert_not_called()
