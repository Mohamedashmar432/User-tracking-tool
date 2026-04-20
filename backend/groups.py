"""
groups.py — EmployeeGroups table: admin-created employee groups.

Table: EmployeeGroups
    PartitionKey : "groups"
    RowKey       : group_id  (URL-safe slug derived from the display name)
    name         : display name (original casing)
    members      : JSON array string e.g. '["alice", "bob"]'
    created_at   : ISO timestamp
    created_by   : username of the admin who created the group
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from azure.core.exceptions import ResourceNotFoundError

_LOG = logging.getLogger("telemetry.groups")
GROUPS_TABLE = "EmployeeGroups"


def _slugify(name: str) -> str:
    """Produce a stable, Azure-safe RowKey from a display name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-") or "group"


class GroupStorage:
    def __init__(self, service):
        """
        Accepts an existing TableServiceClient so we share the connection
        with TelemetryStorage rather than opening a second one.
        """
        service.create_table_if_not_exists(GROUPS_TABLE)
        self._tbl = service.get_table_client(GROUPS_TABLE)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _decode(self, entity: dict) -> dict:
        return {
            "id":         entity["RowKey"],
            "name":       entity.get("name", entity["RowKey"]),
            "members":    json.loads(entity.get("members", "[]")),
            "created_at": entity.get("created_at", ""),
            "created_by": entity.get("created_by", ""),
        }

    # ── Reads ─────────────────────────────────────────────────────────────────

    def list_groups(self) -> List[dict]:
        try:
            return [
                self._decode(e)
                for e in self._tbl.query_entities("PartitionKey eq 'groups'")
            ]
        except Exception as exc:
            _LOG.error("list_groups: %s", exc)
            return []

    def get_group(self, group_id: str) -> Optional[dict]:
        try:
            return self._decode(self._tbl.get_entity("groups", group_id))
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            _LOG.error("get_group(%s): %s", group_id, exc)
            return None

    # ── Writes ────────────────────────────────────────────────────────────────

    def create_group(self, name: str, created_by: str) -> dict:
        gid = _slugify(name)
        now = datetime.now(timezone.utc).isoformat()
        entity = {
            "PartitionKey": "groups",
            "RowKey":       gid,
            "name":         name.strip(),
            "members":      "[]",
            "created_at":   now,
            "created_by":   created_by,
        }
        self._tbl.upsert_entity(entity)
        return self._decode(entity)

    def delete_group(self, group_id: str) -> bool:
        try:
            self._tbl.delete_entity("groups", group_id)
            return True
        except Exception:
            return False

    def add_member(self, group_id: str, username: str) -> Optional[dict]:
        """
        Add a member to the group.  username must already be the canonical form
        (same case as stored in UserIndex) so that get_raw_events() can resolve it.
        No-op if already a member (case-insensitive check).
        Returns updated group.
        """
        try:
            e       = self._tbl.get_entity("groups", group_id)
            members = json.loads(e.get("members", "[]"))
            uname   = username.strip()   # preserve case — do NOT lower()
            if not any(m.lower() == uname.lower() for m in members):
                members.append(uname)
                e["members"] = json.dumps(members)
                self._tbl.update_entity(e, mode="replace")
            return self._decode(e)
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            _LOG.error("add_member(%s, %s): %s", group_id, username, exc)
            return None

    def remove_member(self, group_id: str, username: str) -> Optional[dict]:
        """Remove a member from the group (case-insensitive match). Returns updated group."""
        try:
            e       = self._tbl.get_entity("groups", group_id)
            members = json.loads(e.get("members", "[]"))
            uname   = username.strip().lower()
            members = [m for m in members if m.lower() != uname]
            e["members"] = json.dumps(members)
            self._tbl.update_entity(e, mode="replace")
            return self._decode(e)
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            _LOG.error("remove_member(%s, %s): %s", group_id, username, exc)
            return None
