#!/usr/bin/env python3
"""Generate a canonical CookLang .menu file under plans/ from a JSON description.

Pipeline:
  1. Read a JSON plan from --input (a path, or '-' for stdin).
  2. Validate that every referenced recipe exists at <path>.cook relative
     to --repo (default: cwd).
  3. Validate that block names don't contain '==', and that recipe paths
     don't include a leading './' or trailing '.cook'.
  4. Read each referenced recipe's `servings:` frontmatter and warn (to
     stderr) when the plan-side servings yield a non-integer scale factor
     or when the recipe has no readable servings field.
  5. Emit the canonical CookLang .menu format the iOS Cookbook app's parser
     expects, to --output (or stdout).

The script never silently rewrites a malformed plan. Validation errors exit
non-zero; warnings are advisory.

JSON schema:
  {
    "servings": int,                       # optional, frontmatter informational
    "blocks": [
      {
        "name": str,                       # required, e.g. "Day 0" or "Saturday dinner"
        "meals": [
          {
            "category": str,               # optional, e.g. "Dinner"
            "comment": str,                # optional, becomes a `-- comment` line
            "recipes": [
              {
                "path": str,               # required, relative path WITHOUT '.cook' or leading './'
                "servings": int|float,     # required, plan-side quantity
                "unit": str                # optional, default "servings"
              }, ...
            ]
          }, ...
        ]
      }, ...
    ]
  }

Usage:
  build-menu.py --input plan.json --output plans/week-2026-05-08.menu
  cat plan.json | build-menu.py --input - --output plans/week-2026-05-08.menu
  build-menu.py --input plan.json --validate-only
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

YAML_FENCE = re.compile(r"^---\s*$")


def read_recipe_servings(recipe_path: pathlib.Path) -> float | None:
    """Extract the `servings:` value from a .cook file's YAML frontmatter."""
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


def validate_plan(plan: object, repo: pathlib.Path) -> list[str]:
    """Return a list of human-readable errors. Empty list = valid."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["Top-level value must be a JSON object."]
    blocks = plan.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("`blocks` must be a non-empty list.")
        return errors
    for bi, block in enumerate(blocks):
        if not isinstance(block, dict):
            errors.append(f"blocks[{bi}] must be an object.")
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"blocks[{bi}].name must be a non-empty string.")
        elif "==" in name:
            errors.append(
                f"blocks[{bi}].name {name!r} contains '==' which collides "
                f"with the block-marker syntax."
            )
        meals = block.get("meals")
        if not isinstance(meals, list) or not meals:
            errors.append(f"blocks[{bi}].meals must be a non-empty list.")
            continue
        for mi, meal in enumerate(meals):
            if not isinstance(meal, dict):
                errors.append(f"blocks[{bi}].meals[{mi}] must be an object.")
                continue
            recipes = meal.get("recipes")
            if not isinstance(recipes, list) or not recipes:
                errors.append(
                    f"blocks[{bi}].meals[{mi}].recipes must be a non-empty list."
                )
                continue
            for ri, recipe in enumerate(recipes):
                tag = f"blocks[{bi}].meals[{mi}].recipes[{ri}]"
                if not isinstance(recipe, dict):
                    errors.append(f"{tag} must be an object.")
                    continue
                path = recipe.get("path")
                if not isinstance(path, str) or not path:
                    errors.append(f"{tag}.path must be a non-empty string.")
                else:
                    if path.startswith("./"):
                        errors.append(
                            f"{tag}.path {path!r} must not start with './' "
                            f"(the script adds it)."
                        )
                    if path.endswith(".cook"):
                        errors.append(
                            f"{tag}.path {path!r} must not include the '.cook' "
                            f"extension (the script omits it)."
                        )
                    full = repo / (path + ".cook")
                    if not full.is_file():
                        errors.append(
                            f"{tag}.path {path!r}: file does not exist at {full}."
                        )
                servings = recipe.get("servings")
                if not isinstance(servings, (int, float)) or servings <= 0:
                    errors.append(f"{tag}.servings must be a positive number.")
                unit = recipe.get("unit", "servings")
                if not isinstance(unit, str) or not unit:
                    errors.append(f"{tag}.unit (if present) must be a non-empty string.")
    return errors


def warn_scale_mismatches(plan: dict, repo: pathlib.Path) -> None:
    for block in plan.get("blocks", []):
        for meal in block.get("meals", []):
            for recipe in meal.get("recipes", []):
                if recipe.get("unit", "servings") != "servings":
                    continue
                path = recipe.get("path")
                if not isinstance(path, str) or path.startswith("./") or path.endswith(".cook"):
                    continue
                full = repo / (path + ".cook")
                doc_servings = read_recipe_servings(full)
                if doc_servings is None:
                    print(
                        f"warning: {path}.cook has no `servings:` frontmatter; "
                        f"shopping-list scale factor cannot be computed downstream.",
                        file=sys.stderr,
                    )
                    continue
                plan_servings = recipe.get("servings")
                if not isinstance(plan_servings, (int, float)) or plan_servings <= 0:
                    continue
                ratio = plan_servings / doc_servings
                if abs(ratio - round(ratio)) > 1e-3:
                    print(
                        f"warning: {path} has {doc_servings} servings in "
                        f"frontmatter but plan calls for {plan_servings} "
                        f"(ratio {ratio:.3f}); non-integer scale factors are "
                        f"downcast by Cook CLI's :N flag.",
                        file=sys.stderr,
                    )


def render_menu(plan: dict) -> str:
    lines: list[str] = []
    fm_servings = plan.get("servings")
    if fm_servings is not None:
        lines += ["---", f"servings: {fm_servings}", "---", ""]
    for bi, block in enumerate(plan["blocks"]):
        if bi > 0:
            lines.append("")
        lines.append(f"=={block['name']}==")
        for mi, meal in enumerate(block["meals"]):
            if mi > 0:
                lines.append("")
            cat = meal.get("category")
            if cat:
                lines.append(f"{cat}:")
            comment = meal.get("comment")
            if comment:
                lines.append(f"-- {comment}")
            for recipe in meal["recipes"]:
                unit = recipe.get("unit", "servings")
                qty = recipe["servings"]
                if isinstance(qty, float) and qty.is_integer():
                    qty_str = f"{int(qty)}"
                else:
                    qty_str = f"{qty}"
                lines.append(f"@./{recipe['path']}{{{qty_str}%{unit}}}")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate a canonical CookLang .menu file from a JSON plan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--input",
        required=True,
        help="Path to a JSON plan, or '-' for stdin.",
    )
    p.add_argument(
        "--output",
        type=pathlib.Path,
        help="Write to this path (parent folders auto-created). Default: stdout.",
    )
    p.add_argument(
        "--repo",
        type=pathlib.Path,
        default=pathlib.Path.cwd(),
        help="Repo root for resolving recipe paths (default: cwd).",
    )
    p.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the input and exit; do not emit a .menu file.",
    )
    args = p.parse_args()

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        raw = pathlib.Path(args.input).read_text()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON input: {e}", file=sys.stderr)
        return 2

    errors = validate_plan(plan, args.repo)
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    warn_scale_mismatches(plan, args.repo)

    if args.validate_only:
        print("plan validated.", file=sys.stderr)
        return 0

    out = render_menu(plan)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
