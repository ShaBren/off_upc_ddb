"""Core lookup logic: DuckDB query + data transformation + UPC conformance."""

import json
import os
import duckdb


# Columns we select from the Parquet file for each product lookup.
CURATED_COLUMNS = [
    "code",
    "product_name",
    "generic_name",
    "brands",
    "brands_tags",
    "categories",
    "categories_tags",
    "quantity",
    "ingredients_text",
    "ingredients",
    "allergens_tags",
    "additives_tags",
    "nutriments",
    "nutriscore_grade",
    "nutriscore_score",
    "nova_group",
    "environmental_score_grade",
    "environmental_score_score",
    "labels_tags",
    "origins",
    "packaging_tags",
    "stores",
    "countries_tags",
    "scans_n",
    "unique_scans_n",
    "link",
]

# Column names whose Parquet type is list<struct<lang, text>>.
MULTILINGUAL_FIELDS = {"product_name", "generic_name", "ingredients_text"}

# Column names whose Parquet type is plain list<string>.
TAG_LIST_FIELDS = {
    "brands_tags",
    "categories_tags",
    "allergens_tags",
    "additives_tags",
    "labels_tags",
    "packaging_tags",
    "countries_tags",
}


def conform_upc(raw: str) -> str:
    """Normalise a user-supplied UPC before querying the database.

    * Strips surrounding whitespace.
    * If the result is purely numeric and shorter than 13 digits it is
      zero-padded on the left to length 13 (UPC‑A → EAN‑13 and similar).
    * Non‑numeric codes (e.g. OFF internal identifiers) are passed through
      unchanged.
    """
    code = raw.strip()
    if code.isdigit() and len(code) < 13:
        code = code.zfill(13)
    return code


class ProductLookup:
    """Look up products in an OpenFoodFacts Parquet file via DuckDB."""

    def __init__(self, db_path: str) -> None:
        if not os.path.isfile(db_path):
            raise FileNotFoundError(f"Parquet file not found: {db_path}")
        self._db_path = os.path.abspath(db_path)
        self._conn = duckdb.connect()
        # Cache the total row count (used by the health-check endpoint).
        self._row_count: int = self._conn.execute(
            f"SELECT count(*) FROM '{self._db_path}'"
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> str:
        return self._db_path

    def row_count(self) -> int:
        """Return the total number of products in the database."""
        return self._row_count

    def lookup(self, upc: str) -> dict | None:
        """Return a flattened dict for *upc*, or *None* if not found."""
        columns = ", ".join(f'"{c}"' for c in CURATED_COLUMNS)
        result = self._conn.execute(
            f'SELECT {columns} FROM read_parquet($1) WHERE code = $2',
            [self._db_path, upc],
        )
        row = result.fetchone()
        if row is None:
            return None
        col_names = [d[0] for d in result.description]
        raw = dict(zip(col_names, row))
        return self._flatten(raw)

    # ------------------------------------------------------------------
    # Flattening helpers
    # ------------------------------------------------------------------

    def _flatten(self, raw: dict) -> dict:
        out: dict = {}

        for field in MULTILINGUAL_FIELDS:
            out[field] = self._extract_multilingual(raw.get(field))

        out["nutriments"] = self._flatten_nutriments(raw.get("nutriments"))

        # ingredients is a JSON *string* column in the Parquet file.
        raw_ingredients = raw.get("ingredients")
        out["ingredients"] = self._parse_ingredients(raw_ingredients)

        for field in TAG_LIST_FIELDS:
            val = raw.get(field)
            out[field] = list(val) if val else []

        # Everything else is a scalar — pass through with light coercion.
        handled = MULTILINGUAL_FIELDS | TAG_LIST_FIELDS | {
            "nutriments",
            "ingredients",
        }
        for field in CURATED_COLUMNS:
            if field not in handled:
                out[field] = self._coerce_scalar(raw.get(field))

        return out

    @staticmethod
    def _extract_multilingual(value) -> str | None:
        """Given a list<struct<lang, text>>, return the ``'main'``-language
        text, or the first entry's text as a fallback."""
        if not value:
            return None
        # DuckDB returns structs as tuples by default: (lang, text).
        for item in value:
            if isinstance(item, dict):
                if item.get("lang") == "main":
                    return item.get("text")
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                if item[0] == "main":
                    return item[1]
        # Fallback: first entry.
        first = value[0]
        if isinstance(first, dict):
            return first.get("text")
        if isinstance(first, (tuple, list)) and len(first) >= 2:
            return first[1]
        return None

    @staticmethod
    def _flatten_nutriments(value) -> dict:
        """Convert list<struct<name, …>> to a flat dict keyed by nutrient name.

        Each nutrient struct contains *name*, a per‑100g value,
        per‑serving value, and a unit.  The field order is taken from the
        reference OpenFoodFacts Parquet export schema.  If DuckDB ever
        returns dicts we use those keys directly.
        """
        if not value:
            return {}

        # Known struct field order for nutriments in the OFF Parquet export.
        # Reference:
        # https://github.com/openfoodfacts/openfoodfacts-exports/blob/main/
        #   openfoodfacts_exports/exports/parquet/food.py
        _NUTRIMENT_TUPLE_FIELDS = (
            "name",
            "value",
            "100g",
            "serving",
            "unit",
            "prepared_value",
            "prepared_100g",
            "prepared_serving",
        )

        out: dict = {}
        for item in value:
            if isinstance(item, dict):
                name = item.pop("name", None)
                if name:
                    out[name] = {k: v for k, v in item.items() if v is not None}
            elif isinstance(item, (tuple, list)):
                name = item[0] if len(item) > 0 else None
                if name:
                    entry: dict = {}
                    for idx in range(1, min(len(item), len(_NUTRIMENT_TUPLE_FIELDS))):
                        val = item[idx]
                        if val is not None:
                            entry[_NUTRIMENT_TUPLE_FIELDS[idx]] = val
                    out[name] = entry
        return out

    @staticmethod
    def _parse_ingredients(raw) -> list | None:
        """Parse the *ingredients* column (a JSON string) if present."""
        if not raw or not isinstance(raw, str):
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _coerce_scalar(value):
        """Convert DuckDB-specific types to plain JSON-safe Python values."""
        if value is None:
            return None
        if isinstance(value, (bool, int, float, str)):
            return value
        # DuckDB may return numpy scalars, Decimal, UUID, datetime, etc.
        # For now, just convert via repr/str as a catch-all.
        if hasattr(value, "item"):  # numpy scalar
            val = value.item()
            if isinstance(val, (bool, int, float, str)):
                return val
        return str(value)
