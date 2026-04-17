"""
users.py — UserAuth table: dashboard login and user management.

Table: UserAuth
    PartitionKey : "users"
    RowKey       : username (lowercase)
    password_hash: bcrypt hash
    role         : "admin" | "viewer"
    created_at   : ISO timestamp

On first startup with an empty table, a default admin is created:
    username : admin
    password : value of ADMIN_PASSWORD env var, or "changeme123"
Log output tells you the password — change it immediately via the dashboard.
"""

import os
import logging
from datetime import datetime, timezone
from typing import List, Optional

import bcrypt as _bcrypt
from azure.core.exceptions import ResourceNotFoundError

_LOG = logging.getLogger("telemetry.users")
USER_AUTH_TABLE = "UserAuth"


def _hash(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify(password: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


class UserStorage:
    def __init__(self, service):
        """
        Accepts an existing TableServiceClient so we share the connection
        with TelemetryStorage rather than opening a second one.
        """
        service.create_table_if_not_exists(USER_AUTH_TABLE)
        self._tbl = service.get_table_client(USER_AUTH_TABLE)
        self._ensure_default_admin()

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _ensure_default_admin(self) -> None:
        """Create a default admin account if the table is empty."""
        try:
            existing = list(
                self._tbl.query_entities(
                    "PartitionKey eq 'users'",
                    select=["RowKey"],
                    results_per_page=1,
                )
            )
            if not existing:
                pw = os.getenv("ADMIN_PASSWORD", "admin@123")
                self.create_user("admin", pw, "admin")
                _LOG.warning(
                    "First start: created default admin (username=admin, password=%s). "
                    "Change it immediately via the dashboard -> Users.",
                    pw,
                )
        except Exception as e:
            _LOG.error("ensure_default_admin failed: %s", e)

    # ── Writes ────────────────────────────────────────────────────────────────

    def create_user(self, username: str, password: str, role: str = "viewer") -> dict:
        uname = username.lower().strip()
        now   = datetime.now(timezone.utc).isoformat()
        self._tbl.upsert_entity({
            "PartitionKey":  "users",
            "RowKey":        uname,
            "password_hash": _hash(password),
            "role":          role,
            "created_at":    now,
        })
        return {"username": uname, "role": role, "created_at": now}

    def update_password(self, username: str, new_password: str) -> bool:
        try:
            e = self._tbl.get_entity("users", username.lower())
            e["password_hash"] = _hash(new_password)
            self._tbl.update_entity(e, mode="replace")
            return True
        except Exception as exc:
            _LOG.error("update_password(%s): %s", username, exc)
            return False

    def update_role(self, username: str, role: str) -> bool:
        try:
            e = self._tbl.get_entity("users", username.lower())
            e["role"] = role
            self._tbl.update_entity(e, mode="replace")
            return True
        except Exception as exc:
            _LOG.error("update_role(%s): %s", username, exc)
            return False

    def delete_user(self, username: str) -> bool:
        try:
            self._tbl.delete_entity("users", username.lower())
            return True
        except Exception:
            return False

    # ── Reads ─────────────────────────────────────────────────────────────────

    def verify_password(self, username: str, password: str) -> Optional[dict]:
        """Returns {username, role} on success, None on bad credentials."""
        try:
            e = self._tbl.get_entity("users", username.lower().strip())
            if _verify(password, e["password_hash"]):
                return {"username": e["RowKey"], "role": e.get("role", "viewer")}
        except ResourceNotFoundError:
            pass
        except Exception as exc:
            _LOG.error("verify_password(%s): %s", username, exc)
        return None

    def list_users(self) -> List[dict]:
        try:
            return [
                {
                    "username":   e["RowKey"],
                    "role":       e.get("role", "viewer"),
                    "created_at": e.get("created_at", ""),
                }
                for e in self._tbl.query_entities("PartitionKey eq 'users'")
            ]
        except Exception as exc:
            _LOG.error("list_users: %s", exc)
            return []
