"""Tests for conditional_blocks.strip_conditional_block."""

import textwrap

from conditional_blocks import strip_conditional_block


class TestStripConditionalBlock:
    def test_keep_true_removes_markers_preserves_content(self):
        content = textwrap.dedent("""\
            before
                # BEGIN_FOO
                inside
                # END_FOO
            after
        """)
        result = strip_conditional_block(content, "FOO", keep=True)
        assert "# BEGIN_FOO" not in result
        assert "# END_FOO" not in result
        assert "inside" in result
        assert "before" in result
        assert "after" in result

    def test_keep_false_removes_markers_and_content(self):
        content = textwrap.dedent("""\
            before
                # BEGIN_FOO
                inside
                # END_FOO
            after
        """)
        result = strip_conditional_block(content, "FOO", keep=False)
        assert "# BEGIN_FOO" not in result
        assert "# END_FOO" not in result
        assert "inside" not in result
        assert "before" in result
        assert "after" in result

    def test_no_markers_in_content_is_noop(self):
        content = "just\nplain\ntext\n"
        assert strip_conditional_block(content, "FOO", keep=True) == content
        assert strip_conditional_block(content, "FOO", keep=False) == content

    def test_indentation_does_not_matter(self):
        """Markers are matched on stripped form so any indentation works."""
        content = "x\n            # BEGIN_BAR\nkeepme\n            # END_BAR\ny\n"
        out_keep = strip_conditional_block(content, "BAR", keep=True)
        assert "# BEGIN_BAR" not in out_keep
        assert "keepme" in out_keep
        out_strip = strip_conditional_block(content, "BAR", keep=False)
        assert "keepme" not in out_strip
