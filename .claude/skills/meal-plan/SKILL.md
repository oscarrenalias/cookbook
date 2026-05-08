---
name: meal-plan
description: Plan weekly meals interactively and emit a CookLang .menu file under plans/ that the iOS Cookbook app can load
---

# Meal Plan

## Overview

Create meal plans by selecting recipes for each day/meal and saving them as a CookLang `.menu` file under `plans/`. The output must round-trip cleanly through both Cook CLI and the iOS Cookbook app's Meal Plans tab.

Use this skill when:
- Planning meals for the week.
- Preparing for a dinner party.
- Meal prepping in advance.
- Reducing food waste with intentional planning.

## Things to be aware of

Meals are prepared for **5 persons by default** unless stated otherwise (family of 5). Quantities are scaled per-recipe via the `{N%servings}` annotation on each recipe reference.

The output `.menu` file lives at `plans/<filename>.menu`. The iOS app discovers `.menu` files in this folder; Cook CLI reads from the same location.

## Process

### Step 1: Gather requirements

Ask:
- "How many days are you planning, and which days?"
- "Which meals each day? (breakfast / lunch / dinner)"
- "Default servings (or any per-meal exceptions)?"
- "Any dietary restrictions or preferences?"

### Step 2: Suggest recipes

Based on requirements:
1. Search the recipe collection for matches across `Meat/`, `Pasta/`, `Fish/`, etc.
2. Consider variety (don't repeat the same cuisine twice in a row).
3. Factor in `prep time` / `cook time` for busy weeknights — prefer recipes tagged `quick` or `weekday` for Mon–Thu.
4. Prefer recipes with overlapping ingredients to reduce shopping waste.

Present options for each meal slot.

### Step 3: Decide block labelling

Each `==block==` heading becomes the day-or-context label that appears in the resulting shopping list's `## Meals` provenance bullets. Two reasonable conventions:

- **Numeric**: `==Day 0==`, `==Day 1==`, … (good for week-of plans where the start date is in the filename).
- **Named**: `==Saturday dinner==`, `==Sunday lunch==`, `==Batch Prep==` (more descriptive, surfaces directly in shopping-list bullets).

Pick one style per file; don't mix.

### Step 4: Save the `.menu` file

Filename convention: `week-<yyyy>-<mm>-<dd>.menu` keyed to the week's start date (Saturday or Monday — match the user's working week). Example: `plans/week-2026-05-08.menu`.

**Always** emit the file via `build-menu.py` (sitting alongside this `SKILL.md`). The script enforces the canonical CookLang `.menu` format that the iOS app expects and validates that every referenced recipe exists. Construct the plan as a JSON object matching the schema below, pipe it via stdin, and let the script write the file:

```bash
cat <<'JSON' | <skill>/build-menu.py --input - --repo . --output "plans/week-2026-05-08.menu"
{
  "servings": 5,
  "blocks": [
    {
      "name": "Day 0",
      "meals": [
        {
          "category": "Dinner",
          "comment": "kids: makaroni; adults: grilled tuna + roasted veg",
          "recipes": [
            {"path": "Pasta/Makaroni with Minced Meat", "servings": 2},
            {"path": "Fish/Grilled Tuna", "servings": 2},
            {"path": "Sides/Roasted Vegetables (Side)", "servings": 4}
          ]
        }
      ]
    }
  ]
}
JSON
```

JSON schema:

| Field | Required | Notes |
|---|---|---|
| `servings` (top-level) | optional | Frontmatter informational only; written as `servings: N` in the file's YAML header. Per-recipe servings are set on each recipe entry. |
| `blocks[].name` | required | Becomes `==<name>==`. **Must not contain `==`**. The script rejects this. |
| `blocks[].meals[].category` | optional | Rendered as `Dinner:` / `Lunch:` / `Breakfast:` etc. before the recipes. |
| `blocks[].meals[].comment` | optional | Rendered as a `-- <comment>` line above the recipes. |
| `blocks[].meals[].recipes[].path` | required | Relative to repo root. **Without** leading `./`, **without** trailing `.cook`. The script rejects both. |
| `blocks[].meals[].recipes[].servings` | required | Plan-side absolute servings count for this occurrence. Must be > 0. |
| `blocks[].meals[].recipes[].unit` | optional | Default `"servings"`. Use `"batch"` for staples placeholders that don't follow the servings model. |

The script validates the whole plan before writing — any error exits non-zero and writes the file `plans/...` not at all. Warnings (e.g. recipe missing a `servings:` frontmatter, non-integer scale ratios) go to stderr but don't block writing.

Pass `--validate-only` to dry-run: validates the JSON and exits without producing output. Useful when checking a plan in flight.

After the script writes the file, spot-check the result and ask the user to confirm before moving on. Then offer to run the `shopping-list` skill in Mode B against the new file.

### Step 5: Generate the shopping list

Once the `.menu` file is saved, hand off to the `shopping-list` skill:

```
shopping-list skill — Mode B (--menu)
```

The shopping-list skill's `build-list.py` reads CookLang `.menu` files and produces an aisle-grouped markdown shopping list under `shopping-lists/`.

## `.menu` file format reference

The iOS Cookbook app and modern Cook CLI both parse this format. Other shapes (one-recipe-per-line, `Day:` prefixes, plain comment headers) **will not load in the iOS app**.

### Minimal example

```cooklang
---
servings: 4
---

==Day 0==
Dinner:
@./Pasta/Chicken Stir Fry{4%servings}
```

### Full weekly example (loads in the iOS app — see cookbook-app/docs/examples/meal-plan.menu)

```cooklang
---
servings: 4
---

==Day 0==
Dinner:
-- kids: makaroni; adults: grilled tuna + roasted veg
@./Pasta/Makaroni with Minced Meat{2%servings}
@./Fish/Grilled Tuna{2%servings}
@./Sides/Roasted Vegetables (Side){4%servings}

==Day 1==
Lunch:
@./Fish/Roasted Salmon{4%servings}
@./Sides/Oven Roasted Potatoes{4%servings}

Dinner:
@./Pasta/Pasta with Pesto and Bacon{4%servings}

==Day 2==
Dinner:
@./Meat/Chicken Stir Fry{4%servings}

== Batch Prep ==
-- Mon-Fri lunches, batch-cook on Sunday
@./Vegetarian/Tandoori Confit Chickpeas{5%servings}

== Dairy Staples ==
@./Pantry/Dairy Staples{1%batch}
```

### Format rules

| Element | Syntax | Notes |
|---|---|---|
| YAML frontmatter | `---` … `---` at top of file | Optional but recommended. `servings:` here is informational only — per-recipe servings are set on each reference. |
| Day / block heading | `==Day 0==` *or* `== Saturday dinner ==` | **Double** equals on both sides. Whitespace inside is preserved as the block's display name in the iOS app. |
| Meal-category subsection | `Dinner:`, `Lunch:`, `Breakfast:` | Lowercase or title-case both fine. Optional — only useful when a block has multiple meals. |
| Recipe reference | `@./Folder/Recipe Name{N%servings}` | Path is **relative** to the repo root, starts with `./`, omits the `.cook` extension. `N%servings` is the **absolute** servings count for this occurrence (not a multiplier). |
| Free-text comment | `-- inline comment text` | Two leading hyphens. Survives the parser; useful for notes like "kids: makaroni". |
| Free-text line | (anything else not matching the above) | Discarded by the iOS app's parser. Don't rely on it. |

### Things that break the iOS parser

- Single `=` for day blocks (that's recipe-body syntax).
- `Monday: Recipe.cook` style lines (old Cook CLI prose form).
- `# Comment header` followed by bare recipe paths (intermediate format we used to use — superseded).
- `.cook` extension in the `@./...` reference path.
- Missing leading `./` in the reference path.
- Unbalanced `==` markers (`==Day 0=` or `=Day 0==`).

### Servings and quantities

The `{N%servings}` annotation tells the iOS app and Cook CLI how many servings of each recipe this slot calls for. The downstream shopping-list scale factor for each recipe is computed as `plan-servings / recipe-frontmatter-servings`. Examples:

- Recipe documented as 4 servings, plan calls for `{4%servings}` → scale 1× (no change).
- Recipe documented as 4 servings, plan calls for `{8%servings}` → scale 2×.
- Recipe documented as 4 servings, plan calls for `{2%servings}` → scale 0.5×.

For staples / batch placeholders that don't follow the servings model (e.g. `Pantry/Dairy Staples`), use `{1%batch}` — the unit is informational and the recipe is included as-is.

## Examples

**User:** "Plan weeknight dinners Mon–Thu for 5 people, prefer quick meals."

1. Ask about cuisines / restrictions.
2. Search for `quick` + `weekday` tagged recipes.
3. Suggest variety (e.g. one chicken, one pasta, one fish, one vegetarian).
4. Save as `plans/week-2026-05-08.menu`:

   ```cooklang
   ---
   servings: 5
   ---

   ==Monday==
   Dinner:
   @./Meat/Chicken Stir Fry{5%servings}

   ==Tuesday==
   Dinner:
   @./Pasta/Pasta with Pesto and Bacon{5%servings}

   ==Wednesday==
   Dinner:
   @./Fish/Grilled Tuna{5%servings}

   ==Thursday==
   Dinner:
   @./Vegetarian/Tandoori Confit Chickpeas{5%servings}
   ```

5. Suggest running the `shopping-list` skill against the new file.

## Planning tips

**Weekday strategies:**
- Pick recipes tagged `quick` or `weekday`.
- One-pot / sheet-pan dishes.
- Plan a `batch-cook` recipe on Sunday to cover several lunches.

**Weekend strategies:**
- Longer cook times OK.
- Reach for `involved` or `weekend`-tagged recipes.
- Bulk-cook a freezer-friendly main for the week ahead.

**Reduce waste:**
- Use overlapping ingredients across the week.
- Buy proteins in bulk and portion at home.
- Schedule fresh produce earlier in the week.
