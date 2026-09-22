#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["httpx>=0.27", "keyring>=25", "pyyaml>=6"]
# ///
"""Estimate what a shopping list costs at K-Ruoka.

Reads a markdown shopping list from shopping-lists/, looks each item up in
K-Ruoka's product API, and prints a per-item and total estimate. Nothing is
ever written back to the list.

Preferred brands come from config/kruoka-products.yaml. Only ingredients that
need help are listed there; anything else is searched by its own name.

Credentials come from the kruoka-auth skill (environment variables first,
macOS Keychain second). Run `kruoka-auth.py capture` if this reports an
expired session.

Subcommands:
  estimate    price a shopping list (default)
  lookup      search K-Ruoka directly, to help write the config
  coverage    scan every list and rank unmapped ingredients by frequency

Usage:
  ./estimate-cost.py "shopping-lists/19.09.2026.md"
  ./estimate-cost.py lookup tomaattimurska --brand Mutti
  ./estimate-cost.py coverage
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
AUTH_SCRIPT = REPO_ROOT / ".claude" / "skills" / "kruoka-auth" / "kruoka-auth.py"
PRODUCTS_CONFIG = REPO_ROOT / "config" / "kruoka-products.yaml"
AISLE_CONFIG = REPO_ROOT / "config" / "aisle.conf"
SHOPPING_LISTS = REPO_ROOT / "shopping-lists"
CACHE_DIR = REPO_ROOT / ".cache" / "kruoka"
CACHE_TTL_DAYS = 7
CACHE_VERSION = 2          # bump when trim() changes shape, to retire old files
SEARCH_LIMIT = 100

# Categories that are essentially never what a recipe means by an ingredient.
# Search happily returns baby-food puree for "kesakurpitsa" and a microwave
# pasta for "parmesaani", and both undercut the real product on price.
BLOCKED_CATEGORIES = (
    "lapset/",          # baby and toddler food
    "valmisruoka/",     # ready meals
    "lemmikit",         # pet food
    "elaintarvikkeet",
)

# --- shopping list grammar -------------------------------------------------

AISLE_HEADING = re.compile(r"^##\s+(.+?)\s*$")
ITEM_LINE = re.compile(r"^-\s+\[([ xX])\]\s*(.*?)\s*$")
# Leading *...* emphasis holds the quantity, when the list was generated.
EMPHASIS = re.compile(r"^\*(?P<qty>[^*]*)\*\s*(?P<rest>.*)$")
# A trailing "(for grilling)" style note is commentary, not part of the name.
TRAILING_NOTE = re.compile(r"^(?P<name>.*?)\s*\((?P<note>[^)]*)\)\s*$")
# One comma-separated quantity chunk: a number (or fraction) plus a unit tail.
# The fraction branch must come first: alternation is ordered, and the plain
# number branch would otherwise swallow the "1" of "1/2 tsp".
QTY_CHUNK = re.compile(r"^(?P<num>\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)\s*(?P<unit>.*)$")
# A pack size hiding inside the unit, as in "4 400 g tins".
PACK_SIZE = re.compile(r"^(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>g|kg|ml|l|dl)\b")
# Headings that are not aisles.
RESERVED_HEADINGS = {"notes", "meals"}

MASS_TO_G = {"g": 1.0, "gram": 1.0, "grams": 1.0, "kg": 1000.0}
VOLUME_TO_ML = {"ml": 1.0, "l": 1000.0, "dl": 100.0, "cl": 10.0}
COUNT_UNITS = {
    "", "piece", "pieces", "pcs", "kpl", "pkg", "pack", "packs", "package",
    "can", "cans", "tin", "tins", "jar", "jars", "bottle", "bottles",
    "bunch", "bunches", "head", "heads", "clove", "cloves", "slice", "slices",
    "bag", "bags", "box", "boxes", "punnet", "sachet", "sachets", "tub", "tubs",
}
# Seasoning amounts. Real cost is pennies; pricing them precisely is noise.
NEGLIGIBLE_UNITS = {
    "tsp", "tsps", "teaspoon", "teaspoons", "tbsp", "tbsps", "tablespoon",
    "tablespoons", "pinch", "pinches", "dash", "drop", "drops", "c", "cup", "cups",
}

MASS = "mass"
VOLUME = "volume"
COUNT = "count"
NEGLIGIBLE = "negligible"


class EstimateError(Exception):
    """An actionable error safe to print."""


# --- data ------------------------------------------------------------------

@dataclass
class Measure:
    kind: str = COUNT          # MASS | VOLUME | COUNT | NEGLIGIBLE
    amount: float = 1.0        # grams, millilitres, or a count
    assumed: bool = False      # no quantity given; assumed one package
    unreadable: str = ""       # the bit we could not parse, if any


@dataclass
class Item:
    aisle: str
    name: str
    raw: str
    note: str = ""
    measure: Measure = field(default_factory=Measure)

    @property
    def qualified(self) -> str:
        """Name with its parenthetical kept, e.g. `rahka (flavoured)`.

        `(for grilling)` is commentary, but `(flavoured)` distinguishes two
        real products, so config lookups try this form before the bare name.
        """
        return f"{self.name} ({self.note})" if self.note else self.name


@dataclass
class Pick:
    tag: str                   # exact|brand|generic|guessed|pantry|unpriced
    cost: float = 0.0
    product: str = ""
    brand: str = ""
    unit_price: str = ""
    reason: str = ""
    approximate: bool = False
    on_offer: str = ""


# --- parsing ---------------------------------------------------------------

def _number(text: str) -> float | None:
    text = text.strip().replace(",", ".")
    if "/" in text:
        parts = [p.strip() for p in text.split("/", 1)]
        try:
            top, bottom = float(parts[0]), float(parts[1])
            return top / bottom if bottom else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_quantity(text: str) -> Measure:
    """Read a `*...*` quantity block, which is messier than the spec suggests.

    Real lists contain `800 g, 1.5 kg`, `3 tsp, to taste`, `4 400 g tins`,
    `1/2 tsp`, `2 tsp, 22` and `29.051 tbsp`. Each comma-separated chunk is
    read independently, then chunks of the same kind are summed. When kinds
    disagree, mass beats volume beats count -- the seasoning half of
    `100 g, 3 tbsp` should not decide how flour gets priced.
    """
    totals: dict[str, float] = {}
    unreadable: list[str] = []

    for chunk in (c.strip() for c in text.split(",")):
        if not chunk:
            continue
        match = QTY_CHUNK.match(chunk)
        if not match:
            unreadable.append(chunk)
            continue
        count = _number(match.group("num"))
        if count is None:
            unreadable.append(chunk)
            continue
        unit = match.group("unit").strip().lower()

        # "4 400 g tins" -> four 400-gram tins.
        pack = PACK_SIZE.match(unit)
        if pack:
            size = _number(pack.group("size")) or 0.0
            pack_unit = pack.group("unit")
            if pack_unit in MASS_TO_G:
                totals[MASS] = totals.get(MASS, 0.0) + count * size * MASS_TO_G[pack_unit]
            else:
                totals[VOLUME] = totals.get(VOLUME, 0.0) + count * size * VOLUME_TO_ML[pack_unit]
            continue

        if unit in MASS_TO_G:
            totals[MASS] = totals.get(MASS, 0.0) + count * MASS_TO_G[unit]
        elif unit in VOLUME_TO_ML:
            totals[VOLUME] = totals.get(VOLUME, 0.0) + count * VOLUME_TO_ML[unit]
        elif unit in NEGLIGIBLE_UNITS:
            totals[NEGLIGIBLE] = totals.get(NEGLIGIBLE, 0.0) + count
        elif unit in COUNT_UNITS:
            totals[COUNT] = totals.get(COUNT, 0.0) + count
        else:
            unreadable.append(chunk)

    for kind in (MASS, VOLUME, COUNT, NEGLIGIBLE):
        if kind in totals:
            return Measure(kind=kind, amount=totals[kind], unreadable=", ".join(unreadable))
    return Measure(kind=COUNT, amount=1.0, assumed=True, unreadable=", ".join(unreadable))


def parse_list(path: pathlib.Path) -> list[Item]:
    """Pull items out of a shopping list, tolerating hand-written variants."""
    items: list[Item] = []
    aisle = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = AISLE_HEADING.match(line)
        if heading:
            aisle = heading.group(1).strip()
            continue
        if aisle.lower() in RESERVED_HEADINGS:
            continue
        entry = ITEM_LINE.match(line)
        if not entry:
            continue
        body = entry.group(2).strip()
        if not body:
            continue

        measure = Measure(assumed=True)
        emphasis = EMPHASIS.match(body)
        if emphasis:
            measure = parse_quantity(emphasis.group("qty"))
            body = emphasis.group("rest").strip()
        if not body:
            continue

        note = ""
        trailing = TRAILING_NOTE.match(body)
        if trailing and trailing.group("name").strip():
            note = trailing.group("note").strip()
            body = trailing.group("name").strip()

        # "2 Hand soap" -- a count written outside the emphasis block.
        if measure.assumed:
            leading = re.match(r"^(\d+)\s+(\S.*)$", body)
            if leading:
                measure = Measure(kind=COUNT, amount=float(leading.group(1)))
                body = leading.group(2).strip()

        items.append(Item(aisle=aisle, name=body, raw=line.strip(), note=note, measure=measure))
    return items


def normalise(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.strip().lower())
    return re.sub(r"\s+", " ", text).strip()


# --- configuration ---------------------------------------------------------

@dataclass
class Spec:
    query: str
    brand: str = ""
    ean: str = ""
    category: str = ""
    exclude: list[str] = field(default_factory=list)
    source: str = "config"


@dataclass
class Config:
    pantry: set[str] = field(default_factory=set)
    skip: set[str] = field(default_factory=set)
    products: dict[str, Spec] = field(default_factory=dict)
    synonyms: dict[str, str] = field(default_factory=dict)

    def resolve(self, *names: str) -> Spec | None:
        """First match wins, so callers pass the most specific name first."""
        for name in names:
            key = normalise(name)
            if key in self.products:
                return self.products[key]
            canonical = self.synonyms.get(key)
            if canonical and canonical in self.products:
                return self.products[canonical]
        return None

    def classify(self, *names: str) -> str:
        for name in names:
            key = normalise(name)
            canonical = self.synonyms.get(key, key)
            if key in self.skip or canonical in self.skip:
                return "skip"
            if key in self.pantry or canonical in self.pantry:
                return "pantry"
        return "price"


def load_config(path: pathlib.Path, aisle_path: pathlib.Path) -> Config:
    import yaml

    config = Config()
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise EstimateError(f"{path} should contain a YAML mapping.")
        config.pantry = {normalise(x) for x in (raw.get("pantry") or []) if isinstance(x, str)}
        config.skip = {normalise(x) for x in (raw.get("skip") or []) if isinstance(x, str)}
        for name, value in (raw.get("products") or {}).items():
            key = normalise(str(name))
            if isinstance(value, str):
                config.products[key] = Spec(query=value)
            elif isinstance(value, dict):
                config.products[key] = Spec(
                    query=str(value.get("query") or name),
                    brand=str(value.get("brand") or ""),
                    ean=str(value.get("ean") or ""),
                    category=str(value.get("category") or ""),
                    exclude=[str(x).lower() for x in (value.get("exclude") or [])],
                )
            else:
                raise EstimateError(f"products.{name} must be a string or a mapping.")
    config.synonyms = load_synonyms(aisle_path)
    return config


def load_synonyms(path: pathlib.Path) -> dict[str, str]:
    """Map every alias in aisle.conf to its canonical (first) name.

    The file has warts: trailing whitespace on some entries, a `chcick peas`
    typo that is itself a canonical, and canonicals repeated across sections.
    First definition wins; the rest are harmless.
    """
    synonyms: dict[str, str] = {}
    if not path.exists():
        return synonyms
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("#"):
            continue
        parts = [normalise(p) for p in line.split("|")]
        parts = [p for p in parts if p]
        if not parts:
            continue
        canonical = parts[0]
        for alias in parts:
            synonyms.setdefault(alias, canonical)
    return synonyms


# --- K-Ruoka API -----------------------------------------------------------

def load_auth():
    """Borrow the kruoka-auth skill's credential handling and error taxonomy."""
    if not AUTH_SCRIPT.exists():
        raise EstimateError(
            f"Cannot find {AUTH_SCRIPT}. The kruoka-auth skill provides the "
            "credentials this tool needs."
        )
    spec = importlib.util.spec_from_file_location("kruoka_auth", AUTH_SCRIPT)
    if spec is None or spec.loader is None:
        raise EstimateError(f"Could not load {AUTH_SCRIPT}.")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves annotations through
    # sys.modules, and blows up if the module is not there yet.
    sys.modules["kruoka_auth"] = module
    spec.loader.exec_module(module)
    return module


def _campaign_active(block: dict, now: datetime) -> bool:
    for field_name, is_start in (("startDate", True), ("endDate", False)):
        value = block.get(field_name)
        if not isinstance(value, str):
            return False
        try:
            if len(value) == 10:
                boundary, current = date.fromisoformat(value), now.date()
            else:
                boundary = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if boundary.tzinfo is None:
                    boundary = boundary.replace(tzinfo=now.tzinfo)
                current = now
        except ValueError:
            return False
        if (is_start and current < boundary) or (not is_start and current > boundary):
            return False
    return True


def trim(product: dict) -> dict:
    """Keep only the fields pricing needs, so the cache stays small."""
    pricing = (product.get("mobilescan") or {}).get("pricing") or {}
    normal = pricing.get("normal") or {}
    discount = pricing.get("discount") or {}
    batch = pricing.get("batch") or {}
    category = product.get("category") or {}
    return {
        "ean": product.get("ean") or "",
        "category": category.get("path") or "",
        "name": (product.get("localizedName") or {}).get("finnish") or "",
        "brand": (product.get("brand") or {}).get("name") or "",
        "web": bool((product.get("availability") or {}).get("web")),
        "price": normal.get("price"),
        "unit_value": (normal.get("unitPrice") or {}).get("value"),
        "unit": (normal.get("unitPrice") or {}).get("unit") or "",
        "approximate": normal.get("isApproximate") is True,
        "discount": {
            "price": discount.get("price"),
            "unit_value": (discount.get("unitPrice") or {}).get("value"),
            "type": discount.get("discountType") or "",
            "startDate": discount.get("startDate"),
            "endDate": discount.get("endDate"),
        } if discount else {},
        "batch": bool(batch),
    }


class Catalog:
    """Cached product search against one store."""

    def __init__(self, creds, refresh: bool = False):
        self.creds = creds
        self.refresh = refresh
        self.calls = 0
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _cache_path(self, query: str) -> pathlib.Path:
        key = f"v{CACHE_VERSION}|{self.creds.store_id}|{query}".encode("utf-8")
        return CACHE_DIR / f"{hashlib.sha1(key).hexdigest()}.json"

    def search(self, query: str) -> list[dict]:
        import httpx

        query = query.strip()
        if not query:
            return []
        path = self._cache_path(query)
        if not self.refresh and path.exists():
            with contextlib.suppress(Exception):
                cached = json.loads(path.read_text(encoding="utf-8"))
                fetched = datetime.fromisoformat(cached["fetched"])
                age_days = (datetime.now(timezone.utc) - fetched).days
                if age_days < CACHE_TTL_DAYS:
                    return cached["products"]

        self.calls += 1
        try:
            response = httpx.post(
                f"https://www.k-ruoka.fi/kr-api/v2/product-search/{query}",
                params={
                    "offset": 0, "language": "fi", "storeId": self.creds.store_id,
                    "limit": SEARCH_LIMIT, "discountFilter": "false",
                    "isTrOffer": "false",
                },
                headers=self.creds.headers(), cookies=self.creds.cookies(),
                timeout=httpx.Timeout(5.0, read=30.0), follow_redirects=False,
            )
        except httpx.HTTPError:
            raise EstimateError("Could not reach K-Ruoka. Check your connection.") from None

        if response.headers.get("cf-mitigated") == "challenge":
            raise EstimateError(
                "Cloudflare challenged the request. Run `kruoka-auth.py capture` again."
            )
        if response.status_code in (401, 403):
            raise EstimateError(
                "K-Ruoka rejected the session. Run `kruoka-auth.py capture` again."
            )
        if response.status_code == 409:
            raise EstimateError(
                "The stored build number is stale. Run `kruoka-auth.py capture` again."
            )
        if not response.is_success:
            raise EstimateError(f"K-Ruoka returned HTTP {response.status_code}.")

        products = [trim(row["product"]) for row in response.json().get("result", [])
                    if isinstance(row, dict) and isinstance(row.get("product"), dict)]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"fetched": datetime.now(timezone.utc).isoformat(), "query": query,
             "products": products}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return products


# --- pricing ---------------------------------------------------------------

UNIT_FOR_KIND = {MASS: "kg", VOLUME: "l"}


def effective_price(product: dict, now: datetime) -> tuple[float | None, float | None, str]:
    """Return (per-package price, comparison unit price, offer label)."""
    price, unit_value, label = product.get("price"), product.get("unit_value"), ""
    discount = product.get("discount") or {}
    if discount and discount.get("type") in ("PLUSSA", "STANDARD") and _campaign_active(discount, now):
        if isinstance(discount.get("price"), (int, float)):
            price = discount["price"]
            unit_value = discount.get("unit_value") or unit_value
            label = discount["type"]
    return (
        price if isinstance(price, (int, float)) else None,
        unit_value if isinstance(unit_value, (int, float)) else None,
        label,
    )


def fold(text: str) -> str:
    """Lowercase and strip accents, so 'Kesäkurpitsa' matches 'kesakurpitsa'."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def relevant(products: list[dict], query: str) -> list[dict]:
    """Keep only products whose name actually contains every query term.

    Without this, search behaves far too loosely: 'kesakurpitsa' returns
    plums, 'naudan jauheliha' returns dog bones, and picking the cheapest
    result confidently prices the wrong thing.

    Long tokens match as substrings, because Finnish compounds are how the
    language works ('kananmunat' should match 'kananmuna'). Short tokens must
    match a whole word, or 'voi' (butter) would match 'voileipakeksi'
    (sandwich biscuit).
    """
    tokens = [t for t in re.split(r"[^\w]+", fold(query)) if t]
    if not tokens:
        return products

    def matches(name: str) -> bool:
        folded = fold(name)
        words = set(re.split(r"[^\w]+", folded))
        return all(t in folded if len(t) >= 4 else t in words for t in tokens)

    return [p for p in products if matches(p.get("name", ""))]


def match_rank(name: str, query: str) -> int:
    """0 = the product is the thing; 2 = it merely mentions it.

    'Kesakurpitsa ulkomainen' is a courgette. 'Piltti bataatti, palsternakka
    ja kesakurpitsa' is baby food that happens to contain one, and it is
    cheaper, so price alone picks the baby food.

    A standalone word beats a prefix, because Finnish compounds built on the
    term are usually a different product: 'maitokolmio' is a chocolate drink,
    'munakoisopyree' is puree, 'salaattisikuri' is chicory.
    """
    folded, wanted = fold(name), fold(query)
    if wanted in set(re.split(r"[^\w]+", folded)):
        return 0
    if folded.startswith(wanted):
        return 1
    return 2


def choose(products: list[dict], spec: Spec, kind: str, now: datetime) -> tuple[dict | None, bool]:
    """Pick the cheapest sensible candidate. Returns (product, brand_honoured)."""
    usable = [p for p in products if p.get("web") and p.get("name")]
    usable = [p for p in usable
              if not any(b in p.get("category", "") for b in BLOCKED_CATEGORIES)]
    if spec.category:
        usable = [p for p in usable if spec.category in p.get("category", "")]
    usable = relevant(usable, spec.query)
    if spec.exclude:
        usable = [p for p in usable
                  if not any(term in fold(p["name"]) for term in spec.exclude)]

    wanted_unit = UNIT_FOR_KIND.get(kind)
    pool: list[dict] = []
    if wanted_unit:
        # Comparing EUR/kg against EUR/l would rank lemon squash against lemons.
        pool = [p for p in usable if p.get("unit") == wanted_unit
                and isinstance(p.get("unit_value"), (int, float))]
    if pool:
        price_of = lambda p: effective_price(p, now)[1] or float("inf")
    else:
        # Nothing sold by the unit we wanted (stock cubes against litres of
        # stock, say). Price a package rather than giving up entirely.
        pool = [p for p in usable if isinstance(p.get("price"), (int, float))]
        price_of = lambda p: effective_price(p, now)[0] or float("inf")

    # Match quality first, price second: the cheapest thing merely *mentioning*
    # courgette is baby food.
    key = lambda p: (match_rank(p.get("name", ""), spec.query), price_of(p))

    if spec.brand:
        branded = [p for p in pool if p.get("brand", "").lower() == spec.brand.lower()]
        if branded:
            return min(branded, key=key), True
    if not pool:
        return None, not spec.brand
    return min(pool, key=key), not spec.brand


def price_item(item: Item, config: Config, catalog: Catalog, now: datetime,
               include_pantry: bool) -> Pick:
    kind = config.classify(item.qualified, item.name)
    if kind == "skip":
        return Pick(tag="unpriced", reason="not a purchase")
    if kind == "pantry" and not include_pantry:
        return Pick(tag="pantry", reason="assumed in the cupboard")
    if item.name.rstrip().endswith("?"):
        return Pick(tag="unpriced", reason="not a specific product")

    spec = config.resolve(item.qualified, item.name)
    mapped = spec is not None
    if spec is None:
        # Unmapped names are searched verbatim. Items already written in
        # Finnish resolve fine; English ones usually will not, hence `guessed`.
        spec = Spec(query=item.qualified, source="verbatim")

    products = catalog.search(spec.query)
    if spec.ean:
        exact = next((p for p in products if p.get("ean") == spec.ean), None)
        if exact:
            price, unit_value, label = effective_price(exact, now)
            return _build(item, exact, price, unit_value, label, "exact", "")
        return Pick(tag="unpriced", reason=f"pinned EAN {spec.ean} not found")

    if not products:
        return Pick(tag="unpriced", reason=f"no results for '{spec.query}'")

    measure = item.measure
    chosen, brand_ok = choose(products, spec, measure.kind, now)
    if chosen is None:
        return Pick(
            tag="unpriced",
            reason=f"no product name matches '{spec.query}' -- try a different term",
        )

    price, unit_value, label = effective_price(chosen, now)
    reason = ""
    if spec.brand and not brand_ok:
        tag, reason = "guessed", f"{spec.brand} not found"
    elif not mapped:
        tag, reason = "guessed", "unmapped, searched by name"
    elif spec.brand:
        tag = "brand"
    else:
        tag = "generic"
    return _build(item, chosen, price, unit_value, label, tag, reason)


def _build(item: Item, product: dict, price: float | None, unit_value: float | None,
           label: str, tag: str, reason: str) -> Pick:
    measure = item.measure
    wanted = UNIT_FOR_KIND.get(measure.kind)
    # Only use the comparison price when it is actually in the unit we want;
    # otherwise a EUR/l figure would silently be treated as EUR/kg.
    if wanted and product.get("unit") == wanted and unit_value is not None:
        cost = unit_value * measure.amount / 1000.0
        shown = f"{unit_value:.2f}/{wanted}"
    elif price is not None:
        # Counts, negligible seasoning amounts and unquantified lines all come
        # down to "one package", which is what you would actually put in a bag.
        quantity = measure.amount if measure.kind == COUNT else 1.0
        cost = price * quantity
        shown = f"{price:.2f}/pkg"
    else:
        return Pick(tag="unpriced", reason="no usable price")

    return Pick(
        tag=tag, cost=cost, product=product.get("name", ""),
        brand=product.get("brand", ""), unit_price=shown, reason=reason,
        approximate=bool(product.get("approximate")), on_offer=label,
    )


# --- reporting -------------------------------------------------------------

def describe(measure: Measure) -> str:
    if measure.assumed:
        return "1 pkg*"
    if measure.kind == MASS:
        return f"{measure.amount:g} g" if measure.amount < 1000 else f"{measure.amount / 1000:g} kg"
    if measure.kind == VOLUME:
        return f"{measure.amount:g} ml" if measure.amount < 1000 else f"{measure.amount / 1000:g} l"
    if measure.kind == NEGLIGIBLE:
        return "seasoning"
    return f"{measure.amount:g}x"


def cmd_estimate(args) -> int:
    path = pathlib.Path(args.list)
    if not path.exists():
        candidate = SHOPPING_LISTS / args.list
        if not candidate.exists():
            raise EstimateError(f"No such shopping list: {args.list}")
        path = candidate

    config = load_config(PRODUCTS_CONFIG, AISLE_CONFIG)
    items = parse_list(path)
    if not items:
        raise EstimateError(f"No items found in {path}. Is it a shopping list?")

    auth = load_auth()
    creds = auth.load()
    if creds.missing_required():
        raise EstimateError(
            "No K-Ruoka credentials. Run "
            ".claude/skills/kruoka-auth/kruoka-auth.py capture"
        )
    catalog = Catalog(creds, refresh=args.refresh)
    now = datetime.now(timezone.utc)

    results = [(item, price_item(item, config, catalog, now, args.include_pantry))
               for item in items]

    confident = sum(p.cost for _, p in results if p.tag in ("exact", "brand", "generic"))
    guessed = sum(p.cost for _, p in results if p.tag == "guessed")
    pantry = [(i, p) for i, p in results if p.tag == "pantry"]
    unpriced = [(i, p) for i, p in results if p.tag == "unpriced"]

    store = f"{creds.store_id}" + (f" ({creds.store_name})" if creds.store_name else "")
    print(f"# {path.name}\n")
    print(f"Store: {store}   Items: {len(items)}\n")

    width = max((len(i.name) for i, _ in results), default=10)
    width = min(max(width, 12), 34)
    current_aisle = None
    for item, pick in results:
        if pick.tag in ("pantry", "unpriced"):
            continue
        if item.aisle != current_aisle:
            current_aisle = item.aisle
            print(f"\n## {current_aisle}")
        flags = []
        if pick.on_offer:
            flags.append(pick.on_offer)
        if pick.approximate:
            flags.append("weighed")
        if pick.reason:
            flags.append(pick.reason)
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        print(f"  {pick.cost:>7.2f}  {item.name[:width]:<{width}} "
              f"{describe(item.measure):>10}  {pick.unit_price:>11}  "
              f"{pick.product[:40]}{suffix}")

    if pantry:
        print(f"\n## assumed in the cupboard (not counted)")
        for item, _ in pantry:
            print(f"     0.00  {item.name}")
    if unpriced:
        print(f"\n## not priced")
        for item, pick in unpriced:
            print(f"           {item.name[:width]:<{width}}  {pick.reason}")

    print("\n" + "-" * 60)
    print(f"  Confident subtotal   {confident:>8.2f} EUR")
    if guessed:
        print(f"  Guessed subtotal     {guessed:>8.2f} EUR   (flagged above)")
    print(f"  ESTIMATED TOTAL      {confident + guessed:>8.2f} EUR")
    if unpriced:
        print(f"  {len(unpriced)} item(s) not priced -- total is an undercount.")
    if pantry:
        print(f"  {len(pantry)} pantry item(s) at 0.00; use --include-pantry to price them.")
    print(f"\n  * = no quantity in the list; assumed one package.")
    print(f"  {catalog.calls} API call(s); the rest came from cache.")

    if args.json:
        payload = {
            "list": path.name, "store": creds.store_id,
            "confident": round(confident, 2), "guessed": round(guessed, 2),
            "total": round(confident + guessed, 2),
            "items": [{
                "name": i.name, "aisle": i.aisle, "quantity": describe(i.measure),
                "tag": p.tag, "cost": round(p.cost, 2), "product": p.product,
                "brand": p.brand, "unit_price": p.unit_price, "reason": p.reason,
            } for i, p in results],
        }
        pathlib.Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


def cmd_lookup(args) -> int:
    auth = load_auth()
    creds = auth.load()
    if creds.missing_required():
        raise EstimateError("No K-Ruoka credentials. Run kruoka-auth.py capture.")
    catalog = Catalog(creds, refresh=args.refresh)
    now = datetime.now(timezone.utc)
    products = catalog.search(args.query)
    shown = [p for p in products if p.get("web")]
    if args.brand:
        shown = [p for p in shown if p.get("brand", "").lower() == args.brand.lower()]
    if not shown:
        print(f"No matches for '{args.query}'"
              + (f" from brand {args.brand}" if args.brand else ""))
        return 1
    shown.sort(key=lambda p: effective_price(p, now)[1] or float("inf"))
    print(f"{len(shown)} match(es) for '{args.query}'"
          + (f", brand {args.brand}" if args.brand else "") + ":\n")
    for product in shown[:args.limit]:
        price, unit_value, label = effective_price(product, now)
        unit_text = f"{unit_value:.2f}/{product.get('unit', '?')}" if unit_value else "-"
        print(f"  {price if price is not None else '?':>6} EUR  {unit_text:>12}  "
              f"{product.get('brand', '-'):<14} {product.get('name', '')[:46]}"
              + (f"  [{label}]" if label else ""))
    if len(shown) > args.limit:
        print(f"  ... {len(shown) - args.limit} more")
    return 0


def cmd_coverage(args) -> int:
    import collections

    config = load_config(PRODUCTS_CONFIG, AISLE_CONFIG)
    counts: collections.Counter = collections.Counter()
    files = sorted(SHOPPING_LISTS.glob("*.md"))
    parsed = 0
    for path in files:
        for item in parse_list(path):
            parsed += 1
            if config.classify(item.qualified, item.name) != "price":
                continue
            if config.resolve(item.qualified, item.name) is None:
                counts[normalise(item.qualified)] += 1

    mapped_lines = parsed - sum(counts.values())
    print(f"{len(files)} lists, {parsed} item lines parsed")
    print(f"{mapped_lines} lines mapped or excluded "
          f"({mapped_lines / parsed * 100:.0f}%), {len(counts)} distinct names unmapped\n")
    print(f"Top {args.limit} unmapped ingredients by frequency:")
    for name, count in counts.most_common(args.limit):
        print(f"  {count:>3}x  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate the cost of a shopping list at K-Ruoka.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_est = sub.add_parser("estimate", help="Price a shopping list")
    p_est.add_argument("list", help="Path to a shopping list markdown file")
    p_est.add_argument("--include-pantry", action="store_true",
                       help="Price pantry staples instead of assuming them")
    p_est.add_argument("--refresh", action="store_true", help="Bypass the price cache")
    p_est.add_argument("--json", help="Also write a machine-readable report here")
    p_est.set_defaults(func=cmd_estimate)

    p_look = sub.add_parser("lookup", help="Search K-Ruoka, to help write the config")
    p_look.add_argument("query")
    p_look.add_argument("--brand", help="Only show this brand")
    p_look.add_argument("--limit", type=int, default=15)
    p_look.add_argument("--refresh", action="store_true")
    p_look.set_defaults(func=cmd_lookup)

    p_cov = sub.add_parser("coverage", help="Rank unmapped ingredients across all lists")
    p_cov.add_argument("--limit", type=int, default=40)
    p_cov.set_defaults(func=cmd_coverage)

    # `estimate-cost.py <file>` should work without typing the subcommand.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in {"estimate", "lookup", "coverage", "-h", "--help"}:
        argv.insert(0, "estimate")
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except EstimateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
