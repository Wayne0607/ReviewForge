"""CLI entry point."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="reviewforge", description="ReviewForge CLI")
    parser.add_argument("--config", default=None, help="Path to reviewforge.yaml")
    sub = parser.add_subparsers(dest="command")

    # serve
    serve_parser = sub.add_parser("serve", help="Start the API server")
    serve_parser.add_argument("--host", default=None, help="Override host")
    serve_parser.add_argument("--port", type=int, default=None, help="Override port")
    serve_parser.add_argument("--dev", action="store_true", help="Dev mode with hot reload")
    serve_parser.add_argument("--mock", action="store_true", help="Mock mode (no real LLM/GitHub)")

    # spec-check
    sub.add_parser("spec-check", help="Validate spec and config integrity")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        from reviewforge.app import create_app

        if args.mock:
            import os
            os.environ["REVIEWFORGE_MOCK"] = "1"

        app = create_app(config_path=args.config)

        host = args.host or "127.0.0.1"
        port = args.port or 8000

        if args.dev:
            uvicorn.run(
                "reviewforge.app:create_app",
                host=host, port=port, reload=True, factory=True,
            )
        else:
            uvicorn.run(app, host=host, port=port)

    elif args.command == "spec-check":
        from reviewforge.core.config import ReviewForgeConfig
        from reviewforge.core.specs import build_registry

        cfg = ReviewForgeConfig.load(args.config)
        registry = build_registry()

        errors = registry.validate()

        # Check config
        if not cfg.github.token:
            errors.append("GITHUB_TOKEN not set")
        if not cfg.llm.api_key:
            errors.append("LLM_API_KEY not set")

        # Check skills dir
        from pathlib import Path
        skills_path = Path(cfg.skills_dir)
        if skills_path.exists():
            skill_count = len(list(skills_path.glob("*/SKILL.md")))
            print(f"Skills found: {skill_count}")
        else:
            print(f"Skills dir not found: {cfg.skills_dir}")

        if errors:
            print("Spec validation FAILED:")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            print(f"Spec validation OK: {len(registry.agents)} agents, {len(registry.tools)} tools, {len(registry.skills)} skills")
            print(f"Config: model={cfg.llm.model}, reviewers={len(cfg.reviewers)}")

    else:
        parser.print_help()
