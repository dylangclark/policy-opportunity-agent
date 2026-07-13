from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_agent_config, load_yaml
from .git_publish import commit_and_push, pull, repository_root
from .lock import AlreadyRunningError, exclusive_lock
from .models import Manifest
from .pipeline import run_pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Canadian policy events and identify op-ed hooks.")
    parser.add_argument("--config", type=Path, default=Path("config/sources.yml"))
    parser.add_argument("--rules", type=Path, default=Path("config/rules.yml"))
    parser.add_argument("--output", type=Path, default=Path("docs/data"))
    parser.add_argument("--state-dir", type=Path, default=Path(".state"))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))

    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run collectors and write JSON output.")
    run.add_argument("--now", help="Override current time with an ISO-8601 timestamp (testing only).")

    publish = subparsers.add_parser("publish", help="Pull, run, commit data, and push to GitHub.")
    publish.add_argument("--remote", default=os.getenv("GIT_REMOTE", "origin"))
    publish.add_argument("--branch", default=os.getenv("GIT_BRANCH", "main"))

    subparsers.add_parser("validate", help="Validate the current manifest.")
    subparsers.add_parser("list-sources", help="List configured sources.")
    return parser


def _now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_agent_config(args.config)
    rules = load_yaml(args.rules)

    if args.command == "list-sources":
        for source in config.sources:
            status = "enabled" if source.get("enabled", True) else "disabled"
            print(f"{source['id']}\t{source['collector']}\t{status}\t{source['url']}")
        return 0

    if args.command == "validate":
        payload = json.loads((args.output / "manifest.json").read_text(encoding="utf-8"))
        manifest = Manifest.model_validate(payload)
        print(json.dumps(manifest.model_dump(mode="json"), indent=2))
        return 0

    if args.command == "run":
        manifest = run_pipeline(
            agent_config=config,
            rules=rules,
            output_dir=args.output,
            state_dir=args.state_dir,
            now=_now(args.now),
        )
        print(json.dumps(manifest.model_dump(mode="json"), indent=2))
        return 0 if manifest.status != "failed" else 2

    if args.command == "publish":
        try:
            with exclusive_lock(args.state_dir / "publish.lock"):
                repo = repository_root(Path.cwd())
                pull(repo, args.remote, args.branch)
                manifest = run_pipeline(
                    agent_config=config,
                    rules=rules,
                    output_dir=args.output,
                    state_dir=args.state_dir,
                )
                pushed = commit_and_push(repo, args.output, manifest.run_id, args.remote, args.branch)
        except AlreadyRunningError as exc:
            logging.getLogger(__name__).warning("%s", exc)
            return 0
        print(json.dumps({"run_id": manifest.run_id, "status": manifest.status, "pushed": pushed}, indent=2))
        return 0 if manifest.status != "failed" else 2

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
