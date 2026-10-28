
from pathlib import Path
import sqlite3

from flask import Flask, jsonify

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "catalog.db"

app = Flask(__name__)


def check_database():
    """ກວດວ່າຖານຂໍ້ມູນມີຢູ່ ແລະສາມາດອ່ານໄດ້."""
    if not DATABASE.is_file():
        raise FileNotFoundError(
            f"ບໍ່ພົບຖານຂໍ້ມູນ: {DATABASE}"
        )

    connection = sqlite3.connect(
        f"file:{DATABASE}?mode=ro",
        uri=True
    )

    try:
        tables = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        return [row[0] for row in tables]
    finally:
        connection.close()


@app.route("/api/health")
def health():
    try:
        tables = check_database()
        return jsonify({
            "status": "ok",
            "message": "078food backend is running",
            "database": DATABASE.name,
            "tables": tables
        })
    except (OSError, sqlite3.Error) as error:
        return jsonify({
            "status": "error",
            "message": str(error)
        }), 500


if __name__ == "__main__":
    app.run(debug=True)
