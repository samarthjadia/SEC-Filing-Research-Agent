"""Pull the narrative sections out of a 10-K or 10-Q before they are embedded.

A filing's plain text still contains the cover page, the table of contents,
exhibits, and signatures. Those pages mention the same words as the real
discussion ("revenue", "risk") and would crowd out the passage the question
is about. This module keeps the items that carry the qualitative story and
returns each one as its own block of text.

Headings are detected by item number, not by the full title. "Item 7." and
"Item 7. Management's Discussion and Analysis of Financial Condition and
Results of Operations" are the same item. A line counts as a heading only
when it is short and starts with "Item". A sentence that merely mentions
"Item 1A of the 2025 Form 10-K" stays inside the paragraph it belongs to.

The table of contents uses the same item lines, followed by a page number.
Those stubs are a few characters long, so any section shorter than
_MIN_BODY_CHARS is dropped. When the same item appears twice, the longer
copy is the body and the shorter copy is the contents entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Item numbers to keep. The title here is the label we store with a chunk.
# 10-Q numbering repeats across parts, so the part is required there:
# Part I Item 1 is the financial statements, Part II Item 1 is legal proceedings.
SECTION_CONFIG: dict[str, dict] = {
    "10-K": {
        "1": "Business",
        "1A": "Risk Factors",
        "7": "Management's Discussion and Analysis",
        "7A": "Market Risk",
        "8": "Financial Statements",
    },
    "10-Q": {
        "I": {
            "1": "Financial Statements",
            "2": "Management's Discussion and Analysis",
            "3": "Market Risk",
        },
        "II": {
            "1A": "Risk Factors",
        },
    },
}

# Optional labels inside MD&A. Companies omit some of these, and that is fine:
# the rest of the item is still kept under the item title.
MDNA_SUBSECTIONS = [
    "Results of Operations",
    "Liquidity and Capital Resources",
    "Critical Accounting Estimates",
    "Critical Accounting Policies",
    "Cash Flows",
    "Segment Results",
    "Segment Performance",
    "Business Overview",
]

# Note titles we hope to recognize inside the financial statements. A note
# whose title does not contain one of these is still kept; the keyword only
# marks the ones that usually answer debt, revenue, and commitment questions.
FINANCIAL_NOTE_KEYWORDS = [
    "Debt",
    "Borrowings",
    "Commitments and Contingencies",
    "Revenue",
    "Segment",
]

# A table-of-contents entry is an item line plus a page number. Real sections
# are much longer than this.
_MIN_BODY_CHARS = 80

_ITEM_HEADING = re.compile(r"^item\s+(\d+[a-z]?)\.?\s*(.*)$", re.IGNORECASE)
_PART_HEADING = re.compile(r"^part\s+(i{1,3}|iv)\b", re.IGNORECASE)
_NOTE_HEADING = re.compile(r"^note\s+(\d+)\b[.\s:—\-]*(.*)$", re.IGNORECASE)

# 10-K Item 7 and 10-Q Part I Item 2 are the two MD&A sections.
_MDNA_ITEMS = {("10-K", "7"), ("10-Q", "2")}
# 10-K Item 8 and 10-Q Part I Item 1 hold the statements and the notes.
_NOTE_ITEMS = {("10-K", "8"), ("10-Q", "1")}


@dataclass(frozen=True)
class FilingSection:
    """One kept block of a filing, ready to be split into embedding chunks."""

    form: str
    item: str
    title: str
    subsection: str | None
    text: str
    # 10-Q item numbers repeat. Part I Item 1 is the financial statements;
    # Part II Item 1A is risk factors. 10-K item numbers do not repeat.
    part: str | None = None

    @property
    def label(self) -> str:
        """Short name stored with the chunk and shown in front of its text.

        A 10-Q label names the part, because "Item 1A" alone does not say
        which part it came from: "10-Q Part II, Item 1A | Risk Factors".
        """
        name = self.title if not self.subsection else f"{self.title} — {self.subsection}"
        if self.form == "10-Q" and self.part:
            return f"{self.form} Part {self.part}, Item {self.item} | {name}"
        if self.item:
            return f"{self.form} Item {self.item} | {name}"
        return f"{self.form} | {name}"


def _plain(value: str) -> str:
    """Ignore curly quotes and dashes so "Company's" matches "Company's"."""
    return (
        value.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .strip()
        .lower()
    )


def _canonical_title(form: str, part: str | None, item: str) -> str | None:
    """Return the configured title for this item, or None when we skip it."""
    item = item.upper()
    if form == "10-K":
        return SECTION_CONFIG["10-K"].get(item)
    if form != "10-Q":
        return None
    part_items = SECTION_CONFIG["10-Q"].get(part or "")
    if not isinstance(part_items, dict):
        return None
    return part_items.get(item)


def _heading(line: str) -> tuple[str, str] | None:
    """Parse a heading line into (item number, title). None for ordinary lines.

    The title must look like a heading. "Item 1A of the prior 10-K" starts
    with a lowercase word, so it is a cross-reference and not a new section.
    """
    if len(line) > 180:
        return None
    match = _ITEM_HEADING.match(line.strip())
    if match is None:
        return None
    item = match.group(1).upper()
    title = match.group(2).strip(" .")
    if title and not title[0].isupper() and not title.startswith("["):
        return None
    return item, title


def _subsection_name(line: str) -> str | None:
    """Return the MD&A subsection title when this line is that heading."""
    if len(line) > 90:
        return None
    cleaned = _plain(line).rstrip(".:")
    for name in MDNA_SUBSECTIONS:
        target = _plain(name)
        if cleaned == target:
            return name
    return None


def _note_name(line: str) -> str | None:
    """Return a note label such as 'Note 5 — Debt' when this line starts a note."""
    if len(line) > 160:
        return None
    match = _NOTE_HEADING.match(line.strip())
    if match is None:
        return None
    number = match.group(1)
    title = match.group(2).strip(" .")
    keyword = next((word for word in FINANCIAL_NOTE_KEYWORDS if word.lower() in title.lower()), None)
    if keyword and title:
        return f"Note {number} — {title}"
    if title:
        return f"Note {number} — {title}"
    return f"Note {number}"


def _split_labeled(section: FilingSection, heading_for) -> list[FilingSection]:
    """Break one item into subsection blocks. The item is kept whole when no headings exist."""
    blocks: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in section.text.splitlines():
        name = heading_for(line)
        if name is None:
            blocks[-1][1].append(line)
            continue
        blocks.append((name, []))
    pieces: list[FilingSection] = []
    for name, lines in blocks:
        body = "\n".join(lines).strip()
        if len(body) < _MIN_BODY_CHARS:
            continue
        pieces.append(
            FilingSection(section.form, section.item, section.title, name, body, section.part)
        )
    merged: list[FilingSection] = []
    for piece in pieces:
        # A note title is reprinted at the top of each page. Join those pieces
        # so one note stays one block instead of a block per page.
        if merged and piece.subsection and merged[-1].subsection == piece.subsection:
            previous = merged[-1]
            merged[-1] = FilingSection(
                previous.form,
                previous.item,
                previous.title,
                previous.subsection,
                previous.text + "\n" + piece.text,
                previous.part,
            )
            continue
        merged.append(piece)
    return merged or [section]


def extract_sections(text: str, form: str) -> list[FilingSection]:
    """Return the kept sections of a 10-K or 10-Q, in filing order.

    An empty list means no headings matched. The caller then embeds the
    whole text, so an unusual filing is still searchable.
    """
    if form not in SECTION_CONFIG:
        return []

    lines = text.splitlines()
    part: str | None = None
    # Every item heading, including ones we will not embed. A kept section has
    # to stop at the next item (Item 4, Item 1B, …), not run until the next
    # item that happens to be in SECTION_CONFIG.
    marks: list[tuple[int, str | None, str, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        part_match = _PART_HEADING.match(stripped)
        if part_match and len(stripped) < 40:
            part = part_match.group(1).upper()
            continue
        found = _heading(stripped)
        if found is None:
            continue
        item, title = found
        marks.append((index, part, item, title))

    found_sections: list[FilingSection] = []
    for position, (start, part_name, item, title) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        canonical = _canonical_title(form, part_name, item)
        if canonical is None:
            continue
        body = "\n".join(lines[start + 1:end]).strip()
        if len(body) < _MIN_BODY_CHARS:
            continue
        found_sections.append(FilingSection(form, item, canonical, None, body, part_name))

    # Same item in the contents and again in the body: keep the longer body.
    best: dict[tuple[str, str], FilingSection] = {}
    order: list[tuple[str, str]] = []
    for section in found_sections:
        key = (section.item, section.title)
        previous = best.get(key)
        if previous is None:
            order.append(key)
            best[key] = section
        elif len(section.text) > len(previous.text):
            best[key] = section

    pieces: list[FilingSection] = []
    for key in order:
        section = best[key]
        if (section.form, section.item) in _MDNA_ITEMS:
            pieces.extend(_split_labeled(section, _subsection_name))
        elif (section.form, section.item) in _NOTE_ITEMS:
            pieces.extend(_split_labeled(section, _note_name))
        else:
            pieces.append(section)
    return pieces
