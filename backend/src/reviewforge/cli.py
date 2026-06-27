"""CLI entry point."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="reviewforge", description="ReviewForge CLI")
    sub = parser.add_subparsers(dest="command")

    # serve
    serve_parser = sub.add_parser("serve", help="Start the API server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--dev", action="store_true", help="Dev mode with hot reload")

    # spec-check
    sub.add_parser("spec-check", help="Validate spec integrity")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        from reviewforge.app import create_app

        app = create_app()
        uvicorn.run(
            "reviewforge.app:create_app",
            host=args.host,
            port=args.port,
            reload=args.dev,
            factory=True,
        )

    elif args.command == "spec-check":
        from reviewforge.core.specs import build_registry
        registry = build_registry()
        errors = registry.validate()
        if errors:
            print("Spec validation FAILED:")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            print(f"Spec validation OK: {len(registry.agents)} agents, {len(registry.tools)} tools, {len(registry.skills)} skills")

    else:
        parser.print_help()
