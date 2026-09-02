"""Build the deterministic tarball consumed by ``test_workspace``.

Usage::

    python build_tarball.py [output.tar.gz]

The archive includes a GitHub-like single wrapper directory so the workspace
also exercises archive-root stripping.
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path


def build(output: Path) -> None:
    source = Path(__file__).resolve().parent
    output.parent.mkdir(parents=True, exist_ok=True)
    wrapper = "workspace-repo-test"
    with tarfile.open(output, "w:gz") as archive:
        wrapper_info = tarfile.TarInfo(wrapper)
        wrapper_info.type = tarfile.DIRTYPE
        wrapper_info.mode = 0o755
        wrapper_info.mtime = 1_700_000_000
        archive.addfile(wrapper_info)
        for path in sorted(source.rglob("*")):
            if (
                path.name in {"build_tarball.py", output.name}
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
                or not path.is_file()
            ):
                continue
            relative = path.relative_to(source).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{wrapper}/{relative}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 1_700_000_000
            archive.addfile(info, io.BytesIO(data))


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_suffix(".tar.gz")
    build(destination)
