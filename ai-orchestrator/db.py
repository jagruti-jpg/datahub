"""MySQL connection factory.

Separate from `main.py` so that modules below the web layer can open a connection
without importing it. `main` imports `agent`, which imports `pii_tagger`, so anything
`pii_tagger` reaches would hit a `main` that has only executed as far as its own import
block — and `get_db_connection` is defined hundreds of lines later. This module imports
nothing from the application, so nothing can cycle back through it.
"""
from __future__ import annotations

import os

import mysql.connector

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "datahub")
DB_PASSWORD = os.getenv("DB_PASSWORD", "datahub")
DB_NAME = "datahub"


def get_db_connection(include_db: bool = True):
    """Establishes a connection to MySQL."""
    config = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
    }
    if include_db:
        config["database"] = DB_NAME
    return mysql.connector.connect(**config)
