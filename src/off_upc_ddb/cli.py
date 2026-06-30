"""CLI entry-point for ``off_upc_ddb`` — ``query`` and ``serve`` subcommands."""

import json
import sys

import click

from off_upc_ddb.lookup import ProductLookup, conform_upc
from off_upc_ddb.server import create_app


# Shared decorator so both subcommands inherit the --db-path option.
def db_path_option(func):
    return click.option(
        "--db-path",
        "-d",
        default="./food.parquet",
        envvar="OFF_UPC_DDB_PATH",
        show_default=True,
        help="Path to the food.parquet file.",
    )(func)


@click.group()
def main():
    """UPC product lookup from the OpenFoodFacts Parquet database."""


@main.command()
@db_path_option
@click.argument("upc")
def query(db_path: str, upc: str):
    """Look up a single UPC and print the result as JSON to stdout.

    UPC is auto-conformed (whitespace stripped, numeric codes zero-padded
    to 13 digits) before querying.

    Exit code 0 → found, 1 → not found, 2 → error.
    """
    conformed = conform_upc(upc)
    try:
        lookup = ProductLookup(db_path)
        result = lookup.lookup(conformed)
    except FileNotFoundError as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
        raise SystemExit(2) from exc
    except Exception as exc:
        click.echo(
            json.dumps({"error": f"query failed: {exc}", "code": conformed}),
            err=True,
        )
        raise SystemExit(2) from exc

    if result is None:
        click.echo(
            json.dumps({"error": "UPC not found", "code": conformed}),
            err=True,
        )
        raise SystemExit(1)

    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@main.command()
@db_path_option
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind address.")
@click.option("--port", default=5000, type=int, show_default=True, help="Bind port.")
def serve(db_path: str, host: str, port: int):
    """Start the HTTP lookup server."""
    try:
        app = create_app(db_path)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(2) from exc

    lookup: ProductLookup = app.config["lookup"]
    click.echo(
        f"Serving UPC lookups on http://{host}:{port}\n"
        f"  Parquet : {lookup.db_path}\n"
        f"  Products: {lookup.row_count():,}",
    )
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
