#!/usr/bin/env python3
"""
Read process and CI inputs.

Shared by build_artifact, publish_to_modrinth, and import_modrinth_history.

This module is intentionally tiny so that every consumer can import the same
helpers without duplicating environment-variable or GitHub-Actions output
logic. All values are treated as strings; callers that need other types
perform the conversion themselves.
"""

from __future__ import annotations

import os


def env(name: str, default: str | None = None) -> str:
    """
    Read an environment variable.

    If default is None and the variable is missing (or explicitly set to
    None), abort the process with SystemExit so that CI logs show a clear
    missing-input error. When a default string is supplied, that value is
    returned instead of raising.
    """
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def write_github_output(name: str, value: str) -> None:
    """
    Append a workflow step output (supports multiline via heredoc).

    When GITHUB_OUTPUT is unset (local runs or unit tests), the call is a
    no-op that prints a skip message. Multiline values are written with the
    fixed delimiter OUTPUT_END so that the Actions runtime can reassemble
    them; callers must not place the literal string OUTPUT_END inside the
    value itself.
    """
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
