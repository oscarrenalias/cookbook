---
name: estimate-cost
description: Estimate what a shopping list costs at K-Ruoka, honouring preferred brands, with every chosen product named so wrong picks are easy to correct
---

# Estimate Cost

## Overview

Point this at a shopping list in `shopping-lists/` and it prints a per-item and total cost estimate using live K-Ruoka prices from your own store, including Plussa offers.

It only ever reads. Nothing is written back to the shopping list, no basket is touched, and checkout is out of scope.

Use this skill when:
- You want to know roughly what this week's shop will cost.
- You want to see which items dominate the bill.
- You want to check whether a preferred brand is worth the difference.

**Prerequisite:** the `kruoka-auth` skill must have captured a signed-in session. If this reports an expired session or a Cloudflare challenge, run `.claude/skills/kruoka-auth/kruoka-auth.py capture`.

## Process

### Step 1: Run it

```bash
.claude/skills/estimate-cost/estimate-cost.py "shopping-lists/19.09.2026.md"
```

The script resolves its own dependencies through `uv`; there is nothing to install. A cold run makes one API call per distinct ingredient and caches the results for 7 days, so later runs are near-instant.

### Step 2: Read the output

Each line shows the cost, the item as written in your list, the quantity, the unit price, and **the actual product chosen**. That last column is the important one — it is how you spot a bad match.

```
## canned
     3.98  canned tomatoes           1.6 kg      2.49/kg  Mutti tomaattimurska 3x400g 3-pack  [STANDARD]
     1.24  coconut milk              400 ml       3.10/l  Pirkka kookosmaito 400ml
```

Then a summary:

```
  Confident subtotal     175.18 EUR
  Guessed subtotal         1.70 EUR   (flagged above)
  ESTIMATED TOTAL        176.88 EUR
  4 item(s) not priced -- total is an undercount.
```

### Step 3: Fix bad matches in the config

If a line picked the wrong product, add or refine an entry in `config/kruoka-products.yaml`. That is the whole maintenance loop — the output names the product precisely so you can tell.

Use `lookup` to try a search term before committing to it:

```bash
.claude/skills/estimate-cost/estimate-cost.py lookup tomaattimurska --brand Mutti
```

## Preferred brands

Brand preference is the reason this config exists. Set `brand:` on any ingredient where the store's own label is not what you buy:

```yaml
products:
  crushed tomatoes:
    query: tomaattimurska
    brand: Mutti
    exclude: [basilika, valkosipuli, luomu]
  spaghetti:
    query: spagetti
    brand: Rummo
```

This costs real money and that is the point: Mutti crushed tomatoes are €2.60/kg against Pirkka's €1.98/kg, so the estimate is ~31% higher on those lines than a cheapest-wins estimate would be.

**If a preferred brand has no match**, the line is still priced using the cheapest alternative and flagged with `[Mutti not found]`, so one out-of-stock product never invalidates the total.

## Config reference — `config/kruoka-products.yaml`

Only ingredients that need help are listed. Anything absent is searched by its own name, which works for items already written in Finnish and usually fails for English ones.

| Key | Purpose |
|---|---|
| `query` | The Finnish search term. Shorthand: `milk: maito` is the same as `milk: {query: maito}`. |
| `brand` | Restrict to this brand, matched case-insensitively against the product's brand. |
| `category` | Substring of the K-Ruoka category path, e.g. `hedelmat-ja-vihannekset`. The strongest filter available — use it when search returns the right word in the wrong kind of product. |
| `exclude` | Words that disqualify a product, e.g. `[luomu, maustettu]`. |
| `ean` | Pin one exact product. Most precise, most brittle. |

Two top-level lists:

- `pantry` — assumed already in the cupboard. Priced at €0 and shown separately, because `salt` appears on 33 of 53 lists and `olive oil` on 29; recipes declare them, you do not rebuy them weekly. `--include-pantry` prices them for a genuine restock.
- `skip` — never a purchase at all (`water`, `pasta cooking water`).

## How a product gets chosen

1. Resolve the item name through the config, then through `config/aisle.conf` synonyms, then fall back to the name itself.
2. Search K-Ruoka (cached 7 days).
3. Drop blocked categories — baby food, ready meals and pet food, which routinely match ingredient names and undercut the real product on price.
4. Apply the entry's `category`, `brand` and `exclude` filters.
5. Require every query term to appear in the product name. Long terms match as substrings, since Finnish compounds work that way; short ones must match a whole word, or `voi` (butter) matches `voileipäkeksi` (sandwich biscuit).
6. Rank by match quality, then price. A standalone word beats a prefix, because compounds built on a term are usually a different product — `maitokolmio` is a chocolate drink, `munakoisopyree` is purée.
7. Prefer an active Plussa or standard offer over the normal price.

If nothing survives, the line is reported as **not priced** with the term that failed. That is deliberate: silently pricing the wrong thing is worse than admitting a gap.

## Reading the quantities

| In the list | How it is priced |
|---|---|
| `*700 g*`, `*1.5 kg*` | Comparison price (€/kg) × quantity. |
| `*400 ml*`, `*2 l*` | Comparison price (€/l) × quantity. |
| `*3 piece*`, `*2 can*` | Package price × count. A lemon is €0.51, a garlic bulb €0.45. |
| `*2 tsp*`, `*1 tbsp*` | Seasoning. Priced as one package; the real cost is pennies. |
| no quantity | Assumed one package, marked `*` in the output. |
| `*800 g, 1.5 kg*` | Summed, 2.3 kg. Mixed kinds pick the dominant one. |
| `*4 400 g tins*` | Four 400-gram tins, 1.6 kg. |

## Defaults and constraints

- **Read-only.** Never edits the shopping list, never touches a basket.
- **Store** comes from the captured session — currently `N131`, K‑Citymarket Espoo Sello. Prices and availability are store-specific.
- **Cache** lives in `.cache/kruoka/` (gitignored), 7-day TTL. `--refresh` bypasses it.
- **Multi-buy offers are ignored.** A `batch` price is a total requiring a minimum quantity, so applying it to an arbitrary amount would understate the bill.
- **Accuracy is roughly ±15–25%** on the total. It is an estimate for planning, not a quote.
- Hand-written lists without quantities estimate much less precisely — only 21 of 53 lists carry quantities, and the rest lean entirely on the one-package assumption.

## Commands

```bash
# Price a list
estimate-cost.py "shopping-lists/19.09.2026.md"

# Include pantry staples (a genuine restock)
estimate-cost.py "shopping-lists/19.09.2026.md" --include-pantry

# Ignore cached prices
estimate-cost.py "shopping-lists/19.09.2026.md" --refresh

# Machine-readable output as well
estimate-cost.py "shopping-lists/19.09.2026.md" --json /tmp/estimate.json

# Try a search term before adding it to the config
estimate-cost.py lookup kanan rintafile
estimate-cost.py lookup spagetti --brand Rummo

# See what is still unmapped, ranked by how often it appears
estimate-cost.py coverage

# Find unmapped ingredients, then verify and append translations
estimate-cost.py learn --limit 60
estimate-cost.py learn --apply proposals.json --dry-run
estimate-cost.py learn --apply proposals.json
```

## Growing the config — the agent does this, not the user

`config/kruoka-products.yaml` is meant to be **agent-maintained**. Nobody should be hand-writing Finnish translations into it. When coverage is low or an estimate reports unpriced items, run this loop:

### Step 1: Get the unmapped ingredients

```bash
estimate-cost.py learn --limit 60
```

Prints a JSON array of unmapped ingredient names with how often each appears across all lists, most frequent first.

### Step 2: Translate them

Write a JSON array of proposals. Only `ingredient` and `query` are required:

```json
[
  {"ingredient": "hot dog buns", "query": "hodarisämpylä"},
  {"ingredient": "cheddar cheese", "query": "cheddar", "category": "juusto"},
  {"ingredient": "frying oil", "query": "rypsiöljy", "exclude": ["margariini"]},
  {"ingredient": "garam masala", "pantry": true},
  {"ingredient": "soap for pool", "skip": true}
]
```

### Step 3: Verify before writing — **do not skip this**

```bash
estimate-cost.py learn --apply proposals.json --dry-run
```

This runs a real search for every proposal and prints the product each term actually resolves to. **Read that output.** A translation that reads perfectly can still be wrong, and the only way to find out is to look:

- `cava` → *De Cecco **Cava**tappi pasta* — rejected automatically, the term only matched inside a longer word
- `cheddar` → *Sundlings Cheddar **Popcornmauste*** — accepted by the tool, wrong to a human; needs `category: juusto`
- `rypsiöljy` → *savustetut sinisimpukat* (mussels packed in rapeseed oil) — needs an `exclude`
- `tortilla` → *Tortilla **Chips*** — needs `exclude: [chips]`
- `liemikuutio` → nothing at all, while plain `liemi` works

Fix the bad ones by adding `category` or `exclude`, and re-run the dry run until the matches look right.

### Step 4: Write

```bash
estimate-cost.py learn --apply proposals.json
```

Appends the verified entries to the config, preserving comments and structure. Rejected proposals are listed with the reason so you can retry them with better terms.

### What gets rejected automatically

- The term matches no product at all.
- A specified `brand` is not stocked for that term.
- The term only matches *inside* a longer compound word (`cava` in `cavatappi`). This is stricter than the estimator's own matching, which tolerates a prefix as a fallback — a mapping being written to disk should rest on a clean word match.

Finnish inflection is allowed for: `kalafile` matches `kalafileet`, since the extra suffix is short.

### Why persist it at all, rather than translating on the fly

- **Comparability.** If the term drifts between runs, your weekly totals move for reasons unrelated to prices. Pinned terms mean a change in the total is a real change in cost.
- **No agent required.** `estimate-cost.py "list.md"` stays a plain CLI, usable from a script or a shell with no LLM in the loop.
- **Verification is the valuable part.** The file records terms that were checked against real search results, not terms that merely sounded right.
