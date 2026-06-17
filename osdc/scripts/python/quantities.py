"""Shared parsing of Kubernetes resource quantities.

Canonical home for converting Kubernetes quantity strings to numbers, so the
several generators that need it (nodepools, arc-runners, ...) share one tested
implementation instead of each carrying a copy.

Currently provides memory parsing; CPU/other quantity helpers can join here.
"""

from __future__ import annotations

# Kubernetes resource quantity suffixes → multiplier (bytes).
# Binary (Ki/Mi/Gi/Ti = powers of 1024) and decimal SI (K/M/G/T = powers of
# 1000) are both valid Kubernetes quantity forms.
_K8S_MEMORY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_memory_bytes(memory_str) -> int:
    """Convert a Kubernetes memory quantity to an exact integer byte count.

    Supports binary (Ki, Mi, Gi, Ti) and decimal (K, M, G, T) suffixes, plus a
    plain integer (already bytes). The mantissa must be a whole number —
    fractional quantities (e.g. "4.5Gi") raise ValueError so reservation math
    stays exact rather than silently truncating.

    >>> parse_memory_bytes("115Gi")
    123480309760
    >>> parse_memory_bytes("512Mi")
    536870912
    >>> parse_memory_bytes("500M")
    500000000
    >>> parse_memory_bytes("1024")
    1024
    >>> parse_memory_bytes(0)
    0
    """
    s = str(memory_str).strip()
    # Try two-char suffix first (Ki, Mi, Gi, Ti), then one-char (K, M, G, T).
    for suffix_len in (2, 1):
        if len(s) > suffix_len:
            suffix = s[-suffix_len:]
            if suffix in _K8S_MEMORY_SUFFIXES:
                return int(s[:-suffix_len]) * _K8S_MEMORY_SUFFIXES[suffix]
    return int(s)  # bare integer = bytes
