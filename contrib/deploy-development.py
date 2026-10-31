"""Deploy pushed source to the disposable local Forgejo review experiment."""

import argparse
import configparser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def run(*arguments, cwd=None):
    return subprocess.run(arguments, cwd=cwd, check=True)


def revision(root):
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True
    ).strip():
        raise ValueError(f"Commit and push tracked source changes first: {root}")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != upstream:
        raise ValueError(f"Push the source revision before deployment: {root}")
    return head


def write_config(source, destination, static_root):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read_string("[DEFAULT]\n" + source.read_text())
    config["server"]["STATIC_ROOT_PATH"] = str(static_root)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        config.write(output, space_around_delimiters=True)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forgejo-root", type=Path, default=Path.home() / "forgejo")
    parser.add_argument("--forgejo-config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args(arguments)
    companion = Path(__file__).resolve().parent.parent
    forgejo = options.forgejo_root.resolve()
    companion_revision = revision(companion)
    forgejo_revision = revision(forgejo)
    profile = companion / "lore_review_companion/policies/xen-development.json"
    policy = json.loads(profile.read_text())
    if policy["owner"] != "xen-review" or policy["repository"] != "xen":
        raise ValueError(
            "This deployer is scoped to the xen-review/xen development fixture"
        )
    if not options.apply:
        print(
            json.dumps(
                {
                    "forgejo_revision": forgejo_revision,
                    "companion_revision": companion_revision,
                    "policy": str(profile),
                    "action": "build, install, restart, verify worker and resume timer",
                }
            )
        )
        return 0
    run(
        "systemctl",
        "--user",
        "stop",
        "lore-review-companion.timer",
        "lore-review-companion.service",
    )
    run("make", "TAGS=sqlite sqlite_unlock_notify", "build", cwd=forgejo)
    share = Path.home() / ".local/share"
    release = share / "forgejo-review/releases" / forgejo_revision[:12]
    release.mkdir(parents=True, exist_ok=True)
    shutil.copy2(forgejo / "gitea", release / "gitea")
    for name in ("options", "public", "templates"):
        shutil.copytree(forgejo / name, release / name, dirs_exist_ok=True)
    config = (
        Path.home()
        / ".config/forgejo-review/releases"
        / forgejo_revision[:12]
        / "app.ini"
    )
    write_config(options.forgejo_config, config, release)
    runtime = share / "lore-review-companion"
    interpreter = runtime / ".venv/bin/python"
    if not interpreter.exists():
        run("uv", "venv", "--python", sys.executable, str(runtime / ".venv"))
    run(
        "uv",
        "pip",
        "install",
        "--python",
        str(interpreter),
        "--reinstall-package",
        "lore-review-companion",
        str(companion),
    )
    run(str(interpreter), "-m", "lore_review_companion.cli", "check-b4-runtime")
    installed_profile = subprocess.check_output(
        [
            str(interpreter),
            "-c",
            "from lore_review_companion.import_policy import DEFAULT_POLICY_PATH; print(DEFAULT_POLICY_PATH)",
        ],
        cwd=runtime,
        text=True,
    ).strip()
    if Path(installed_profile).read_bytes() != profile.read_bytes():
        raise ValueError("Installed policy differs from committed source")
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    for name in ("lore-review-companion.service", "lore-review-companion.timer"):
        shutil.copy2(companion / "contrib" / name, units / name)
    override = units / "forgejo-review.service.d/90-development.conf"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        f"[Service]\nExecStart=\nExecStart={release}/gitea web --config {config} "
        f"--custom-path {forgejo}/custom --work-path {forgejo}\n"
    )
    state = Path.home() / ".local/state/lore-review-companion"
    for obsolete in (
        state / "worker.sqlite3.incident",
        Path.home() / ".config/lore-review-companion/import-policy.json",
    ):
        obsolete.unlink(missing_ok=True)
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "restart", "forgejo-review.service")
    version = json.loads(
        subprocess.check_output(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--retry",
                "20",
                "--retry-connrefused",
                "--retry-delay",
                "1",
                "--max-time",
                "10",
                policy["api_url"] + "version",
            ],
            text=True,
        )
    )
    run("systemctl", "--user", "reset-failed", "lore-review-companion.service")
    run("systemctl", "--user", "start", "lore-review-companion.service")
    run("systemctl", "--user", "enable", "--now", "lore-review-companion.timer")
    print(
        json.dumps(
            {
                "forgejo_revision": forgejo_revision,
                "companion_revision": companion_revision,
                "installed_policy": installed_profile,
                "server_version": version,
                "timer": "enabled",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
