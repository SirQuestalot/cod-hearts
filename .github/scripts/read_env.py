#!/usr/bin/env python3
"""
Read process and CI inputs.

Shared by build_artifact, publish_to_modrinth, and import_modrinth_history.
"""

from __future__ import annotations

import os


def env(name: str, default: str | None = None) -> str:
    """
    Read an environment variable.
    If default is None and the var is missing, abort with SystemExit.
    """
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def write_github_output(name: str, value: str) -> None:
    """Append a workflow step output (supports multiline via heredoc)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"GITHUB_OUTPUT not set; skip output {name}")
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<OUTPUT_END\n")
            fh.write(value)
            if not value.endswith("\n"):
                fh.write("\n")
            fh.write("OUTPUT_END\n")
        else:
            fh.write(f"{name}={value}\n")
