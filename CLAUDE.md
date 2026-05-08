# Cookbook

A personal recipe collection in [CookLang](https://cooklang.org/) format. Used for cooking, weekly meal planning, and shopping-list generation.

## Consumers

These files are read by **two parsers**, both of which must be kept happy:

1. **Cook CLI** (<https://cooklang.org/docs/cli/>) — local tooling for rendering, scaling, and shopping-list generation.
2. **The CookbookApp iOS app** (separate repo at `~/Projects/cookbook-app`) — loads this folder as its recipes source. Reads `.cook` files, `.menu` files in `plans/`, `.md` shopping lists in `shopping-lists/`, and `config/aisle.conf`. Canonical examples live at `cookbook-app/docs/examples/{recipe.cook, meal-plan.menu, shopping-list.md, aisle.conf}`.

When in doubt about format, the iOS app's parser is the stricter consumer — round-tripping through it is the safest correctness bar. Files the iOS app can't load silently disappear from the relevant tab.

## Repository layout

```
Meat/         # chicken, beef, pork, lamb, mixed-meat dishes
Fish/         # salmon, tuna, fish, shrimp dishes
Pasta/        # anything tagged `pasta-dish`
Pizza/        # pizza recipes
Rice/         # anything tagged `rice-dish`
Soup/         # anything tagged `soup`
Salad/        # salad recipes
Sides/        # side dishes (e.g. hummus)
Desserts/     # sweet courses (e.g. tiramisu)
Vegetarian/   # vegetarian dishes (beans, lentils, egg, cheese-not-pizza, vegetables)
Pantry/       # non-recipe "staples" lists (workaround for Cook iOS app's lack of ad-hoc items)
config/       # Cook CLI config: aisle.conf (and planned pantry.conf)
plans/        # weekly menus, e.g. week-2026-05-02.menu, plus generated shopping lists
```

Every recipe lives in exactly one category folder; nothing sits at the repo root. Pick the folder that matches the recipe's most prominent tag (`pasta-dish`, `rice-dish`, `soup`, `salad`) first, then fall back to protein/main-component (`Pizza`, `Fish`, `Meat`, `Vegetarian`). Folder names are Title Case.

## File naming

- One recipe per `.cook` file. Place it in the matching category folder (see Repository layout).
- Title Case with spaces, matching the `title:` frontmatter value: `Roasted Pork Fillet.cook`.
- Minor words stay lowercase when not the first word: `with`, `and`, and Spanish `de` (e.g. `Pasta with Tomato Sauce.cook`, `Tortilla de Patatas.cook`).
- The filename is the source of truth for the title — keep it in sync with the `title:` field.

## Recipe file format

Every `.cook` file starts with **YAML frontmatter** (the older `>> key: value` syntax is deprecated and must not be used), followed by CookLang body.

### Frontmatter template

```cook
---
title: Recipe Name
servings: 4
prep time: 15 minutes
cook time: 25 minutes
difficulty: easy
cuisine: generic
protein: chicken
course: dinner
tags:
  - weekday
  - kid-friendly
equipment:
  - frying pan
  - mixing bowl
---
```

### Metadata rules

- Keys use spaces, not snake_case: `prep time`, not `prep_time`. Use `course` (not `meal_type`), `servings` numeric. Total time is implicit (`prep time` + `cook time`); a separate `time:` key is redundant and not consumed by either Cook CLI or the iOS app — don't add one.
- Values are lowercase except `title`.
- `tags` and `equipment` are YAML arrays. Put planning traits in `tags` rather than inventing new boolean keys.
- Don't add equivalent variants of the same concept (`batch-cook` vs `batch_cook` vs `suitable_for_batching`).

### Controlled vocabulary

| Field | Allowed values |
|---|---|
| `difficulty` | `easy`, `medium`, `involved` |
| `course` | `breakfast`, `lunch`, `dinner`, `snack`, `side`, `dessert` |
| `protein` | `chicken`, `beef`, `pork`, `fish`, `salmon`, `tuna`, `shrimp`, `egg`, `tofu`, `beans`, `lentils`, `cheese`, `mixed`, `none` |
| `cuisine` | `finnish`, `italian`, `japanese`, `japanese-inspired`, `chinese`, `thai`, `indian`, `mexican`, `spanish`, `middle-eastern`, `american`, `generic`, `fusion` |

### Tag vocabulary

```
weekday  weekend  kid-friendly  batch-cook  freezer-friendly  quick
vegetarian  pescatarian  vegan  spicy  mild  comfort-food  one-pot
oven  grill  stir-fry  leftovers  rice-dish  pasta-dish  soup  salad
no-cook  fresh  needs-review  staples
```

Tag semantics:
- `quick` — total time ≤ ~30 minutes.
- `weekday` — feasible on a normal work/school evening (≤ ~45 minutes).
- `batch-cook` — scales well, intentionally produces leftovers.
- `freezer-friendly` — finished dish freezes well.
- `leftovers` — especially good the next day.
- `needs-review` — generated with uncertain assumptions; verify before relying on it.
- `staples` — file is a placeholder list of regularly-bought items, not a real recipe (lives in `Pantry/`).

### Default tag inference

- Total time ≤ 30 min → add `quick`.
- Total time ≤ 45 min → add `weekday`.
- Sauces, stews, soups, meatballs, casseroles, oven trays → `batch-cook`, often `freezer-friendly`.
- Mild familiar dishes → `kid-friendly`.
- Add `spicy` only when chili/curry heat is central; otherwise `mild` for family-style dishes.

## Section heading syntax

Two different "section" markers exist; do not mix them up:

| Where | Syntax | Meaning |
|---|---|---|
| Recipe `.cook` body | `=Section name` (single equals, optionally with trailing spaces) | Subdivides a recipe's steps (e.g. `=Filling`, `=Sauce`). Modern CookLang format. |
| Meal plan `.menu` body | `==Day or block name==` (double equals on both sides) | A day or named planning block in a `.menu` file. **Never use this in a recipe body.** |

The iOS app's parser is strict about this distinction. Putting `==Filling==` inside a recipe will not be recognised as a section.

## CookLang body syntax

### Ingredients

```cook
@ground chicken{700%g}
@soy sauce{2%tbsp}
@egg{1}
@rice{}
@fresh ginger{2%tsp}(grated)
```

- Singular names: `egg`, not `eggs`.
- Units: `g`, `kg`, `ml`, `l`, `tsp`, `tbsp`, `piece`, `can`, `minutes`/`min`. Use `can` for canned goods (tomatoes, beans, coconut milk, chickpeas, etc.) and note the can size in parentheses, e.g. `@canned tomatoes{2%can}` (400 g each).
- Empty quantity `{}` is fine for "to taste" or serving-side ingredients.
- Put preparation in parentheses *after* the ingredient (`(grated)`, `(finely chopped)`) instead of folding it into the name.

### Cookware

```cook
#frying pan{}
#mixing bowl{}
#oven{}
```

Mark only equipment that matters for cooking or planning — don't tag every spoon.

### Timers

```cook
~{10%minutes}
~{8%min} to ~{10%min}
```

Use timers where they actually help cooking flow.

## When creating or editing recipes

1. Pick a Title Case filename with spaces; it must match the `title:` field exactly.
2. Use the frontmatter template above. All required keys (`title`, `servings`, `prep time`, `cook time`, `difficulty`, `cuisine`, `protein`, `course`, `tags`, `equipment`) must be present.
3. Keep recipes practical and household-style, sized for family cooking, with common European/Finnish supermarket ingredients unless the cuisine implies otherwise.
4. Mark every shopping-list-relevant ingredient with `@…{qty%unit}` so shopping-list generation works.
5. If you make non-trivial assumptions (substitutions, invented quantities, unfamiliar dish), add `needs-review` to `tags`.

## Validation checklist before saving

- [ ] Filename is Title Case with spaces and matches `title:`.
- [ ] File starts and ends frontmatter with `---`.
- [ ] `servings` is numeric.
- [ ] All controlled-vocabulary fields use allowed values.
- [ ] Ingredients use `@name{qty%unit}` syntax.
- [ ] Major cookware uses `#name{}` syntax.
- [ ] Useful timers use `~{duration%unit}`.
- [ ] Uncertain recipes are tagged `needs-review`.

## Cook CLI

Common commands when working in this repo:

```sh
cook recipe read "Meat/Roasted Pork Fillet.cook"               # render a recipe
cook recipe scale "Pasta/Pasta Bolognese.cook" 6               # rescale servings
cook shopping-list "Pasta/Pasta Bolognese.cook" "Soup/Salmon Soup.cook"
```

For meal plans and shopping lists, prefer the dedicated skills (`meal-plan`, `shopping-list`) over hand-authoring or invoking `cook shopping-list` directly — they output formats the iOS app can load.

## Generated outputs

### Meal plans (`plans/*.menu`)

Modern CookLang `.menu` format with optional YAML frontmatter and `==Day==` blocks containing `@./relative-path/Recipe Name{N%servings}` references. See `cookbook-app/docs/examples/meal-plan.menu` for the canonical shape. The iOS app expects this format; the older "one recipe path per line with `#` comment headers" format is **not** supported.

### Shopping lists (`shopping-lists/*.md`)

Markdown with this exact heading hierarchy:

- `# <list title>` — H1, becomes the list's in-app title.
- `## Notes` *(optional)* — free-text block, surfaces as the editable Notes section in the iOS app.
- `## Meals` — provenance bullets, e.g. `- **Saturday dinner** — Recipe Name`.
- `## <aisle name>` — one section per aisle; checklist items as `- [ ] *qty unit* item name`.

Anything outside those four block kinds is dropped on save by the iOS app's parser.

Weekly menus go in `plans/` as `.menu` files; shopping lists derived from them go in `shopping-lists/`.
