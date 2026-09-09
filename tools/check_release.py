"""Validate the public package version and, when supplied, its release ref."""

import argparse
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="Full Git ref, e.g. refs/tags/v0.0.1")
    parser.add_argument("--notes-out", type=Path, help="Write only this version's release notes")
    args = parser.parse_args()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if project["name"] != "appguard-runtime":
        parser.error("Only the public appguard-runtime distribution may be released")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        parser.error("Use a stable MAJOR.MINOR.PATCH version")
    if args.ref is not None and args.ref != f"refs/tags/v{version}":
        parser.error(f"Release ref must be refs/tags/v{version}, got {args.ref!r}")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    if not re.search(rf"^## \[{re.escape(version)}\](?: |$)", changelog, re.MULTILINE):
        parser.error(f"CHANGELOG.md is missing an entry for {version}")
    if args.notes_out:
        notes = re.search(rf"^## \[{re.escape(version)}\].*?(?=^## |\Z)",
                          changelog, re.MULTILINE | re.DOTALL).group()
        args.notes_out.write_text(notes.strip() + "\n")
    print(f"Validated {project['name']} {version}")


if __name__ == "__main__":
    main()
