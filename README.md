# off_upc_ddb

UPC product lookup from the [OpenFoodFacts](https://world.openfoodfacts.org/)
database, served as a CLI tool and a lightweight HTTP API.

Uses DuckDB to query a Parquet snapshot of the full OpenFoodFacts product
database (~4.6 million products, ~7.6 GB) — no import step, no external
database server.

## Quick start

```bash
# 1. Download the database from Hugging Face (~7.6 GB)
uvx off_upc_ddb fetch

# 2. Look up a product by UPC
uvx off_upc_ddb query 3017620422003

# 3. Or start the HTTP server
uvx off_upc_ddb serve
```

## Installation

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/).

### Run directly with uvx (no install)

```bash
uvx --from git+https://github.com/ShaBren/off_upc_ddb off_upc_ddb fetch
uvx --from git+https://github.com/ShaBren/off_upc_ddb off_upc_ddb query 3017620422003
uvx --from git+https://github.com/ShaBren/off_upc_ddb off_upc_ddb serve
```

### Install from source

```bash
git clone https://github.com/ShaBren/off_upc_ddb.git
cd off_upc_ddb
uv sync
uv run off_upc_ddb fetch
```

## Commands

### `fetch` — download the database

```bash
uvx off_upc_ddb fetch [--force] [-d ./food.parquet]
```

Downloads `food.parquet` from the
[`openfoodfacts/product-database`](https://huggingface.co/datasets/openfoodfacts/product-database)
dataset on Hugging Face (~7.6 GB).

| Behaviour | Trigger |
|-----------|---------|
| **No-op** | Local file exists and remote revision matches stored manifest |
| **Register** | Local file exists but no manifest — registers it without re-downloading |
| **Download** | File missing, revision changed, or `--force` given |

`huggingface_hub` handles caching, resume, LFS integrity verification, and
progress display internally.  A `.manifest.json` sidecar tracks the dataset
revision for future update checks.

The database path can also be set via the `OFF_UPC_DDB_PATH` environment
variable.

### `query` — look up a single UPC

```bash
uvx off_upc_ddb query <UPC> [-d ./food.parquet]
```

Prints product data as JSON to stdout.  The UPC is automatically conformed:

* Surrounding whitespace is stripped.
* Purely-numeric codes shorter than 13 digits are zero-padded on the left
  (handles UPC-A → EAN-13 conversion).

Exit codes: **0** = found, **1** = not found, **2** = error.

```json
{
  "code": "3017620422003",
  "product_name": "Nutella",
  "brands": "Ferrero, Nutella, Yum yum",
  "nutriscore_grade": "e",
  "nova_group": 4,
  "nutriments": {
    "energy-kcal": { "100g": 539.0, "unit": "kcal" },
    "fat": { "100g": 30.9, "unit": "g" }
  },
  "ingredients_text": "Sucre, huile de palme, NOISETTES 13%…",
  "categories_tags": ["en:breakfasts", "en:spreads", …],
  "allergens_tags": ["en:milk", "en:nuts", "en:soybeans"],
  …
}
```

### `serve` — start the HTTP API

```bash
uvx off_upc_ddb serve [--host 0.0.0.0] [--port 5000] [-d ./food.parquet]
```

Starts a Flask development server with two endpoints:

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/` | `{"status":"ok","products":4576155,"db_path":"…"}` |
| `GET` | `/upc/<code>` | Product JSON (200) or `{"error":"UPC not found","code":"…"}` (404) |

UPC conformance is applied automatically on the server too.

## Data format

Queries return a curated subset of ~24 fields from the full OpenFoodFacts
schema.  Nested Parquet types are flattened for JSON compatibility:

| Parquet type | JSON output |
|---|---|
| `list<struct<lang, text>>` | The `"main"` language text, or first available |
| `list<struct<name, value, …>>` (nutriments) | Flat dict keyed by nutrient name |
| `list<string>` (tags) | JSON array of strings |
| JSON string (`ingredients`) | Parsed JSON object |

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OFF_UPC_DDB_PATH` | Default path to `food.parquet` (overridden by `--db-path`) |

## License

MIT — see [LICENSE](LICENSE).

The OpenFoodFacts database is licensed under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
