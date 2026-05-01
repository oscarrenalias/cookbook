# Cookbook

A personal recipe collection in [CookLang](https://cooklang.org/) format, used for cooking, weekly meal planning, and shopping-list generation with the [Cook CLI](https://cooklang.org/docs/cli/).

Recipes are plain-text `.cook` files with YAML frontmatter for metadata (servings, difficulty, cuisine, protein, course, tags, equipment) and CookLang body markup for ingredients (`@name{qty%unit}`), cookware (`#name{}`), and timers (`~{duration%unit}`). Conventions and the controlled vocabulary used across the collection are documented in [CLAUDE.md](CLAUDE.md).

## Layout

- **Meat/**, **Fish/**, **Pasta/**, **Pizza/**, **Rice/**, **Soup/**, **Salads/**, **Sides/**, **Vegetarian/** — recipes grouped by main category.
- **Pantry/** — placeholder "staples" lists (e.g. weekly dairy haul) used as a workaround when generating shopping lists, since the Cook iOS app doesn't support ad-hoc items.
- **plans/** — weekly menu files (`week-YYYY-MM-DD.menu`) and the markdown shopping lists generated from them.
- **config/aisle.conf** — Cook CLI aisle configuration so shopping lists group items by supermarket section.

## Common Cook CLI commands

```sh
cook recipe read "Meat/Roasted Pork Fillet.cook"
cook recipe scale "Pasta/Pasta Bolognese.cook" 6
cook shopping-list "Pasta/Pasta Bolognese.cook" "Soup/Salmon Soup.cook"
cook shopping-list --format markdown --pretty "Meat/Beef Tacos.cook" -o list.md
```

## Included agent skills

The repository ships with agent skills under `.claude/skills/` and `.agent/skills/` that automate common cookbook tasks:

| Skill | Purpose |
|---|---|
| `convert-recipe` | Import recipes from URLs or plain text and convert to CookLang. |
| `create-recipe` | Create a new CookLang recipe from a description or template. |
| `export-recipe` | Convert CookLang recipes to Markdown, JSON, YAML, or other formats. |
| `manage-pantry` | Track kitchen inventory, find expiring items, and discover recipes you can make now. |
| `meal-plan` | Plan weekly meals interactively and generate a combined shopping list. |
| `organize-recipes` | Structure folders, audit metadata consistency, and set up config files. |
| `scale-recipe` | Adjust recipe servings and display scaled ingredient quantities. |
| `search-recipes` | Find recipes by ingredient, tag, metadata, or text search. |
| `shopping-list` | Generate shopping lists from one or more recipes with scaling support. |
| `validate-recipes` | Check recipes for syntax errors, warnings, and best-practice issues. |

Invoke a skill with `/<skill-name>` (e.g. `/meal-plan`).
