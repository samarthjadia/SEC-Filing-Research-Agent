#!/usr/bin/env python3
"""
HTML → plain-text converter for 10-K, 10-Q, and earnings-report HTML.

Optional offline helper. The running app does not use it: utils/edgar.py
downloads filings and utils/html_text.py converts them. This script does not
extract filing sections. For each HTML file it:

  1. Drops scripts, styles, and other non-document chrome
  2. Strips remaining HTML tags
  3. Normalizes whitespace
  4. Writes a .txt file next to the same security name

Layout
------
    data/raw/<TICKER>/*.html              # input (local only; data/ is gitignored)
    data/processedToTxt/<TICKER>/*.txt    # output (created by this script)

The ticker / security name is the parent folder of each HTML file
(e.g. data/raw/NVDA/nvda-20260125-10k.html → data/processedToTxt/NVDA/).

Setup (once)
------------
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Run (from the project root, or from anywhere — paths are resolved
relative to this file):

    # Convert every HTML file under data/raw/
    python parsers/htmlToTxt.py

    # Convert one security only
    python parsers/htmlToTxt.py --ticker AMD

    # Convert a single file (ticker still taken from its parent folder)
    python parsers/htmlToTxt.py --file data/raw/APLD/apld-20260531-10k.html

    # Override input / output roots
    python parsers/htmlToTxt.py --input data/raw --output data/processedToTxt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Project root is the parent of parsers/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "processedToTxt"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.html_text import decode_html, html_to_text  # noqa: E402


def load_html(path: Path) -> str:
    """Read HTML as text. Filings are usually UTF-8; fall back if not."""
    return decode_html(path.read_bytes())


def output_path_for(html_path: Path, input_root: Path, output_root: Path) -> Path:
    """
    Mirror data/raw/<TICKER>/<file>.html → data/processedToTxt/<TICKER>/<file>.txt

    The security name is the ticker folder under data/raw. If the file is
    outside --input (e.g. --file with an absolute path), use its parent name.
    """
    try:
        relative = html_path.relative_to(input_root)
        ticker = relative.parts[0] if relative.parts else html_path.parent.name
    except ValueError:
        ticker = html_path.parent.name
    return output_root / ticker / (html_path.stem + ".txt")


def iter_html_files(input_root: Path, ticker: str | None) -> list[Path]:
    """HTML files under data/raw, optionally limited to one ticker folder."""
    if ticker:
        folder = input_root / ticker
        if not folder.is_dir():
            raise FileNotFoundError(f"No raw folder for ticker {ticker!r}: {folder}")
        roots = [folder]
    else:
        roots = [p for p in sorted(input_root.iterdir()) if p.is_dir()]

    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.glob("*.html")))
        files.extend(sorted(root.glob("*.htm")))
    return files


def convert_file(html_path: Path, output_path: Path) -> int:
    """Parse one HTML file and write UTF-8 text. Returns character count."""
    html = load_html(html_path)
    text = html_to_text(html)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return len(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert raw 10-K / 10-Q / earnings HTML into searchable .txt files."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Root folder of raw HTML (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Root folder for .txt output (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="Only convert this security folder (e.g. AMD, NVDA, APLD).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Convert a single HTML file instead of walking --input.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_root = args.input.resolve()
    output_root = args.output.resolve()

    if args.file:
        html_files = [args.file.resolve()]
        if not html_files[0].is_file():
            print(f"File not found: {html_files[0]}", file=sys.stderr)
            return 1
    else:
        if not input_root.is_dir():
            print(f"Input folder not found: {input_root}", file=sys.stderr)
            return 1
        html_files = iter_html_files(input_root, args.ticker.upper() if args.ticker else None)

    if not html_files:
        print("No HTML files found.")
        return 0

    print(f"Converting {len(html_files)} file(s) → {output_root}")
    for html_path in html_files:
        out_path = output_path_for(html_path, input_root, output_root)
        n_chars = convert_file(html_path, out_path)
        rel_out = out_path.relative_to(PROJECT_ROOT) if out_path.is_relative_to(PROJECT_ROOT) else out_path
        print(f"  {html_path.name:40s} → {rel_out}  ({n_chars:,} chars)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
