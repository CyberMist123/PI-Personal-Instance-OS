from cmx_mcp.compact import strip_html


def test_strip_html_preserves_paragraphs():
    assert strip_html("<p>one<br>two</p><p>three</p>") == "one\ntwo\nthree"
