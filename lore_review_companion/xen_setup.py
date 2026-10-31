# SPDX-License-Identifier: GPL-2.0-or-later

"""Explicit API provisioning for a new Xen review deployment."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
from dataclasses import asdict
from pathlib import Path

from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError, read_private_token
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.git_branches import GitHttpCredentials
from lore_review_companion.import_worker import run_import_worker_round
from lore_review_companion.ledger import Ledger
from lore_review_companion.import_policy import DEFAULT_POLICY_PATH, read_import_policy

SOURCE = "https://lore.kernel.org/xen-devel/"
UPSTREAM = "https://github.com/xen-project/xen.git"
IMPORTER = "review-companion"
SAFETY = {
    name: False
    for name in (
        "has_actions",
        "allow_merge_commits",
        "allow_rebase",
        "allow_rebase_explicit",
        "allow_squash_merge",
        "allow_fast_forward_only_merge",
        "allow_manual_merge",
        "autodetect_manual_merge",
        "allow_rebase_update",
        "default_allow_maintainer_edit",
    )
}


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError(
            "Configuration/state directory must be private and owned by this user"
        )


def write_private(path: Path, content: str) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def optional(api: ForgeAPI, path: str):
    try:
        return api.request_json(path)
    except ForgeAPIError as error:
        if error.status == 404:
            return None
        raise


def rebuild_development_fork(
    api: ForgeAPI, config: Path, state: Path, policy_path: Path
) -> dict:
    policy = read_import_policy(policy_path)
    if (
        policy.owner != "xen-review"
        or policy.repository != "xen"
        or policy.api_url != api.base_url
    ):
        raise ValueError(
            "Development rebuild is restricted to the committed xen-review/xen destination"
        )
    if api.request_json("user").get("is_admin") is not True:
        raise ValueError("Development rebuild requires a site-administrator token")
    mirror = api.request_json("repos/xen-project/xen")
    review = api.request_json("repos/xen-review/xen")
    if (
        mirror.get("mirror") is not True
        or mirror.get("original_url", "").rstrip("/") != UPSTREAM
        or review.get("id") != policy.repository_id
        or review.get("mirror")
        or review.get("parent", {}).get("id") != mirror["id"]
    ):
        raise ValueError("Development fork/upstream identity mismatch; nothing reset")
    private_directory(config)
    private_directory(state)
    tokens = api.request_json(f"admin/users/{IMPORTER}/tokens", params={"limit": 50})
    if len(tokens) >= 50:
        raise ValueError("Development token listing is incomplete; no reset performed")
    subprocess.run(
        [
            "systemctl",
            "--user",
            "stop",
            "lore-review-companion.timer",
            "lore-review-companion.service",
        ],
        check=True,
    )
    api.request_json("repos/xen-review/xen", method="DELETE")
    for token in tokens:
        if token["name"] in ("lore-xen-import-token", "lore-xen-git-token"):
            api.request_json(
                f"admin/users/{IMPORTER}/tokens/{token['id']}", method="DELETE"
            )
    retired = state.with_name(state.name + ".rebuild-" + secrets.token_hex(6))
    state.rename(retired)
    for name in ("import-token", "git-token"):
        (config / name).unlink(missing_ok=True)
    review = api.request_json(
        "repos/xen-project/xen/forks",
        method="POST",
        payload={"organization": "xen-review", "name": "xen"},
    )
    return {
        "repository_id": review["id"],
        "retired_state": str(retired),
        "next": "Update repository_id in the committed policy, test, commit and push; then run setup without --rebuild-development-fork",
    }


def provision_xen(
    api: ForgeAPI,
    config: Path,
    state: Path,
    *,
    email: str,
    enable_policy: bool = False,
    policy_path: Path = DEFAULT_POLICY_PATH,
) -> dict:
    policy = asdict(read_import_policy(policy_path))
    if policy["api_url"] != api.base_url:
        raise ValueError("Setup API differs from the committed deployment policy")
    private_directory(config)
    private_directory(state)
    administrator = api.request_json("user")
    if administrator.get("is_admin") is not True:
        raise ValueError("Provisioning requires a temporary site-administrator token")
    user = optional(api, f"users/{IMPORTER}")
    if user is None:
        password = secrets.token_urlsafe(48)
        write_private(config / "initial-password", password + "\n")
        user = api.request_json(
            "admin/users",
            method="POST",
            payload={
                "username": IMPORTER,
                "email": email,
                "password": password,
                "must_change_password": False,
                "send_notify": False,
                "restricted": False,
                "full_name": "Review Companion",
            },
        )
    if (
        user.get("is_admin")
        or user.get("login") != IMPORTER
        or user.get("email") != email
    ):
        raise ValueError(
            "Existing importer identity is incompatible; no account adoption or password reset performed"
        )
    for name in ("xen-project", "xen-review"):
        if optional(api, f"orgs/{name}") is None:
            api.request_json(
                "orgs",
                method="POST",
                payload={"username": name, "visibility": "public"},
            )
    mirror = optional(api, "repos/xen-project/xen")
    if mirror is None:
        mirror = api.request_json(
            "repos/migrate",
            method="POST",
            payload={
                "repo_owner": "xen-project",
                "repo_name": "xen",
                "clone_addr": UPSTREAM,
                "mirror": True,
            },
        )
    if (
        mirror.get("mirror") is not True
        or mirror.get("original_url", "").rstrip("/") != UPSTREAM
    ):
        raise ValueError(
            "Existing xen-project/xen is not the expected GitHub Xen mirror"
        )
    teams = api.request_json("orgs/xen-review/teams", params={"limit": 50})
    owners = [
        team
        for team in teams
        if team.get("name") == "Owners" and team.get("permission") == "owner"
    ]
    if len(owners) != 1:
        raise ValueError("Expected one Owners team; inspect organization ownership")
    api.request_json(f"teams/{owners[0]['id']}/members/{IMPORTER}", method="PUT")
    review = optional(api, "repos/xen-review/xen")
    if review is None:
        review = api.request_json(
            "repos/xen-project/xen/forks",
            method="POST",
            payload={"organization": "xen-review", "name": "xen"},
        )
    if (
        review.get("mirror")
        or review.get("parent", {}).get("id") != mirror["id"]
        or review.get("archived")
    ):
        raise ValueError(
            "Existing xen-review/xen must be an active writable fork of xen-project/xen"
        )
    api.request_json(
        "repos/xen-review/xen",
        method="PATCH",
        payload={
            **SAFETY,
            "has_pull_requests": True,
            "default_branch": policy["target_branch"],
        },
    )
    review = api.request_json("repos/xen-review/xen")
    if review.get("default_branch") != policy["target_branch"]:
        raise ValueError("Review fork default branch differs from committed policy")
    api.request_json(
        f"repos/xen-review/xen/review-import-grants/{IMPORTER}",
        method="PUT",
        payload={"source_url": SOURCE, "allow_unattended": True},
    )
    for filename, scopes in (
        ("import-token", ["write:review-import", "write:repository", "write:issue"]),
        ("git-token", ["write:repository"]),
    ):
        path = config / filename
        if path.exists():
            read_private_token(path)
            continue
        token = api.request_json(
            f"admin/users/{IMPORTER}/tokens",
            method="POST",
            payload={
                "name": f"lore-xen-{filename}",
                "scopes": scopes,
                "repositories": [{"owner": "xen-review", "name": "xen"}],
            },
        )
        try:
            write_private(path, token["sha1"] + "\n")
        except Exception:
            api.request_json(
                f"admin/users/{IMPORTER}/tokens/{token['id']}", method="DELETE"
            )
            raise
    runtime_api = ForgeAPI(
        api.base_url, token=read_private_token(config / "import-token")
    )
    capabilities = ForgeImportClient(
        runtime_api, "xen-review", "xen", source_url=SOURCE, unattended=True
    ).capabilities()
    if (
        capabilities["repository_id"] != review["id"]
        or capabilities["importer_id"] != user["id"]
    ):
        raise ValueError("Runtime token does not match the created repository/importer")
    git_api = ForgeAPI(api.base_url, token=read_private_token(config / "git-token"))
    if (
        not git_api.request_json("repos/xen-review/xen")
        .get("permissions", {})
        .get("push")
    ):
        raise ValueError("Git token lacks selected review-repository push permission")
    repository = state / "xen.git"
    source_remote = mirror["clone_url"]
    if not repository.exists():
        subprocess.run(
            ["git", "clone", "--bare", "--", source_remote, str(repository)], check=True
        )
    elif (
        subprocess.check_output(
            ["git", "--git-dir", str(repository), "remote", "get-url", "origin"],
            text=True,
        ).strip()
        != source_remote
    ):
        raise ValueError(
            "Existing assembly clone has a different origin; inspect it rather than overwriting"
        )
    branch = policy["target_branch"]
    base = api.request_json(f"repos/xen-review/xen/branches/{branch}")["commit"]["id"]
    subprocess.run(
        ["git", "--git-dir", str(repository), "cat-file", "-e", base + "^{commit}"],
        check=True,
    )
    expected = {
        "api_url": api.base_url,
        "owner": "xen-review",
        "repository": "xen",
        "repository_id": review["id"],
        "importer_id": user["id"],
        "importer_login": IMPORTER,
        "source_url": SOURCE,
        "nntp_host": "nntp.lore.kernel.org",
        "nntp_group": "org.xenproject.lists.xen-devel",
        "git_remote": review["clone_url"],
        "git_username": IMPORTER,
        "target_branch": branch,
        "base_commit": base,
    }
    if any(policy.get(key) != value for key, value in expected.items()):
        raise ValueError(
            f"Bootstrap identities differ from committed policy; update and commit {policy_path}: {json.dumps(expected, sort_keys=True)}"
        )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(repository),
            "cat-file",
            "-e",
            policy["base_commit"] + "^{commit}",
        ],
        check=True,
    )
    return policy


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--admin-token-file", type=Path, required=True)
    parser.add_argument(
        "--email", required=True, help="Operator-controlled integration account email"
    )
    parser.add_argument(
        "--config-dir", type=Path, default=Path.home() / ".config/lore-review-companion"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/state/lore-review-companion",
    )
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument(
        "--rebuild-development-fork",
        action="store_true",
        help="Explicitly delete/recreate only xen-review/xen and retire its local import state",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly authorize the documented provisioning API writes",
    )
    parser.add_argument(
        "--enable-policy",
        action="store_true",
        help="Run imports using the enabled committed policy; does not enable a timer",
    )
    parser.add_argument(
        "--bootstrap-rounds",
        type=int,
        default=0,
        help="Run 0..32 bounded recent-mail rounds after provisioning",
    )
    options = parser.parse_args(arguments)
    if not 0 <= options.bootstrap_rounds <= 32 or (
        options.bootstrap_rounds and not options.enable_policy
    ):
        parser.error("Bootstrap requires --enable-policy and 0..32 rounds")
    if not options.apply:
        if options.rebuild_development_fork:
            print(
                "Plan: stop companion, validate identities, delete/recreate only xen-review/xen, retire its local state and scoped credentials; commit the new policy repository_id before setup/imports. Add --apply to rebuild."
            )
            return 0
        print(
            "Plan: create review-companion; xen-project/Xen GitHub mirror with server defaults; xen-review Owners membership and fork; review-only grant; secret selected-repo tokens and assembly clone; validate committed public policy. Add --apply to provision; no requests made."
        )
        return 0
    try:
        api = ForgeAPI(
            options.api_url,
            token=read_private_token(options.admin_token_file),
            timeout=600,
            max_retries=0,
        )
        if options.rebuild_development_fork:
            print(
                json.dumps(
                    rebuild_development_fork(
                        api, options.config_dir, options.state_dir, options.policy_file
                    )
                )
            )
            return 0
        policy = provision_xen(
            api,
            options.config_dir,
            options.state_dir,
            email=options.email,
            enable_policy=options.enable_policy,
            policy_path=options.policy_file,
        )
        print(
            json.dumps(
                {
                    "review_repository": policy["git_remote"],
                    "base_commit": policy["base_commit"],
                    "policy_enabled": policy["enabled"],
                    "expires_at": policy["expires_at"],
                }
            )
        )
        runtime = ForgeAPI(
            policy["api_url"],
            token=read_private_token(options.config_dir / "import-token"),
        )
        for _round in range(options.bootstrap_rounds):
            result = run_import_worker_round(
                Ledger(options.state_dir / "mail.sqlite3"),
                options.state_dir / "xen.git",
                options.policy_file,
                options.state_dir / "worker.sqlite3",
                runtime,
                GitHttpCredentials(
                    IMPORTER, read_private_token(options.config_dir / "git-token")
                ),
            )
            print(json.dumps(result.__dict__))
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Setup stopped: {type(error).__name__}: {error}")
        return 1
