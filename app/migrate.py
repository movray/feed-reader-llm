"""
Creates or updates the database schema (schema.sql) and exits.

Runs before the app starts: as init container in Kubernetes, as one-shot
service in docker-compose. Waits until the database accepts connections.
"""

import os
import sys
import time
from pathlib import Path

import psycopg2

SCHEMA = Path(__file__).with_name("schema.sql")
WAIT_SECONDS = int(os.environ.get("DB_WAIT_SECONDS", "300"))
# Any constant: serializes migrations of several starting pods.
LOCK_ID = 0x66656564  # "feed"


def connect():
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            return psycopg2.connect(
                host=os.environ.get("DB_HOST"),
                dbname=os.environ.get("DB_NAME"),
                user=os.environ.get("DB_USER"),
                password=os.environ.get("DB_PASSWORD"),
                port=os.environ.get("DB_PORT"),
                connect_timeout=5,
            )
        except psycopg2.OperationalError as e:
            if time.monotonic() > deadline:
                raise
            print(f"Waiting for database: {str(e).strip()}", flush=True)
            time.sleep(2)


def main():
    params = {
        "llm_url": os.environ.get("LLM_BASE_URL", ""),
        "llm_model": os.environ.get("LLM_MODEL", ""),
    }
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            cur.execute(SCHEMA.read_text(encoding="utf-8"), params)
    finally:
        conn.close()
    print("Schema up to date", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
