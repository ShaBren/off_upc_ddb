# Design — `off_upc_ddb`

## Overview

`off_upc_ddb` is a Python package that provides UPC product lookup from the
[OpenFoodFacts](https://world.openfoodfacts.org/) database.  It exposes the
same core lookup logic through three interfaces: a CLI, an HTTP server, and a
download/update manager.  All share a single `ProductLookup` engine backed by
DuckDB reading a Parquet file directly — no import step, no external database
process.

```
┌─────────────────────────────────────────────────────────┐
│                       User / Caller                      │
├──────────────┬─────────────────────┬────────────────────┤
│   uvx query  │    uvx serve        │   uvx fetch        │
│   (stdio)    │   (Flask HTTP)      │  (download mgr)    │
└──────┬───────┴──────────┬──────────┴──────────┬─────────┘
       │                  │                     │
       ▼                  ▼                     ▼
  ┌─────────┐      ┌───────────┐       ┌──────────────┐
  │ cli.py   │      │ server.py │       │  fetch.py     │
  │ (Click)  │      │ (Flask)   │       │ (hf_hub)      │
  └────┬─────┘      └─────┬─────┘       └──────────────┘
       │                  │
       └────────┬─────────┘
                │
                ▼
       ┌───────────────┐
       │  lookup.py     │
       │ ProductLookup  │
       │ conform_upc()  │
       └───────┬───────┘
               │
               ▼
       ┌───────────────┐
       │    DuckDB      │  ← reads food.parquet directly
       │ (in-memory)    │     (7.6 GB, ~4.6M rows)
       └───────────────┘
```

## Project structure

```
off_upc_ddb/
├── pyproject.toml            # uv-managed project, deps, entry points
├── LICENSE                   # MIT
├── README.md
├── DESIGN.md                 # This file
├── food.parquet              # Data file (gitignored, ~7.6 GB)
├── food.parquet.manifest.json # Sidecar manifest (gitignored)
└── src/
    └── off_upc_ddb/
        ├── __init__.py        # Package init, __version__
        ├── __main__.py        # python -m off_upc_ddb
        ├── cli.py             # Click CLI (query, serve, fetch)
        ├── lookup.py          # ProductLookup + conform_upc()
        ├── server.py          # Flask app factory
        └── fetch.py           # Hugging Face download manager
```

## Module details

### `lookup.py` — core engine

The heart of the application.  Contains two public symbols:

**`ProductLookup(db_path: str)`**

| Method | Returns | Notes |
|--------|---------|-------|
| `lookup(upc: str)` | `dict \| None` | Flattened product dict or `None` |
| `row_count()` | `int` | Cached at init; used by health endpoint |
| `db_path` (property) | `str` | Resolved absolute path |

Internally:
1. Opens a DuckDB in-memory connection at `__init__` time.
2. Queries the Parquet file via `read_parquet(?) WHERE code = ?` with a
   curated column list (~24 columns).
3. `_flatten()` transforms the raw DuckDB row into a JSON-safe dict:
   - Multilingual `list<struct<lang, text>>` → extract `"main"` language
     string.
   - Nutriment `list<struct<name, …>>` → flat dict keyed by nutrient name.
   - `list<string>` tags → JSON arrays.
   - `ingredients` JSON string → parsed dict.
   - Scalars (`int`, `float`, `str`, `bool`) → passthrough with numpy
     coercion.

DuckDB was chosen over alternatives (PyArrow, Polars, SQLite) because:
- It reads Parquet directly with zero preprocessing.
- Columnar scan with filter pushdown on `code` makes point lookups fast
  without loading the full dataset.
- The in-memory connection is thread-safe for concurrent reads by Flask's
  default threaded server.

**`conform_upc(raw: str) -> str`**

Normalises user-supplied barcodes before querying:
1. Strips whitespace.
2. If purely numeric and < 13 digits → left-pads with zeros (UPC-A → EAN-13).
3. Non-numeric codes (OFF internal identifiers starting with `200`, etc.)
   pass through unchanged.

This is best-effort — the database query is the final arbiter.

### `server.py` — HTTP API

**`create_app(db_path: str) -> Flask`**

Factory function that returns a configured Flask application:

| Route | Method | Response |
|-------|--------|----------|
| `/` | `GET` | Health check — `{"status":"ok","products":N,"db_path":"…"}` |
| `/upc/<code>` | `GET` | Product JSON (200) or error JSON (404) |

Design decisions:
- **Single `ProductLookup` instance** per app, stored in `app.config`.
  DuckDB's in-memory connections are thread-safe for reads, and the Parquet
  file is read-only.
- **`conform_upc()` applied before every lookup** — the server normalises
  incoming UPCs identically to the CLI.
- **Flask development server** — sufficient for the stated use case (simple
  internal API).  For production, wrap with `gunicorn` or similar.
- **Flask over FastAPI** — deliberate choice for simplicity.  Synchronous,
  zero async overhead, minimal dependencies.

### `cli.py` — command-line interface

Built with [Click](https://click.palletsprojects.com/).  Three subcommands:

**`query <UPC>`** — stdin/stdout lookup.

Applies `conform_upc()`, queries via `ProductLookup`, prints JSON to stdout.
Exit codes follow Unix conventions: 0 = found, 1 = not found, 2 = error.

**`serve`** — start HTTP server.

Accepts `--host` (default `0.0.0.0`) and `--port` (default `5000`).  Calls
`create_app()` and `app.run()`.

**`fetch`** — download database from Hugging Face.

Delegates to `fetch.download_db()`.  Accepts `--force` to bypass revision
checks.

All three share a `--db-path` / `-d` option (default `./food.parquet`) and
the `OFF_UPC_DDB_PATH` environment variable.  The environment variable is
checked by Click's `envvar` parameter, so it serves as a fallback when
`--db-path` is not given.

### `fetch.py` — download manager

Handles acquiring and updating the `food.parquet` file from the
[`openfoodfacts/product-database`](https://huggingface.co/datasets/openfoodfacts/product-database)
dataset on Hugging Face.

**Design principles:**
- **Never waste bandwidth.**  Remote metadata (dataset revision) is fetched
  in a single lightweight API call before any download decision.
- **Trust the library.**  `huggingface_hub` handles caching, resume, LFS
  integrity verification, and progress display.  No manual hashing.
- **Atomic writes.**  The library downloads to a temporary location and
  moves on success; a failed download never clobbers a good file.

**`download_db(target_path, *, force=False) -> bool`**

Decision flow:

```
target.is_file()?
├── yes, no manifest, !force → register existing file, return True
├── yes, manifest, revision match, !force → skip, return False
├── yes, manifest, revision differs, !force → download, return True
├── yes, force → download, return True
└── no → download, return True
```

**`status(target_path) -> dict`**

Returns current state including remote revision, update availability, and
local manifest info.  Makes only the lightweight API call — no download.

**Manifest format** (`food.parquet.manifest.json`):

```json
{
  "file": "food.parquet",
  "size": 7618033819,
  "downloaded_at": "2026-06-30T14:43:17Z",
  "revision": "792ca83e28a61b4d47a6a39c690ff2f369219f18"
}
```

**Disk-space guard:** Refuses to download if available space on the target
volume is less than 1.2× the expected file size (~9 GB).

## Data flow

### Query (CLI or HTTP)

```
User UPC
  │
  ▼
conform_upc()          ── strip, zero-pad numeric
  │
  ▼
ProductLookup.lookup()
  │
  ├─ DuckDB: SELECT … FROM read_parquet(?) WHERE code = ?
  │
  ├─ fetchone() → raw tuple
  │
  └─ _flatten()
       ├─ _extract_multilingual()  → str | None
       ├─ _flatten_nutriments()    → dict
       ├─ _parse_ingredients()     → list | None
       └─ _coerce_scalar()         → int | float | str | None
  │
  ▼
JSON response
```

### Fetch

```
CLI: off_upc_ddb fetch [--force]
  │
  ▼
_get_remote_revision()  ── HF API (1 KB)
  │
  ▼
Manifest check
  ├─ up-to-date  → skip, exit 0
  └─ stale/missing
       │
       ▼
     Disk-space guard
       │
       ▼
     hf_hub_download()   ── cache check → resume → LFS verify
       │
       ▼
     _write_manifest()   ── record revision + timestamp
```

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `duckdb` | ≥ 1.2 | Parquet query engine |
| `flask` | ≥ 3.1 | HTTP server |
| `click` | ≥ 8.1 | CLI framework |
| `huggingface_hub` | ≥ 0.25 | Dataset download + caching |

No external database server, no preprocessing step.

## Trade-offs & rationale

### DuckDB direct reads vs. database import

**Chosen:** Query the Parquet file directly on each request.

| Approach | Pros | Cons |
|----------|------|------|
| Direct Parquet reads | Zero setup; no duplicate storage | Slightly higher per-query latency |
| Import to DuckDB persistent DB | Index on `code` for sub-ms lookups | Duplicates 7.6 GB; import step |
| Import to SQLite | Ubiquitous; indexed | Same duplication; conversion overhead |

For an internal API, DuckDB's columnar Parquet scan on a single string column
is fast enough (~100-500 ms per point lookup).  If latency becomes a concern,
switching to a persistent DuckDB database with an index on `code` is a
one-line change in `lookup.py`.

### Flask vs. FastAPI

**Chosen:** Flask.

FastAPI would add `starlette`, `pydantic`, and async complexity for no benefit
in a read-only, synchronous lookup service.  Flask is simpler, lighter, and
has fewer moving parts.

### Revision-based update detection vs. hash-based

**Chosen:** Revision (commit SHA) comparison.

The Hugging Face API exposes the dataset's HEAD commit SHA but not the LFS
file SHA-256.  Revision comparison is sufficient for update detection — if
the commit hash changes, the data may have changed.  `huggingface_hub`
handles actual file integrity internally via LFS pointers.

### src/ layout vs. flat package

**Chosen:** `src/` layout (PEP 517 / PEP 621).

Prevents accidental imports of the package from the project root during
development, and ensures the installed package is tested, not the source
tree.

## Future considerations

- **Persistent DuckDB index** — if per-query latency becomes an issue, add an
  optional `off_upc_ddb index` command that imports the Parquet file into a
  persistent DuckDB database with an index on `code`.
- **Production WSGI server** — document `gunicorn` / `waitress` deployment.
- **Multiple Parquet files** — DuckDB supports glob patterns; could support
  sharded datasets.
- **Partial column retrieval** — allow callers to request specific columns
  via query parameter to reduce response size.
- **Cache layer** — add an in-process LRU cache for frequently-queried UPCs
  to eliminate Parquet scans for hot keys.
