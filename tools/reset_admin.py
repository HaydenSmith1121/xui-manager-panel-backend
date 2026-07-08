from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from xui_manager.auth import hash_password
from xui_manager.db import Database


def reset_admin(db_path: str | Path, email: str, password: str) -> None:
    db = Database(db_path)
    db.init_schema()
    normalized_email = email.strip().lower()
    now = int(time.time())
    password_hash = hash_password(password)
    with db.session() as conn:
        existing = conn.execute("select id from users where email=?", (normalized_email,)).fetchone()
        if existing:
            conn.execute(
                "update users set password_hash=?, role='admin', status='active', approved_at=? where email=?",
                (password_hash, now, normalized_email),
            )
            return
        conn.execute(
            """
            insert into users(email, password_hash, role, status, token, created_at, approved_at)
            values (?, ?, 'admin', 'active', ?, ?, ?)
            """,
            (normalized_email, password_hash, secrets.token_urlsafe(24), now, now),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset or create the xui-manager-panel admin account.")
    parser.add_argument("--db", default="/opt/xui-manager-panel-data/app.db", help="SQLite database path")
    parser.add_argument("--email", required=True, help="Admin email")
    parser.add_argument("--password", required=True, help="Admin password")
    args = parser.parse_args()
    reset_admin(args.db, args.email, args.password)
    print(f"admin reset: {args.email.strip().lower()}")


if __name__ == "__main__":
    main()
