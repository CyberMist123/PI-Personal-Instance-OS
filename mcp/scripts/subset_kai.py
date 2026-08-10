"""Subset a Kai typeface to everyday Chinese and write a woff2.

Used for both tiers of the voice transcript font:

* the open-source face that ships in this repository, and
* the licensed system face that must never leave this machine.

A full CJK face is ~26 MB, which is absurd for one line of transcript. GB2312
covers 6763 hanzi — effectively all modern speech — plus the Latin and
punctuation a transcript actually contains. The charset is enumerated from
Python's own gb2312 codec, so this needs no external word list. Anything rarer
falls back to the next family in the stack.

    py -3 subset_kai.py <source.ttf|otf> <target.woff2>

Requires: pip install fonttools brotli
"""

from __future__ import annotations

import sys
from pathlib import Path


def gb2312_chars() -> set[str]:
    chars: set[str] = set()
    for lead in range(0xA1, 0xF8):
        for trail in range(0xA1, 0xFF):
            try:
                chars.add(bytes([lead, trail]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return chars


def build_charset() -> str:
    keep = gb2312_chars()
    keep |= {chr(c) for c in range(0x20, 0x7F)}              # Latin, digits, ASCII marks
    keep |= set("。，、；：？！“”‘’（）《》〈〉【】—…·　～－")      # CJK punctuation
    keep |= {chr(c) for c in range(0x2000, 0x206F)}          # general punctuation
    return "".join(sorted(keep))


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    from fontTools import subset
    from fontTools.ttLib import TTFont

    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    if not source.is_file():
        print(f"no such font: {source}")
        return 1

    text = build_charset()
    print(f"charset: {len(text)} glyphs requested")

    font = TTFont(source)
    options = subset.Options()
    options.flavor = "woff2"
    options.desubroutinize = True
    options.drop_tables += ["FFTM"]
    options.layout_features = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = True

    subsetter = subset.Subsetter(options=options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    font.flavor = "woff2"
    target.parent.mkdir(parents=True, exist_ok=True)
    font.save(target)

    print(f"source : {source.stat().st_size / 1e6:6.1f} MB  {source.name}")
    print(f"woff2  : {target.stat().st_size / 1e6:6.2f} MB  {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
