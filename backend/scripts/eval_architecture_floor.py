"""Build a deterministic offline architecture-floor artifact from judged observations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from reviewforge.eval.architecture_floor import ArchitectureFloorError, build_architecture_floor


def _atomic_write(path: Path, text: str) -> None:
    """Durably write beside the destination, then atomically replace it."""
    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            file_descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate an offline ReviewForge architecture-floor experiment.")
    parser.add_argument("--input", required=True, help="Architecture-floor input JSON artifact.")
    parser.add_argument("--out", default="", help="Optional path for the new output artifact.")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.out).resolve() if args.out else None
    output_aliases_input = output_path == input_path or (
        output_path is not None and output_path.exists() and input_path.exists() and output_path.samefile(input_path)
    )
    if output_aliases_input:
        parser.error("--out must differ from --input; source and legacy artifacts are never overwritten")

    try:
        experiment = json.loads(input_path.read_text(encoding="utf-8"))
        result = build_architecture_floor(experiment)
    except (OSError, json.JSONDecodeError, ArchitectureFloorError) as exc:
        parser.error(str(exc))

    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if output_path is not None:
        try:
            _atomic_write(output_path, text + "\n")
        except OSError as exc:
            parser.error(f"failed to write {output_path}: {exc}")
    print(text)


if __name__ == "__main__":
    main()
