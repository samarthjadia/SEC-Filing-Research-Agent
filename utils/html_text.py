"""Turn SEC filing HTML into plain, searchable text.

Simply: a 10-Q is a web page full of tags, styles, and hidden XBRL data.
This strips all of that and leaves the words and numbers a person would read,
one paragraph per line. The section parser and the vector search use that text.

Technically: BeautifulSoup parses the HTML into a tree, we delete nodes that
are never document text (scripts, styles, inline-XBRL bookkeeping, anything
styled display:none), call get_text() with newline separators so block
elements stay on separate lines, then normalize whitespace.

Used by utils/edgar.py (live EDGAR downloads) and parsers/htmlToTxt.py (optional local HTML files).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

# Tags whose contents are never useful as searchable filing text.
DROP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "svg",
    "canvas",
    "link",
    "meta",
}

# Inline XBRL bookkeeping in SEC 10-K / 10-Q HTML. Removing it is not
# section extraction — it just keeps hidden taxonomy facts out of the txt.
DROP_XBRL_LOCAL_NAMES = {
    "hidden",      # ix:hidden
    "header",      # ix:header (resources, references, hidden facts)
    "references",  # ix:references
    "resources",   # ix:resources
}

# Block-ish tags: get_text uses these so paragraphs stay on separate lines.
BLOCK_SEPARATOR = "\n"


def local_name(tag_name: str) -> str:
    """ix:hidden → hidden; {namespace}hidden → hidden."""
    name = tag_name or ""
    if name.startswith("{") and "}" in name:
        name = name.split("}", 1)[1]
    if ":" in name:
        name = name.split(":", 1)[1]
    return name.lower()


def is_xbrl_bookkeeping(tag_name: str) -> bool:
    """True for inline-XBRL metadata tags (ix:hidden, ix:header, ...).

    Only namespaced tags are dropped so HTML <header> on IR pages is kept.
    """
    name = tag_name or ""
    namespaced = ":" in name or name.startswith("{")
    return namespaced and local_name(name) in DROP_XBRL_LOCAL_NAMES


def looks_display_none(style: str) -> bool:
    """True if an inline style hides the node (common on iXBRL wrappers)."""
    if not style:
        return False
    compact = style.lower().replace(" ", "")
    return "display:none" in compact or "visibility:hidden" in compact


def decode_html(raw: bytes) -> str:
    """Bytes → str. Filings are usually UTF-8; older ones may be Latin-1."""
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Strip tags / scripts / styles and return normalized plain text."""
    # html.parser is stdlib-backed and handles both IR pages and iXBRL.
    soup = BeautifulSoup(html, "html.parser")

    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()

    for tag in list(soup.find_all(True)):
        # Parent decompose() leaves detached children in this snapshot.
        if getattr(tag, "decomposed", False) or tag.parent is None:
            continue
        name = local_name(tag.name)
        if name in DROP_TAGS or is_xbrl_bookkeeping(tag.name):
            tag.decompose()
            continue
        style = (tag.attrs or {}).get("style", "")
        if looks_display_none(style or ""):
            tag.decompose()

    text = soup.get_text(separator=BLOCK_SEPARATOR)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    """Collapse messy HTML spacing into readable, searchable lines."""
    text = (
        text.replace("\u00a0", " ")  # &nbsp;
        .replace("\u200b", "")       # zero-width space
        .replace("\ufeff", "")       # BOM
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    cleaned_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines))
    return collapsed.strip() + "\n"
