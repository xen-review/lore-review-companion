# SPDX-License-Identifier: GPL-2.0-or-later

"""Public, source-controlled configuration for the development import worker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_POLICY_PATH = Path(__file__).with_name("policies") / "xen-development.json"


@dataclass(frozen=True)
class ImportPolicy:
    version: int
    enabled: bool
    expires_at: str | None
    api_url: str
    owner: str
    repository: str
    repository_id: int
    importer_id: int
    importer_login: str
    source_url: str
    nntp_host: str
    nntp_group: str
    git_remote: str
    git_username: str
    base_commit: str
    target_branch: str
    message_limit: int
    revision_limit: int
    max_inline_lines: int
    lookback_hours: int
    notifications: str
    identity_rule: str
    retry_uncertain: bool

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def assert_current(self, path: Path, capabilities: dict) -> None:
        if read_import_policy(path) != self:
            raise ValueError(
                "Import policy changed; stop and review the new worker authorization"
            )
        if capabilities.get("unattended_allowed") is not True or any(
            capabilities.get(name) != getattr(self, name)
            for name in (
                "repository_id",
                "importer_id",
                "importer_login",
                "source_url",
            )
        ):
            raise ValueError(
                "Repository/importer/source authorization no longer matches the import policy"
            )
        if not {"pull", "conversation", "review", "review-reply"} <= set(
            capabilities.get("operations", ())
        ):
            raise ValueError("Required import capabilities were revoked")
        if (
            type(capabilities.get("max_inline_lines")) is not int
            or capabilities["max_inline_lines"] < self.max_inline_lines
        ):
            raise ValueError(
                "Repository inline capability is below the reviewed policy budget"
            )


def read_import_policy(path: Path) -> ImportPolicy:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o022
                or not 1 <= info.st_size <= 16384
            ):
                raise ValueError(
                    "Import policy must be a regular file without group/world write permission"
                )
            raw = os.read(descriptor, 16385)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError("Import policy is unavailable or revoked") from error

    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Import policy contains a duplicate field")
            result[name] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        policy = ImportPolicy(**value)
        expires = (
            datetime.fromisoformat(policy.expires_at)
            if policy.expires_at is not None
            else None
        )
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Import policy has an invalid schema") from error
    if (
        type(policy.version) is not int
        or policy.version != 1
        or policy.enabled is not True
    ):
        raise ValueError("Import policy is disabled or unsupported")
    if expires is not None and (
        expires.tzinfo is None or expires <= datetime.now(timezone.utc)
    ):
        raise ValueError(
            "Import policy expiry must be an explicit future timezone-aware time"
        )
    limits = {
        "message_limit": (1, 1000),
        "revision_limit": (1, 32),
        "max_inline_lines": (1, 50),
        "lookback_hours": (1, 8760),
    }
    for name, (minimum, maximum) in limits.items():
        if (
            type(getattr(policy, name)) is not int
            or not minimum <= getattr(policy, name) <= maximum
        ):
            raise ValueError("Import policy resource budget is invalid")
    if any(
        type(getattr(policy, name)) is not int or getattr(policy, name) <= 0
        for name in ("repository_id", "importer_id")
    ):
        raise ValueError(
            "Import policy requires numeric repository and importer identities"
        )
    if (
        not isinstance(policy.base_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", policy.base_commit) is None
        or policy.notifications != "disabled"
        or policy.identity_rule != "isolated-unverified"
        or type(policy.retry_uncertain) is not bool
    ):
        raise ValueError(
            "Import policy must pin its base, identity rule and disabled notifications"
        )
    for name in (
        "api_url",
        "owner",
        "repository",
        "importer_login",
        "source_url",
        "git_remote",
        "git_username",
        "target_branch",
        "nntp_host",
        "nntp_group",
    ):
        text = getattr(policy, name)
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 2048
            or any(ord(character) < 33 for character in text)
        ):
            raise ValueError("Import policy destination is invalid")
    return policy
