from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run(args: list[str], cwd: Path, *, check: bool = True) -> CommandResult:
    process = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    result = CommandResult(process.returncode, process.stdout.strip(), process.stderr.strip())
    if check and process.returncode != 0:
        raise RuntimeError(
            f"Command failed ({process.returncode}): {' '.join(args)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def repository_root(path: Path) -> Path:
    result = _run(["git", "rev-parse", "--show-toplevel"], path)
    return Path(result.stdout)


def configure_identity(repo: Path) -> None:
    name = os.getenv("GIT_COMMIT_NAME", "Policy Opportunity Agent")
    email = os.getenv("GIT_COMMIT_EMAIL", "policy-agent@localhost")
    _run(["git", "config", "user.name", name], repo)
    _run(["git", "config", "user.email", email], repo)


def pull(repo: Path, remote: str, branch: str) -> bool:
    """Rebase from the data branch without preventing a local collection on network failure."""
    if os.getenv("GIT_PULL_BEFORE_RUN", "true").lower() not in {"1", "true", "yes"}:
        return False
    result = _run(
        ["git", "pull", "--rebase", "--autostash", remote, branch],
        repo,
        check=False,
    )
    if result.returncode == 0:
        return True
    required = os.getenv("GIT_PULL_REQUIRED", "false").lower() in {"1", "true", "yes"}
    message = (
        f"Git pull failed; {'aborting' if required else 'continuing with local collection'}. "
        f"stderr: {result.stderr or 'none'}"
    )
    if required:
        raise RuntimeError(message)
    LOGGER.warning(message)
    return False


def commit_and_push(repo: Path, data_path: Path, run_id: str, remote: str, branch: str) -> bool:
    configure_identity(repo)
    relative = data_path.resolve().relative_to(repo.resolve())
    _run(["git", "add", "--", str(relative)], repo)

    staged = _run(["git", "diff", "--cached", "--quiet"], repo, check=False)
    # git diff --quiet: 0 means no difference, 1 means differences, >1 means an error.
    if staged.returncode == 0:
        LOGGER.info("No data changes to commit.")
        return False
    if staged.returncode > 1:
        raise RuntimeError(f"Unable to inspect staged changes: {staged.stderr}")

    message = os.getenv("GIT_COMMIT_MESSAGE") or f"data: policy opportunity refresh {run_id}"
    _run(["git", "commit", "-m", message], repo)

    first_push = _run(["git", "push", remote, f"HEAD:{branch}"], repo, check=False)
    if first_push.returncode == 0:
        return True

    LOGGER.warning("Initial push failed; rebasing once and retrying.")
    _run(["git", "pull", "--rebase", "--autostash", remote, branch], repo)
    _run(["git", "push", remote, f"HEAD:{branch}"], repo)
    return True
