import sqlite3
import psycopg2
import os

SQLITE_PATH = "jobs.db"
POSTGRES_URL = "postgresql://neondb_owner:npg_a7M8BSvNyrmR@ep-flat-fire-az7b1wpw.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

def migrate():
    if not os.path.exists(SQLITE_PATH):
        print(f"Local SQLite database '{SQLITE_PATH}' not found! Make sure you are in the workspace folder.")
        return

    print("Connecting to SQLite...")
    lite_conn = sqlite3.connect(SQLITE_PATH)
    lite_conn.row_factory = sqlite3.Row
    lite_cur = lite_conn.cursor()

    print("Connecting to PostgreSQL (Neon)...")
    pg_conn = psycopg2.connect(POSTGRES_URL)
    pg_cur = pg_conn.cursor()

    tables = [
        "users",
        "jobs",
        "contacts",
        "cover_letters",
        "application_notes",
        "application_timeline",
        "received_emails",
        "tailored_resumes"
    ]

    for table in tables:
        print(f"Migrating table '{table}'...")
        # Get data from SQLite
        lite_cur.execute(f"SELECT * FROM {table}")
        rows = lite_cur.fetchall()
        if not rows:
            print(f"  No rows in SQLite table '{table}'. Skipping.")
            continue

        # Get column names
        cols = rows[0].keys()
        
        # Clear existing rows in target table to avoid unique constraint violations
        print(f"  Clearing target table '{table}' on PostgreSQL...")
        pg_cur.execute(f"TRUNCATE TABLE {table} CASCADE")

        # Construct INSERT statement
        col_list = ", ".join(cols)
        placeholder_list = ", ".join(["%s"] * len(cols))
        insert_query = f"INSERT INTO {table} ({col_list}) VALUES ({placeholder_list})"

        # Insert rows
        for row in rows:
            pg_cur.execute(insert_query, [row[c] for c in cols])
        print(f"  Successfully copied {len(rows)} rows into PostgreSQL '{table}'.")

    pg_conn.commit()
    print("\nMigration complete! All local database jobs and accounts are now on your Neon database!")

    lite_conn.close()
    pg_conn.close()

if __name__ == "__main__":
    migrate()
