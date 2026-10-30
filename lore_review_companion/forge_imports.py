"""Explicitly scoped, idempotent Forgejo review-import operations."""

from __future__ import annotations

import re
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError
from lore_review_companion.forge_comments import DeliveryUncertainError


@dataclass(frozen=True)
class ImportDeliveryResult:
    remote_object: dict[str, Any]
    planned: bool = False


def _positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ForgeImportClient:
    def __init__(
        self, api: ForgeAPI, owner: str, repo: str, *, source_url: str,
        write_enabled: bool = False,
    ):
        source = urlsplit(source_url)
        if (
            not owner or not repo or source.scheme != "https" or not source.hostname
            or source.username is not None or source.password is not None
            or source.query or source.fragment or not source.path.endswith("/")
            or "%" in source.path
        ):
            raise ValueError("A repository and credential-free HTTPS source inbox are required")
        self.api = api
        self.owner, self.repo = owner, repo
        self.repository_path = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        self.path = f"{self.repository_path}/review-imports"
        self.source_url = source_url
        self.write_enabled = write_enabled

    def capabilities(self) -> dict[str, Any]:
        result = self.api.request_json(f"{self.path}/capabilities")
        if (
            not isinstance(result, dict) or type(result.get("version")) is not int
            or result["version"] != 1 or result.get("source_url") != self.source_url
            or not _positive_id(result.get("repository_id"))
            or not _positive_id(result.get("importer_id"))
            or not isinstance(result.get("importer_login"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", result["importer_login"]) is None
            or not isinstance(result.get("operations"), list)
            or any(not isinstance(operation, str) for operation in result["operations"])
        ):
            raise ValueError("Unsupported or mismatched review-import capability response")
        return result

    @staticmethod
    def _operation_key(value: Any) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
            raise ValueError("A bounded path-safe operation key is required")
        return value

    def lookup_operation(self, operation_key: str) -> dict[str, Any] | None:
        operation_key = self._operation_key(operation_key)
        capabilities = self.capabilities()
        try:
            result = self.api.request_json(f"{self.path}/operations/{quote(operation_key, safe='')}")
        except ForgeAPIError as error:
            if error.status == 404:
                return None
            raise
        if (
            not isinstance(result, dict) or not _positive_id(result.get("id"))
            or not _positive_id(result.get("repository_id"))
            or result.get("repository_id") != capabilities["repository_id"]
            or result.get("operation_key") != operation_key
        ):
            raise ValueError("Mismatched imported-operation lookup response")
        return result

    def comparison(self, pull_index: int, *, after_commit: str | None = None) -> dict[str, Any]:
        if not _positive_id(pull_index) or (after_commit is not None and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", after_commit) is None):
            raise ValueError("A positive PR index and full patch commit are required")
        self.capabilities()
        suffix = "comparison" if after_commit is None else f"patches/{after_commit}"
        result = self.api.request_json(f"{self.path}/pulls/{pull_index}/{suffix}")
        if not isinstance(result, dict) or any(
            not isinstance(result.get(field), str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", result[field]) is None
            for field in ("head_commit", "base_commit", "merge_base")
        ):
            raise ValueError("Forgejo returned an invalid imported PR comparison")
        if after_commit is not None and (
            result.get("after_commit") != after_commit or not isinstance(result.get("before_commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", result["before_commit"]) is None
            or not isinstance(result.get("diff"), str)
            or hashlib.sha256(result["diff"].encode("utf-8")).hexdigest() != result.get("diff_sha256")
        ):
            raise ValueError("Forgejo returned an unverified exact patch comparison")
        return result

    def import_conversation(self, pull_index: int, payload: Mapping[str, Any]) -> ImportDeliveryResult:
        return self._import(pull_index, payload, "conversation", "comments")

    def import_review(self, pull_index: int, payload: Mapping[str, Any]) -> ImportDeliveryResult:
        return self._import(pull_index, payload, "review", "reviews")

    def import_reply(self, pull_index: int, payload: Mapping[str, Any]) -> ImportDeliveryResult:
        return self._import(pull_index, payload, "review-reply", "replies")

    def import_pull(self, payload: Mapping[str, Any]) -> ImportDeliveryResult:
        return self._import(None, payload, "pull", "pulls")

    def _import(self, pull_index: int | None, payload: Mapping[str, Any], operation: str, route: str) -> ImportDeliveryResult:
        if operation != "pull" and not _positive_id(pull_index):
            raise ValueError("A positive pull request index is required")
        operation_key = self._operation_key(payload.get("operation_key"))
        payload = dict(payload)
        path = f"{self.path}/pulls" if operation == "pull" else f"{self.path}/pulls/{pull_index}/{route}"
        for attempt in range(2):
            try:
                capabilities = self.capabilities()
                if operation not in capabilities["operations"]:
                    raise ValueError(f"{operation} imports are not supported by this repository")
            except (ForgeAPIError, ValueError) as error:
                if attempt:
                    raise DeliveryUncertainError("Import acceptance could not be confirmed after capability recheck") from error
                raise
            if not self.write_enabled:
                return ImportDeliveryResult({"path": path, **payload}, planned=True)
            try:
                result = self.api.request_json(path, method="POST", payload=payload)
            except ForgeAPIError as error:
                if attempt:
                    raise DeliveryUncertainError("Import acceptance could not be confirmed by idempotent replay") from error
                if error.status is None or error.status >= 500 or 200 <= error.status < 300:
                    continue
                raise
            if (
                not isinstance(result, dict) or not _positive_id(result.get("id"))
                or (operation != "pull" and not _positive_id(result.get("comment_id")))
                or not _positive_id(result.get("participant_id"))
                or not _positive_id(result.get("repository_id")) or not _positive_id(result.get("importer_id"))
                or result.get("repository_id") != capabilities["repository_id"]
                or result.get("importer_id") != capabilities["importer_id"]
                or result.get("type") != operation or result.get("operation_key") != operation_key
                or result.get("message_id") != payload.get("message_id")
                or result.get("message_sha256") != payload.get("message_sha256")
                or not isinstance(result.get("source_url"), str)
                or unquote(result["source_url"]) != self.source_url + str(payload.get("message_id")) + "/"
            ):
                raise DeliveryUncertainError("Forgejo returned an unconfirmed imported operation")
            if operation == "pull" and (
                not _positive_id(result.get("pull_index")) or not _positive_id(result.get("issue_id"))
                or any(result.get(field) != payload.get(field) for field in (
                    "head_commit", "base_commit", "head_branch", "base_branch",
                ))
            ):
                raise DeliveryUncertainError("Forgejo returned an unconfirmed imported PR comparison")
            if operation in ("review", "review-reply") and not _positive_id(result.get("review_id")):
                raise DeliveryUncertainError("Forgejo returned an unconfirmed imported review")
            if operation == "review":
                comments, identifiers = payload.get("comments"), result.get("code_comment_ids")
                if (
                    not isinstance(comments, list) or not comments or not isinstance(identifiers, list)
                    or len(identifiers) != len(comments) or any(not _positive_id(value) for value in identifiers)
                    or len(set(identifiers)) != len(identifiers)
                ):
                    raise DeliveryUncertainError("Forgejo returned unconfirmed inline comment identifiers")
            return ImportDeliveryResult(result)
        raise AssertionError("Unreachable import retry state")