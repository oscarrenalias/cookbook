---
name: shopping-list
description: Generate aisle-grouped shopping lists with markdown checkboxes from one or more Cooklang recipes or a meal plan, saved to shopping-lists/ in a format the iOS Cookbook app loads
---

# Shopping List

## Overview

Generate a markdown shopping list from CookLang recipes or a meal plan and save it under `shopping-lists/` at the repo root. The output is consumed by both the **Cook CLI** workflow and the **iOS Cookbook app's Shopping Lists tab**. The iOS app's parser is the stricter of the two — see *Format reference* below.

Use this skill when the user asks for a weekly shop, a one-off list, or to materialize a meal plan into a grocery list.

## Process

### Step 1: Decide the input source

Two supported modes:

**Mode A — explicit recipes.** The user names recipes directly (e.g. "Pasta Bolognese, Hummus, Beef Tacos"). Resolve each one to its `.cook` path under the repo's category folders (`Meat/`, `Pasta/`, `Soup/`, etc.) — use `find . -iname '<name>.cook'` if needed. Pass each path to `--recipes`. Cook CLI's `:N` scaling syntax and globs are passed through.

**Mode B — meal plan.** The user points to a `.menu` file under `plans/`. Pass the file to `--menu`. The script parses the modern CookLang `.menu` format — YAML frontmatter, `==Day==` blocks, `@./relative-path/Recipe Name{N%servings}` references, indented `Dinner:` / `Lunch:` meal-category subsections, and `--` comment lines. Each recipe reference inherits its enclosing `==block==` heading as the day/slot prefix in the `## Meals` section (e.g. `==Saturday dinner==` → `**Saturday dinner** — Recipe Name`). Per-recipe scaling is computed automatically as `plan-servings / recipe-frontmatter-servings`.

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
- `--title "..."` to override the auto-derived `# <stem>` heading. The H1 becomes the in-app list title in the iOS app, so the filename's stem is usually fine; override only if the stem is awkward.
- `--notes "..."` to insert a `## Notes` block between the title and `## Meals` (free-text reminders for the shop, surfaces as the editable Notes section in the iOS app).
- Omit `--output` to print to stdout (useful for piping or previewing).

What the script does, end to end:
1. Resolves recipes (parses the `.menu` file or takes them from `--recipes`). For Mode B, reads each referenced recipe's `servings:` frontmatter and computes the Cook CLI scale factor as `plan-servings / recipe-servings`.
2. Calls `cook shopping-list --format markdown --pretty` on the resolved recipes (with `:N` scale suffixes appended where the factor is not 1).
3. Bumps aisle headers from `# foo` to `## foo`.
4. Converts ingredient bullets `- *qty* name` into checkboxes `- [ ] *qty* name`.
5. Drops stray malformed empty-quantity bullets (e.g. `- ** black`) and any aisle section that ends up with no items.
6. Prepends `# <title>` and a `## Meals` section. In Mode B each meal is prefixed with its `==block==` heading from the menu file; the per-recipe servings annotation is shown as `(×N)` when ≠ 1.
7. If `--notes` was supplied, inserts the `## Notes` block between the title and `## Meals`.

This produces the exact heading hierarchy the iOS app's parser expects: `# title` → `## Notes` *(optional)* → `## Meals` → one `## <aisle>` per aisle.

### Step 4: One-off items and confirmation

After the script runs, edit the saved file directly to insert one-off items the user mentioned that aren't in any recipe (fruits, batteries, frying oil, etc.) — see the Tips section below. Then report the saved path and the first ~20 lines so the user can spot-check.

## Defaults and constraints

- **Output folder**: always `shopping-lists/` at the repo root. Never write the list elsewhere — this is exactly where the iOS app's Shopping Lists tab looks.
- **Date format**: `dd.mm.yyyy` (European), e.g. `02.05.2026`.
- **Aisle grouping**: always on — never use `--plain` for this skill.
- **Format**: always markdown (`--format markdown --pretty`). No JSON / YAML / human variants.
- **Checkboxes**: only on ingredient bullets (`- *qty* name` → `- [ ] *qty* name`). Meal-list bullets stay plain.
- **Heading depth**: aisle headers from Cook CLI are bumped from `#` to `##` so the file's hierarchy is `# title` → `## Notes` *(optional)* → `## Meals` → `## <aisle>` for each aisle.
- **H1 title**: just the filename's stem (e.g. `# shopping list 02.05.2026`). The iOS app stores the H1 verbatim as `ShoppingListDocument.title` and renders it as the visible heading; doubling up with a `Shopping list:` prefix would render as "Shopping list: shopping list 02.05.2026".
- **Meal list**: always present, even if there is only one recipe.
- **Cook CLI warnings**: the script captures stdout only, so Cook CLI's stderr warnings don't pollute the markdown.

## Examples

**User:** "Generate a shopping list for Pasta Bolognese, Beef Tacos, and Hummus."

1. Resolve paths: `Pasta/Pasta Bolognese.cook`, `Meat/Beef Tacos.cook`, `Sides/Hummus.cook`.
2. Propose filename `shopping list 02.05.2026.md`. User accepts.
3. Run Cook CLI, post-process, save to `shopping-lists/shopping list 02.05.2026.md`.
4. File looks like:

```markdown
# shopping list 02.05.2026

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

## Format reference

The full canonical example for the iOS Cookbook app's parser is at `cookbook-app/docs/examples/shopping-list.md`. Round-tripping through the iOS app is the strictest correctness bar — anything outside the four recognised block kinds (H1 title, `## Notes`, `## Meals`, `## <aisle>`) is dropped on the next save by the iOS app's parser.

| Block | Heading | Body |
|---|---|---|
| Title | `# <list title>` | One line, becomes `ShoppingListDocument.title`. |
| Notes *(optional)* | `## Notes` | Free-text body until the next `##` heading. Surfaces as the editable Notes section in the iOS app. |
| Meals | `## Meals` | Provenance bullets like `- **Saturday dinner** — Recipe Name`. |
| Aisle | `## <aisle name>` | One section per aisle, contents as `- [ ] *qty unit* item name`. |

Order: title → Notes → Meals → aisles. Block ordering matters; the iOS app's renderer canonicalises in this order on save.

## Tips

- If the user adds the `Pantry/Dairy Staples.cook` file (or similar staples placeholder) to the list, those items will appear under `## dairy` automatically — useful since the Cook iOS app can't add ad-hoc items.
- If a new ingredient lands under `## other` in the output, it's missing from `config/aisle.conf`. Suggest adding it (one line under the right `[section]`) so the next list groups it correctly.
- For scaling in **Mode A** (explicit recipes), append `:N` to a recipe path: `"Pasta/Pasta Bolognese.cook:2"` doubles it. In **Mode B** (`--menu`), per-recipe scaling is computed automatically from the `.menu` file's `{N%servings}` annotations.
- **Cook CLI scaling is integer-only.** A `.menu` reference like `{2%servings}` against a 4-serving recipe ideally maps to `:0.5`, but Cook CLI's `:N` syntax doesn't accept fractions. `build-list.py` rounds to the nearest integer (floor 1) and emits a stderr warning naming the affected recipe and the resulting quantity overshoot. For full precision in the shopping list, specify integer multiples of each recipe's documented servings in the `.menu` file. The same constraint is reported earlier by `build-menu.py` when it writes the `.menu`, so it usually surfaces at meal-plan time before reaching shopping-list generation.
- The user may want to add one-off items not in any recipe (e.g. "AA batteries for the kitchen scale"). After the file is generated, edit it directly to insert these as checkbox bullets under the most appropriate aisle section — `## other` if nothing else fits. For example:

```markdown
# shopping list 02.05.2026
## other
- [ ] *1 pack* AA batteries (for kitchen scale)
```

- For shop-level reminders (e.g. "ask the butcher to slice thinly"), use `--notes "..."` on the script invocation — the text becomes the editable Notes block in the iOS app, *not* a checklist item.