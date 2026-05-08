#!/usr/bin/env python3
"""Generate a markdown shopping list per the shopping-list skill spec.

Pipeline:
  1. Resolve recipes from a .menu file (Mode B) or from --recipes args (Mode A).
     For Mode B, the .menu file uses the modern CookLang format
     (==Day== blocks with @./path/Recipe{N%servings} references). Per-recipe
     scaling is computed as plan_servings / recipe_frontmatter_servings.
  2. Run `cook shopping-list --format markdown --pretty` on them.
  3. Post-process the output:
     - bump aisle headers from `# foo` to `## foo`
     - turn ingredient bullets `- *qty* name` into checkboxes `- [ ] *qty* name`
     - drop stray malformed empty-quantity bullets (e.g. `- ** black`)
     - drop any aisle section that ends up with no items
  4. Prepend `# <title>` and a `## Meals` section. In Mode B, each meal is
     prefixed with the `==block==` heading from the .menu file.
  5. Optionally insert a `## Notes` block (between title and Meals) from
     --notes.
  6. Write to --output (creating parent folders) or stdout.

Output shape matches the iOS Cookbook app's parser exactly:
  # <title>
  ## Notes        (optional)
  ## Meals
  ## <aisle>      (one per non-empty aisle)

Usage:
  build-list.py --menu plans/week-2026-05-03.menu \\
      --output "shopping-lists/shopping list 03.05.2026.md"

  build-list.py --recipes "Pasta/Pasta Bolognese.cook" "Meat/Beef Tacos.cook" \\
      --output "shopping-lists/shopping list 02.05.2026.md"

  # Scaling and globs are passed through to cook CLI in Mode A:
  build-list.py --recipes "Pasta/Pasta Bolognese.cook:2" "Sides/*.cook"

  # Add a Notes block (surfaces as the iOS app's editable Notes section):
  build-list.py --menu plans/week-2026-05-03.menu \\
      --notes "Ask the butcher to slice the pancetta thinly." \\
      --output "shopping-lists/shopping list 03.05.2026.md"
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

STRAY_BULLET = re.compile(r"^- \*\* \S+\s*$")

# Matches a CookLang menu day/block heading: `==Day 0==` or `== Saturday dinner ==`.
BLOCK_HEADING = re.compile(r"^==\s*(.+?)\s*==\s*$")

# Matches a recipe reference: `@./Pasta/Recipe Name{4%servings}` or `{1%batch}`.
# Captures: 1=relative path (no leading ./, no .cook extension), 2=quantity number, 3=unit
RECIPE_REF = re.compile(r"^@\./(.+?)\{(\d+(?:\.\d+)?)%(\w+)\}\s*$")

# YAML frontmatter delimiter (any line that is just `---`).
YAML_FENCE = re.compile(r"^---\s*$")


def parse_menu(menu_path: pathlib.Path) -> list[tuple[str | None, str, float | None, str]]:
    """Parse a CookLang .menu file.

    Returns a list of (block_header, recipe_path, plan_servings, unit) tuples,
    where:
      - block_header is the text inside the most recent `==...==` block (or None).
      - recipe_path is the relative path WITHOUT a leading `./` and WITHOUT
        a `.cook` extension (e.g. "Pasta/Pasta Bolognese").
      - plan_servings is the numeric quantity from `{N%unit}` (or None if the
        line didn't have a parseable quantity).
      - unit is the unit string from `{N%unit}` (typically "servings"; may be
        "batch" or other for staples).

    YAML frontmatter (between `---` fences at the top), blank lines,
    `Dinner:`/`Lunch:` meal-category subsection markers, and `--`-prefixed
    comment lines are all ignored. Lines that do not match a recipe-reference
    or a block heading are silently skipped.
    """
    pairs: list[tuple[str | None, str, float | None, str]] = []
    current_block: str | None = None
    in_frontmatter = False
    seen_first_fence = False

    for raw in menu_path.read_text().splitlines():
        line = raw.strip()
        # Handle YAML frontmatter at file top.
        if YAML_FENCE.match(line):
            if not seen_first_fence:
                in_frontmatter = True
                seen_first_fence = True
                continue
            elif in_frontmatter:
                in_frontmatter = False
                continue
        if in_frontmatter:
            continue
        if not line:
            continue
        # Block heading: ==Day 0== / == Saturday dinner ==
        m = BLOCK_HEADING.match(line)
        if m:
            current_block = m.group(1).strip() or None
            continue
        # Recipe reference.
        m = RECIPE_REF.match(line)
        if m:
            path = m.group(1)
            qty = float(m.group(2))
            unit = m.group(3)
            pairs.append((current_block, path, qty, unit))
            continue
        # Everything else (comment lines starting with `--`, meal-category
        # subsection markers like `Dinner:`, free text) is intentionally
        # discarded: it has no shopping-list semantics.
    return pairs


def read_recipe_servings(recipe_path: pathlib.Path) -> float | None:
    """Extract the `servings:` value from a .cook file's YAML frontmatter.

    Returns None if the file has no frontmatter or no `servings:` key, or if
    the value is non-numeric.
    """
    try:
        text = recipe_path.read_text()
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or not YAML_FENCE.match(lines[0].strip()):
        return None
    for ln in lines[1:]:
        if YAML_FENCE.match(ln.strip()):
            return None
        m = re.match(r"^\s*servings\s*:\s*(\d+(?:\.\d+)?)\s*$", ln)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def menu_pair_to_recipe_arg(path: str, plan_qty: float | None, unit: str) -> tuple[str, float | None]:
    """Convert a (path, plan_qty, unit) triple to a Cook CLI shopping-list arg.

    The `path` from the .menu has no `.cook` extension and no `./` prefix; the
    Cook CLI wants the file path with `.cook` and (optionally) `:N` scale.

    For unit == "servings", the scale factor is plan_qty / recipe_frontmatter_servings.
    For other units (e.g. "batch"), no scaling is applied — pass the recipe
    as-is and trust its frontmatter defaults.

    Cook CLI's `:N` syntax is integer-only. Non-integer ratios (e.g. plan
    {2%servings} against a 4-serving recipe = 0.5×) are rounded to the
    nearest integer with a floor of 1, and a warning is emitted to stderr
    naming the affected recipe and the actual vs. requested quantities.
    For full precision the upstream .menu file should specify integer
    multiples of the recipe's documented servings.

    Returns (cook_cli_arg, scale_factor) where scale_factor is None for
    no-scaling cases.
    """
    cook_path = path + ".cook"
    if unit != "servings" or plan_qty is None:
        return cook_path, None
    recipe_servings = read_recipe_servings(pathlib.Path(cook_path))
    if not recipe_servings or recipe_servings <= 0:
        # No frontmatter servings; can't compute scale, pass through unscaled.
        return cook_path, None
    scale = plan_qty / recipe_servings
    if abs(scale - 1.0) < 1e-6:
        return cook_path, 1.0
    # Cook CLI's :N syntax wants integer scale; round to nearest int with a
    # sanity floor of 1 to avoid `:0`.
    int_scale = max(1, round(scale))
    if abs(scale - int_scale) > 1e-3:
        actual_qty = int_scale * recipe_servings
        print(
            f"warning: {path} (recipe servings={recipe_servings:g}, "
            f"plan asked for {plan_qty:g}, ideal scale {scale:.3f}×): "
            f"Cook CLI :N is integer-only, rounded to :{int_scale} "
            f"({actual_qty:g} servings worth of ingredients will land in "
            f"the shopping list).",
            file=sys.stderr,
        )
    return f"{cook_path}:{int_scale}", float(int_scale)


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


def render_meals_from_menu(pairs: list[tuple[str | None, str, float | None, str]]) -> list[str]:
    """Render the Meals section from .menu pairs.

    Each meal: `- **<block>** — <Recipe Name> (×N)` where N is the scale
    factor relative to the recipe's frontmatter servings (omitted when 1×).
    """
    lines = ["## Meals"]
    for block, path, plan_qty, unit in pairs:
        name = pathlib.Path(path).name
        scale_suffix = ""
        if unit == "servings" and plan_qty is not None:
            recipe_servings = read_recipe_servings(pathlib.Path(path + ".cook"))
            if recipe_servings and recipe_servings > 0:
                scale = plan_qty / recipe_servings
                if abs(scale - 1.0) >= 1e-6:
                    int_scale = max(1, round(scale))
                    scale_suffix = f" (×{int_scale})"
        prefix = f"**{block}** — " if block else ""
        lines.append(f"- {prefix}{name}{scale_suffix}")
    return lines


def render_meals_from_recipes(recipes: list[str]) -> list[str]:
    """Render the Meals section from --recipes args (Mode A).

    Each entry: `- <Recipe Name>` with `(×N)` appended when the cook-CLI
    `:N` scale suffix is present.
    """
    lines = ["## Meals"]
    for spec in recipes:
        if ":" in spec and not spec.endswith(":"):
            path_str, scale = spec.rsplit(":", 1)
            scale_suffix = f" (×{scale})" if scale.isdigit() else ""
        else:
            path_str, scale_suffix = spec, ""
        name = pathlib.Path(path_str).stem
        lines.append(f"- {name}{scale_suffix}")
    return lines


def assemble(title: str, notes: str | None, meals: list[str], body: list[str]) -> str:
    while body and not body[0].strip():
        body.pop(0)
    parts: list[str] = [f"# {title}", ""]
    if notes and notes.strip():
        parts += ["## Notes", notes.strip(), ""]
    parts += [*meals, "", *body]
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate a markdown shopping list per the shopping-list skill spec.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--menu", type=pathlib.Path, help="Path to a CookLang .menu file (Mode B)")
    src.add_argument(
        "--recipes",
        nargs="+",
        help="Recipe paths (Mode A); supports cook CLI :N scaling and globs",
    )
    p.add_argument(
        "--title",
        help="Title rendered as `# <title>`. Default: derived from --output stem, or 'shopping list'.",
    )
    p.add_argument(
        "--notes",
        help="Insert a `## Notes` block between the title and Meals (free text, surfaces as the iOS app's editable Notes section).",
    )
    p.add_argument(
        "--output",
        type=pathlib.Path,
        help="Write to this path (parent folders auto-created). Default: stdout.",
    )
    args = p.parse_args()

    if args.menu:
        pairs = parse_menu(args.menu)
        if not pairs:
            print(f"No recipe references found in {args.menu}.", file=sys.stderr)
            return 1
        recipes = []
        for _, path, plan_qty, unit in pairs:
            cook_arg, _scale = menu_pair_to_recipe_arg(path, plan_qty, unit)
            recipes.append(cook_arg)
        meals = render_meals_from_menu(pairs)
    else:
        recipes = list(args.recipes)
        if not recipes:
            print("No recipes resolved from input.", file=sys.stderr)
            return 1
        meals = render_meals_from_recipes(args.recipes)

    cook_out = run_cook(recipes)
    body = post_process(cook_out)

    if args.title:
        title = args.title
    elif args.output:
        title = args.output.stem
    else:
        title = "shopping list"

    text = assemble(title, args.notes, meals, body)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
