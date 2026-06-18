"""Strip or keep `# BEGIN_<tag>` / `# END_<tag>` conditional blocks in templates.

Shared helper used by both the ARC runner generator and the integration-test
workflow generator. Markers are matched on their stripped form so indentation
inside the template does not affect behavior.
"""


def strip_conditional_block(content: str, tag: str, keep: bool) -> str:
    """Remove or keep a `# BEGIN_<tag>` / `# END_<tag>` conditional block.

    When *keep* is False the block (markers + content between them) is stripped
    entirely. When *keep* is True the marker comment lines are removed but the
    content between them is preserved. Marker lines are matched on their stripped
    form, so indentation doesn't matter.
    """
    begin = f"# BEGIN_{tag}"
    end = f"# END_{tag}"
    lines = content.split("\n")
    filtered = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == begin:
            inside = True
            continue
        if stripped == end:
            inside = False
            continue
        if not keep and inside:
            continue
        filtered.append(line)
    return "\n".join(filtered)
