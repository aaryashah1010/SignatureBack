"""
Quick SQL Server connectivity test.
Run from the backend/ directory:

    python test_sqlserver_connection.py

Or with a connection string override:

    SQLSERVER_URL="mssql+pyodbc://user:pass@host/db?driver=ODBC+Driver+17+for+SQL+Server" \
    python test_sqlserver_connection.py
"""

import os
import sys

# ── 1. Resolve connection string ──────────────────────────────────────────────
# Priority: env var > .env file > manual input
url = os.environ.get("SQLSERVER_URL", "").strip()

if not url:
    # Try reading from .env in the same or parent directory
    for env_path in [".env", "../.env"]:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SQLSERVER_URL="):
                        url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        print(f"[INFO] Loaded SQLSERVER_URL from {env_path}")
                        break
            if url:
                break

if not url:
    print("[WARN] SQLSERVER_URL not found in environment or .env file.")
    url = input("Enter SQL Server connection string: ").strip()

if not url:
    print("[ERROR] No connection string provided. Exiting.")
    sys.exit(1)

print(f"\n[INFO] Testing connection to: {url[:80]}{'...' if len(url) > 80 else ''}\n")

# ── 2. Test with sqlalchemy ────────────────────────────────────────────────────
try:
    from sqlalchemy import create_engine, text
except ImportError:
    print("[ERROR] sqlalchemy is not installed. Run: pip install sqlalchemy pyodbc")
    sys.exit(1)

try:
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT @@VERSION AS version, DB_NAME() AS current_db"))
        row = result.fetchone()
        print("[OK] Connection SUCCESSFUL!\n")
        print(f"   Server version : {row.version[:80]}...")
        print(f"   Current DB     : {row.current_db}")

    # ── 3. Check expected tables ──────────────────────────────────────────────
    expected_tables = ["LoginDetail", "CPAUser", "Client", "CAPUserClientMapping", "DocumentMaster"]
    print("\n[INFO] Checking expected integration tables...")
    with engine.connect() as conn:
        for table in expected_tables:
            check = conn.execute(
                text(
                    "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_NAME = :t"
                ),
                {"t": table},
            )
            exists = check.fetchone().cnt > 0
            status = "[EXISTS]" if exists else "[MISSING]"
            print(f"   {status} : {table}")

    print("\n[DONE] Connection test complete.")

except Exception as exc:
    print(f"[FAILED] Connection FAILED!\n\n   Error: {exc}\n")
    print("Common fixes:")
    print("  - Check ODBC driver: 'ODBC Driver 17 for SQL Server' must be installed")
    print("  - Verify server/host is reachable from this machine")
    print("  - Check username/password and database name")
    print("  - If using Windows Auth, use 'Trusted_Connection=yes' instead of user:pass")
    sys.exit(1)
