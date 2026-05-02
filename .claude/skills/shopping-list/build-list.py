#!/usr/bin/env python3
"""Generate a markdown shopping list per the shopping-list skill spec.

Pipeline:
  1. Resolve recipes from a .menu file (Mode B) or from --recipes args (Mode A).
  2. Run `cook shopping-list --format markdown --pretty` on them.
  3. Post-process the output:
     - bump aisle headers from `# foo` to `## foo`
     - turn ingredient bullets `- *qty* name` into checkboxes `- [ ] *qty* name`
     - drop stray malformed empty-quantity bullets (e.g. `- ** black`)
     - drop any aisle section that ends up with no items
  4. Prepend `# <title>` and a `## Meals` section. In Mode B, each meal is
     prefixed with the day/slot taken from the comment header that precedes
     the recipe in the .menu file.
  5. Write to --output (creating parent folders) or stdout.

Usage:
  build-list.py --menu plans/week-2026-05-03.menu \\
      --output "shopping-lists/shopping list 03.05.2026.md"

  build-list.py --recipes "Pasta/Pasta Bolognese.cook" "Meat/Beef Tacos.cook" \\
      --output "shopping-lists/shopping list 02.05.2026.md"

  # Scaling and globs are passed through to cook CLI:
  build-list.py --recipes "Pasta/Pasta Bolognese.cook:2" "Sides/*.cook"
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

DAY_HEADER_SPLIT = re.compile(r"\s+[–\-(:]")
STRAY_BULLET = re.compile(r"^- \*\* \S+\s*$")


def parse_menu(menu_path: pathlib.Path) -> list[tuple[str | None, str]]:
    """Return (day_header, recipe_spec) pairs from a .menu file.

    Lines starting with `#` set the current header; subsequent non-blank,
    non-comment lines inherit it. The header text is trimmed at the first
    separator (`–`, `-`, `(`, `:`) so a comment like
    `# Saturday dinner – kids: makaroni` becomes `Saturday dinner`.
    """
    pairs: list[tuple[str | None, str]] = []
    current_header: str | None = None
    for raw in menu_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            text = line.lstrip("# ").strip()
            text = DAY_HEADER_SPLIT.split(text, maxsplit=1)[0].strip()
            current_header = text or None
            continue
        pairs.append((current_header, line))
    return pairs


def run_cook(recipes: list[str]) -> str:
    result = subprocess.run(
        ["cook", "shopping-list", "--format", "markdown", "--pretty", *recipes],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def post_process(cook_output: str) -> list[str]:
    out: list[str] = []
    for ln in cook_output.splitlines():
        if STRAY_BULLET.match(ln):
            continue
        if ln.startswith("- "):
            out.append("- [ ] " + ln[2:])
        elif ln.startswith("# "):
            out.append("## " + ln[2:])
        else:
            out.append(ln)
    return drop_empty_sections(out)


def drop_empty_sections(lines: list[str]) -> list[str]:
    """Drop any `## ...` header with no ingredient bullets before the next `##` or EOF."""
    kept: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("## "):
            j = i + 1
            has_items = False
            while j < len(lines) and not lines[j].startswith("## "):
                if lines[j].startswith("- "):
                    has_items = True
                    break
                j += 1
            if has_items:
                kept.append(ln)
                i += 1
            else:
                while j < len(lines) and not lines[j].startswith("## "):
                    j += 1
                i = j
        else:
            kept.append(ln)
            i += 1
    return kept


def render_meals(pairs: list[tuple[str | None, str]]) -> list[str]:
    """Render the Meals section. Includes day prefix when present, and a
    scaling annotation `(×N)` when the recipe spec uses cook CLI's `:N` syntax.
    """
    lines = ["## Meals"]
    for header, spec in pairs:
        if ":" in spec and not spec.endswith(":"):
            path_str, scale = spec.rsplit(":", 1)
            scale_suffix = f" (×{scale})" if scale.isdigit() else ""
        else:
            path_str, scale_suffix = spec, ""
        name = pathlib.Path(path_str).stem
        prefix = f"**{header}** — " if header else ""
        lines.append(f"- {prefix}{name}{scale_suffix}")
    return lines


def assemble(title: str, meals: list[str], body: list[str]) -> str:
    while body and not body[0].strip():
        body.pop(0)
    parts = [f"# {title}", "", *meals, "", *body]
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate a markdown shopping list per the shopping-list skill spec.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--menu", type=pathlib.Path, help="Path to a .menu file (Mode B)")
    src.add_argument(
        "--recipes",
        nargs="+",
        help="Recipe paths (Mode A); supports cook CLI :N scaling and globs",
    )
    p.add_argument(
        "--title",
        help="Title shown as `# <title>`. Default: derived from --output stem, or 'Shopping list'.",
    )
    p.add_argument(
        "--output",
        type=pathlib.Path,
        help="Write to this path (parent folders auto-created). Default: stdout.",
    )
    args = p.parse_args()

    if args.menu:
        pairs = parse_menu(args.menu)
        recipes = [r for _, r in pairs]
    else:
        pairs = [(None, r) for r in args.recipes]
        recipes = list(args.recipes)

    if not recipes:
        print("No recipes resolved from input.", file=sys.stderr)
        return 1

    cook_out = run_cook(recipes)
    body = post_process(cook_out)
    meals = render_meals(pairs)

    if args.title:
        title = args.title
    elif args.output:
        title = f"Shopping list: {args.output.stem}"
    else:
        title = "Shopping list"

    text = assemble(title, meals, body)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
