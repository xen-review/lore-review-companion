"""Identify the actual source checkout or packaged build posting an import."""

from pathlib import Path
import re
import subprocess


def checkout_revision(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%H%n%s", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha, subject = result.stdout.strip().split("\n", 1)
        if re.fullmatch(r"[0-9a-f]{40,64}", sha) is None:
            return ""
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:80].rstrip("-")
    return f"{sha[:12]}{'-dirty' if status.stdout else ''}/{slug or 'build'}"


def companion_revision() -> str:
    try:
        from lore_review_companion._build_identity import REVISION
    except ImportError:
        return checkout_revision(Path(__file__).resolve().parent.parent)
    return REVISION
