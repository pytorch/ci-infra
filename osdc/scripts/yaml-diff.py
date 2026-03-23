#!/usr/bin/env python3
"""YAML-aware semantic comparison of two files.

Usage: yaml-diff.py FILE1 FILE2

Exit codes:
    0 — files are semantically equal
    1 — files differ
    2 — error (missing file, invalid YAML)
"""

import sys

import yaml


def normalize_documents(path: str) -> list[str]:
    """Parse a YAML file and return sorted, normalized document strings."""
    with open(path) as f:
        docs = list(yaml.safe_load_all(f))
    docs = [d for d in docs if d is not None]
    return sorted(yaml.dump(d, sort_keys=True, default_flow_style=False) for d in docs)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: yaml-diff.py FILE1 FILE2", file=sys.stderr)
        return 2
    try:
        left = normalize_documents(sys.argv[1])
        right = normalize_documents(sys.argv[2])
    except FileNotFoundError as e:
        print(f"File not found: {e.filename}", file=sys.stderr)
        return 2
    except yaml.YAMLError as e:
        print(f"Invalid YAML: {e}", file=sys.stderr)
        return 2
    return 0 if left == right else 1


if __name__ == "__main__":
    sys.exit(main())
