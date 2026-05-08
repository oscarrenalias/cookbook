#!/usr/bin/env python3
"""Audit .cook frontmatter against the cookbook's mandatory keys.

Validates that every .cook file in the search path has the keys mandated by
the cookbook's CLAUDE.md. With --strict, also enforces controlled-vocabulary
values for difficulty/course/protein/cuisine and a numeric servings field.

Reports issues to stdout (one per line, `<path>: <issue>`), and exits
non-zero if any issues are found. Designed to be CI-friendly and free of
external dependencies (standard library only).

Usage:
  audit-frontmatter.py                                   # walk current directory
  audit-frontmatter.py --root ~/Projects/cookbook        # walk a specific tree
  audit-frontmatter.py "Meat/Roasted Pork Fillet.cook"   # one or more specific files
  audit-frontmatter.py --strict                          # also enforce vocabulary
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

REQUIRED_KEYS = [
    "title",
    "servings",
    "prep time",
    "cook time",
    "difficulty",
    "cuisine",
    "protein",
    "course",
    "tags",
    "equipment",
]

DIFFICULTY_VOCAB = {"easy", "medium", "involved"}
COURSE_VOCAB = {"breakfast", "lunch", "dinner", "snack", "side", "dessert"}
PROTEIN_VOCAB = {
    "chicken", "beef", "pork", "fish", "salmon", "tuna", "shrimp",
    "egg", "tofu", "beans", "lentils", "cheese", "mixed", "none",
}
CUISINE_VOCAB = {
    "finnish", "italian", "japanese", "japanese-inspired", "chinese",
    "thai", "indian", "mexican", "spanish", "middle-eastern", "american",
    "generic", "fusion",
}
TAG_VOCAB = {
    "weekday", "weekend", "kid-friendly", "batch-cook", "freezer-friendly",
    "quick", "vegetarian", "pescatarian", "vegan", "spicy", "mild",
    "comfort-food", "one-pot", "oven", "grill", "stir-fry", "leftovers",
    "rice-dish", "pasta-dish", "soup", "salad", "no-cook", "fresh",
    "needs-review", "staples",
}

YAML_FENCE = re.compile(r"^---\s*$")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Return YAML frontmatter as {key: raw_value_string}.

    Handles top-level scalar keys and two list shapes:
      - inline:   `key: [a, b, c]`
      - block:    `key:` followed by indented `  - value` lines
    For list values, the captured value is the comma-joined list contents
    (a sentinel that the audit treats as "key present, value is a list").

    Returns None if the file has no frontmatter or the frontmatter is
    unterminated.
    """
    lines = text.splitlines()
    if not lines or not YAML_FENCE.match(lines[0].strip()):
        return None
    out: dict[str, str] = {}
    in_list = False
    list_key = ""
    list_items: list[str] = []
    for ln in lines[1:]:
        if YAML_FENCE.match(ln.strip()):
            if in_list:
                out[list_key] = ", ".join(list_items)
            return out
        # List item line (indented `- value`).
        m = re.match(r"^\s+-\s+(.*?)\s*$", ln)
        if m and in_list:
            list_items.append(m.group(1))
            continue
        # Inline list `key: [a, b, c]`.
        m = re.match(r"^([a-zA-Z][\w ]*?)\s*:\s*\[(.*)\]\s*$", ln)
        if m:
            if in_list:
                out[list_key] = ", ".join(list_items)
                in_list = False
                list_items = []
            out[m.group(1)] = m.group(2).strip()
            continue
        # Block list `key:` (no value on the line).
        m = re.match(r"^([a-zA-Z][\w ]*?)\s*:\s*$", ln)
        if m:
            if in_list:
                out[list_key] = ", ".join(list_items)
            in_list = True
            list_key = m.group(1)
            list_items = []
            continue
        # Scalar `key: value`.
        m = re.match(r"^([a-zA-Z][\w ]*?)\s*:\s*(.*?)\s*$", ln)
        if m:
            if in_list:
                out[list_key] = ", ".join(list_items)
                in_list = False
                list_items = []
            out[m.group(1)] = m.group(2)
            continue
    # Frontmatter never terminated.
    return None


def audit_file(path: pathlib.Path, strict: bool) -> list[str]:
    issues: list[str] = []
    try:
        text = path.read_text()
    except OSError as e:
        return [f"{path}: cannot read ({e})"]
    fm = parse_frontmatter(text)
    if fm is None:
        return [f"{path}: missing or unterminated YAML frontmatter"]
    for key in REQUIRED_KEYS:
        if key not in fm:
            issues.append(f"{path}: missing key `{key}`")
    if strict:
        diff = fm.get("difficulty", "").strip().lower()
        if diff and diff not in DIFFICULTY_VOCAB:
            issues.append(
                f"{path}: difficulty {fm['difficulty']!r} not in vocabulary "
                f"{sorted(DIFFICULTY_VOCAB)}"
            )
        course = fm.get("course", "").strip().lower()
        if course and course not in COURSE_VOCAB:
            issues.append(
                f"{path}: course {fm['course']!r} not in vocabulary "
                f"{sorted(COURSE_VOCAB)}"
            )
        protein = fm.get("protein", "").strip().lower()
        if protein and protein not in PROTEIN_VOCAB:
            issues.append(
                f"{path}: protein {fm['protein']!r} not in vocabulary "
                f"{sorted(PROTEIN_VOCAB)}"
            )
        cuisine = fm.get("cuisine", "").strip().lower()
        if cuisine and cuisine not in CUISINE_VOCAB:
            issues.append(
                f"{path}: cuisine {fm['cuisine']!r} not in vocabulary "
                f"{sorted(CUISINE_VOCAB)}"
            )
        servings = fm.get("servings", "")
        if servings and not re.match(r"^\d+(?:\.\d+)?$", servings.strip()):
            issues.append(f"{path}: servings {fm['servings']!r} is not numeric")
        # tags vocabulary (only when tags is present — missing-key already
        # reported above by the required-keys check)
        tags_raw = fm.get("tags", "")
        if tags_raw:
            tag_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
            unknown = sorted(set(tag_list) - TAG_VOCAB)
            if unknown:
                issues.append(
                    f"{path}: unknown tags {unknown} not in vocabulary "
                    f"(see CLAUDE.md § Tag vocabulary)"
                )
        # filename ↔ title invariant: the .cook filename's stem (without
        # extension) must equal the frontmatter `title:` value exactly.
        title = fm.get("title", "").strip()
        stem = path.stem
        if title and title != stem:
            issues.append(
                f"{path}: filename stem {stem!r} does not match title "
                f"{title!r} (CLAUDE.md mandates filename↔title sync)"
            )
    return issues


def collect_files(targets: list[str], root: pathlib.Path) -> list[pathlib.Path]:
    if targets:
        return [pathlib.Path(t) for t in targets]
    return sorted(root.glob("**/*.cook"))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Audit .cook frontmatter against the cookbook's mandatory keys.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "files",
        nargs="*",
        help="Specific .cook files to audit. Default: walk --root for all .cook files.",
    )
    p.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path.cwd(),
        help="Cookbook root for collection-wide audits (default: cwd).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Also enforce controlled-vocabulary values for difficulty / course / "
             "protein / cuisine / tags, a numeric servings field, and the "
             "filename↔title invariant.",
    )
    args = p.parse_args()
    files = collect_files(args.files, args.root)
    if not files:
        print(f"warning: no .cook files found under {args.root}", file=sys.stderr)
        return 0
    all_issues: list[str] = []
    for f in files:
        all_issues.extend(audit_file(f, args.strict))
    for issue in all_issues:
        print(issue)
    if all_issues:
        print(
            f"\n{len(all_issues)} issue(s) across {len(files)} file(s).",
            file=sys.stderr,
        )
        return 1
    print(f"all {len(files)} file(s) pass.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
