---
name: shopping-list
description: Generate aisle-grouped shopping lists with markdown checkboxes from one or more Cooklang recipes or a meal plan, saved to shopping-lists/
---

# Shopping List

## Overview

Generate a markdown shopping list from CookLang recipes or a meal plan and save it under `shopping-lists/` at the repo root. The output is suitable for importing into the iOS / macOS Notes app: aisle-grouped by `config/aisle.conf`, with markdown checkboxes for each item, and a `## Meals` section at the top listing the recipes that contributed to the list.

Use this skill when the user asks for a weekly shop, a one-off list, or to materialize a meal plan into a grocery list.

## Process

### Step 1: Decide the input source

Two supported modes:

**Mode A — explicit recipes.** The user names recipes directly (e.g. "Pasta Bolognese, Hummus, Beef Tacos"). Resolve each one to its `.cook` path under the repo's category folders (`Meat/`, `Pasta/`, `Soup/`, etc.) — use `find . -iname '<name>.cook'` if needed. Pass each path to `--recipes`. Cook CLI's `:N` scaling syntax and globs are passed through.

**Mode B — meal plan.** The user points to a `.menu` file under `plans/`. Pass the file to `--menu` and the script handles parsing (skipping blank lines and `#` comments) — no manual extraction needed. Comment headers immediately above a recipe become the day/slot prefix in the `## Meals` section (e.g. `# Saturday dinner – kids: makaroni` → `**Saturday dinner** — Recipe Name`).

### Step 2: Confirm the output filename

Always ask the user for the filename, but propose this default:

```
shopping list <dd>.<mm>.<yyyy>.md
```

…using today's date in `dd.mm.yyyy` format (European). Use `AskUserQuestion` with two options: the proposed default and "Other" so the user can supply a custom name.

The final path is `shopping-lists/<chosen name>` (the script creates the folder if missing).

### Step 3: Generate the list with `build-list.py`

The skill ships a Python script — `build-list.py`, sitting alongside this SKILL.md in the skill folder — that performs the entire generate-and-post-process pipeline deterministically. Always invoke this script rather than reimplementing the steps inline. In the examples below, `<skill>/build-list.py` stands for the script's actual path; resolve it relative to wherever the skill is installed.

```bash
# Mode B (meal plan)
<skill>/build-list.py \
  --menu "plans/<menu-file>.menu" \
  --output "shopping-lists/<chosen name>"

# Mode A (explicit recipes; supports cook CLI :N scaling and globs)
<skill>/build-list.py \
  --recipes "Pasta/Pasta Bolognese.cook" "Meat/Beef Tacos.cook" "Sides/Hummus.cook" \
  --output "shopping-lists/<chosen name>"
```

Run `<skill>/build-list.py --help` for the full flag set. Useful options:
- `--title "..."` to override the auto-derived `# Shopping list: <stem>` heading.
- Omit `--output` to print to stdout (useful for piping or previewing).

What the script does, end to end:
1. Resolves recipes (parses the `.menu` file or takes them from `--recipes`).
2. Calls `cook shopping-list --format markdown --pretty` on the resolved recipes.
3. Bumps aisle headers from `# foo` to `## foo`.
4. Converts ingredient bullets `- *qty* name` into checkboxes `- [ ] *qty* name`.
5. Drops stray malformed empty-quantity bullets (e.g. `- ** black`) and any aisle section that ends up with no items.
6. Prepends `# <title>` and a `## Meals` section. In Mode B each meal is prefixed with its day/slot from the menu's comment headers; `:N` scaling is shown as `(×N)`.

This keeps the file's heading hierarchy consistent: `#` (file title) → `##` (`Meals` and aisle sections).

### Step 4: One-off items and confirmation

After the script runs, edit the saved file directly to insert one-off items the user mentioned that aren't in any recipe (fruits, batteries, frying oil, etc.) — see the Tips section below. Then report the saved path and the first ~20 lines so the user can spot-check.

## Defaults and constraints

- **Output folder**: always `shopping-lists/` at the repo root. Never write the list elsewhere.
- **Date format**: `dd.mm.yyyy` (European), e.g. `02.05.2026`.
- **Aisle grouping**: always on — never use `--plain` for this skill.
- **Format**: always markdown (`--format markdown --pretty`). No JSON / YAML / human variants.
- **Checkboxes**: only on ingredient bullets (`- *qty* name` → `- [ ] *qty* name`). Meal-list bullets stay plain.
- **Heading depth**: aisle headers from Cook CLI are bumped from `#` to `##` so the file's hierarchy is `#` (file title) → `##` (`Meals` and aisle sections).
- **Meal list**: always present, even if there is only one recipe.
- **Cook CLI warnings**: the script captures stdout only, so Cook CLI's stderr warnings don't pollute the markdown.

## Examples

**User:** "Generate a shopping list for Pasta Bolognese, Beef Tacos, and Hummus."

1. Resolve paths: `Pasta/Pasta Bolognese.cook`, `Meat/Beef Tacos.cook`, `Sides/Hummus.cook`.
2. Propose filename `shopping list 02.05.2026.md`. User accepts.
3. Run Cook CLI, post-process, save to `shopping-lists/shopping list 02.05.2026.md`.
4. File looks like:

```markdown
# Shopping list: shopping list 02.05.2026

## Meals
- Pasta Bolognese
- Beef Tacos
- Hummus

## produce
- [ ] *2 piece* tomatoes
- [ ] *1 piece* onion
- [ ] *1 piece* garlic
…

## dairy
- [ ] *50 g* parmesan
…
```

**User:** "Make the shopping list for plans/week-2026-05-02.menu."

1. Mode B: pass the menu file to `build-list.py --menu`.
2. Propose filename `shopping list 02.05.2026.md` (or use the menu's start date if obvious from the filename).
3. Run:
   ```bash
   <skill>/build-list.py \
     --menu "plans/week-2026-05-02.menu" \
     --output "shopping-lists/shopping list 02.05.2026.md"
   ```
4. Each meal in the resulting `## Meals` section is prefixed with the day/slot taken from the menu's `# ...` comment headers.

## Tips

- If the user adds the `Pantry/Dairy Staples.cook` file (or similar staples placeholder) to the list, those items will appear under `## dairy` automatically — useful since the Cook iOS app can't add ad-hoc items.
- If a new ingredient lands under `## other` in the output, it's missing from `config/aisle.conf`. Suggest adding it (one line under the right `[section]`) so the next list groups it correctly.
- For scaling, append `:N` to a recipe path: `"Pasta/Pasta Bolognese.cook:2"` doubles it.
- The user may want to add one-off items not in any recipe (e.g. "AA batteries for the kitchen scale"). After the file is generated, edit it directly to insert these as checkbox bullets under the most appropriate aisle section — `## other` if nothing else fits. For example:

```markdown
# shopping-lists/shopping list 02.05.2026.md
## other
- [ ] *1 pack* AA batteries (for kitchen scale)
```