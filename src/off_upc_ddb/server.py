"""Flask application factory for the UPC lookup HTTP server."""

from flask import Flask, jsonify

from off_upc_ddb.lookup import ProductLookup, conform_upc


def create_app(db_path: str) -> Flask:
    """Create and configure the Flask application.

    Parameters
    ----------
    db_path:
        Absolute or relative path to the ``food.parquet`` file.
    """
    app = Flask(__name__)

    # Initialise the lookup engine once and attach it to the app for reuse.
    lookup = ProductLookup(db_path)
    app.config["lookup"] = lookup
    app.config["db_path"] = lookup.db_path

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.route("/")
    def health():
        return jsonify(
            {
                "status": "ok",
                "products": lookup.row_count(),
                "db_path": lookup.db_path,
            }
        )

    @app.route("/upc/<code>")
    def upc_lookup(code: str):
        conformed = conform_upc(code)
        try:
            result = lookup.lookup(conformed)
        except Exception:
            return (
                jsonify(
                    {
                        "error": "query failed",
                        "code": conformed,
                    }
                ),
                500,
            )
        if result is None:
            return (
                jsonify(
                    {
                        "error": "UPC not found",
                        "code": conformed,
                    }
                ),
                404,
            )
        return jsonify(result)

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def server_error(_error):
        return jsonify({"error": "internal server error"}), 500

    return app
