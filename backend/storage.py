"""
storage.py — Azure Table Storage interface for raw telemetry.

Tables
------
RawTelemetry  — one row per agent event (agent aggregates consecutive events before sending)
UserIndex     — one row per known user (keeps /api/users O(1) instead of full scan)

Schema: RawTelemetry
    PartitionKey : {username}_{YYYY-MM-DD}
    RowKey       : {ISO-timestamp}_{batch-index:04d}   (unique within a batch)
    timestamp    : ISO timestamp string  (stored as field for easy reads)
    app          : foreground process name
    domain       : browser tab title / domain (empty string if not a browser)
    active       : bool  — False when idle >= IDLE_THRESHOLD
    locked       : bool  — True when workstation is locked
    duration     : int   — seconds this event covers (agent-aggregated)
    device       : hostname of the agent machine

Write strategy
--------------
Events are grouped by PartitionKey then submitted via submit_transaction() in
chunks of ≤ 100 (Azure batch limit).  This replaces per-event upsert_entity()
calls, cutting network round-trips from N → ceil(N/100).
"""

import os
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from azure.data.tables import TableServiceClient
from azure.core.exceptions import ResourceExistsError

RAW_TABLE        = "RawTelemetry"
USER_INDEX_TABLE = "UserIndex"


# ── In-memory TTL cache ─────────────────────────────────────────────────────────
# Prevents the three analytics endpoints (summary / apps / timeline) from each
# making an independent Table Storage round-trip for the same user+date data.
#
# TTL policy:
#   today's data    → 2 minutes  (agent batches every 5 min, 2 min is fresh enough)
#   historical data → 30 minutes (past days rarely change)
#   user list       → 5 minutes

class _TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[Any, float]] = {}

    def get(self, key: str) -> Tuple[Optional[Any], bool]:
        """Returns (value, hit). Expired entries are evicted on access."""
        entry = self._store.get(key)
        if entry:
            value, expires = entry
            if time.monotonic() < expires:
                return value, True
            del self._store[key]
        return None, False

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


_cache = _TTLCache()


# ── Batch transaction helper ─────────────────────────────────────────────────────

def _submit_with_retry(table_client, operations: list, max_retries: int = 1) -> None:
    """
    Submit a batch transaction to Azure Table Storage.
    Retries once on transient failure (network blip, throttle).
    Raises on final failure so the HTTP handler can return 500.
    """
    for attempt in range(max_retries + 1):
        try:
            table_client.submit_transaction(operations)
            return
        except Exception:
            if attempt < max_retries:
                time.sleep(0.5)
                continue
            raise


# ── Connection string resolution ────────────────────────────────────────────────

def _resolve_conn_str() -> str:
    """
    Priority order:
    1. AZURE_STORAGE_CONNECTION_STRING  env var  (production)
    2. AzureWebJobsStorage              env var  (set by func host locally)
    3. telemetry-func/local.settings.json        (local dev convenience)
    4. Azurite default shorthand                 (last resort)
    """
    for var in ("AZURE_STORAGE_CONNECTION_STRING", "AzureWebJobsStorage"):
        val = os.getenv(var)
        if val:
            return val

    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "telemetry-func", "local.settings.json"),
        os.path.join("telemetry-func", "local.settings.json"),
    ]
    for path in candidates:
        try:
            with open(path) as f:
                val = json.load(f).get("Values", {}).get("AzureWebJobsStorage")
                if val:
                    return val
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

    return "UseDevelopmentStorage=true"


# ── Storage service ─────────────────────────────────────────────────────────────

class TelemetryStorage:
    def __init__(self):
        self.service = TableServiceClient.from_connection_string(_resolve_conn_str())
        self.service.create_table_if_not_exists(RAW_TABLE)
        self.service.create_table_if_not_exists(USER_INDEX_TABLE)

    # ── Writes ────────────────────────────────────────────────────────────────

    def write_raw_batch(self, user: str, device: str, events: List[Dict[str, Any]]) -> int:
        """
        Write a batch of raw events to RawTelemetry using batch transactions.

        Events are grouped by PartitionKey (user_date) then submitted via
        submit_transaction() in chunks of ≤ 100.  This replaces the old
        per-event upsert_entity() loop, cutting network round-trips from
        N → ceil(N/100).

        Upsert semantics keep retried batches idempotent.
        """
        raw_table   = self.service.get_table_client(RAW_TABLE)
        index_table = self.service.get_table_client(USER_INDEX_TABLE)

        written: int = 0
        dates_seen: set = set()

        # ── Build entities grouped by PartitionKey ───────────────────────────
        # Azure batch transactions require all entities in a transaction to share
        # the same PartitionKey.
        pk_groups: Dict[str, List[Dict]] = {}
        for i, event in enumerate(events):
            ts       = event.get("timestamp") or datetime.now(timezone.utc).isoformat()
            date_str = ts[:10]
            pk       = f"{user}_{date_str}"
            rk       = f"{ts}_{i:04d}"

            entity = {
                "PartitionKey": pk,
                "RowKey":       rk,
                "timestamp":    ts,
                "app":          str(event.get("app",    "Unknown")),
                "domain":       str(event.get("domain", "")),
                "active":       bool(event.get("active", False)),
                "locked":       bool(event.get("locked", False)),
                "duration":     int(event.get("duration", 0)),
                "device":       str(device),
            }
            pk_groups.setdefault(pk, []).append(entity)
            dates_seen.add(date_str)

        # ── Submit in chunks of ≤ 100 per PartitionKey ───────────────────────
        for pk, entities in pk_groups.items():
            for chunk_start in range(0, len(entities), 100):
                chunk      = entities[chunk_start:chunk_start + 100]
                operations = [("upsert", entity) for entity in chunk]
                _submit_with_retry(raw_table, operations)
                written += len(chunk)

        # Invalidate cached raw events for every date touched by this batch
        # so the next dashboard read sees the fresh rows immediately.
        for date_str in dates_seen:
            _cache.invalidate(f"raw:{user}:{date_str}")
        _cache.invalidate("users")

        # Keep UserIndex in sync — one row per user for O(1) listing
        try:
            index_table.upsert_entity({
                "PartitionKey": "users",
                "RowKey":       user,
                "last_seen":    datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass  # best-effort; don't fail the whole batch

        return written

    # ── Deletes ───────────────────────────────────────────────────────────────

    def delete_user(self, user: str) -> int:
        """
        Delete ALL data for a user: every row in RawTelemetry whose
        PartitionKey starts with '{user}_', plus the UserIndex entry.
        Returns the number of deleted events.
        """
        raw_table   = self.service.get_table_client(RAW_TABLE)
        index_table = self.service.get_table_client(USER_INDEX_TABLE)

        # Prefix scan: PartitionKey format is {user}_{YYYY-MM-DD}
        lo = f"{user}_"
        hi = f"{user}_\uffff"
        entities = list(raw_table.query_entities(
            f"PartitionKey ge '{lo}' and PartitionKey lt '{hi}'",
            select=["PartitionKey", "RowKey"],
        ))

        deleted = self._delete_entities(raw_table, entities)

        # Remove from UserIndex
        try:
            index_table.delete_entity(partition_key="users", row_key=user)
        except Exception:
            pass

        # Wipe every cache key for this user
        stale = [k for k in list(_cache._store) if k.startswith(f"raw:{user}:")]
        for k in stale:
            _cache.invalidate(k)
        _cache.invalidate("users")

        return deleted

    def delete_user_date(self, user: str, date: str) -> int:
        """
        Delete all raw events for a user on a single date (one PartitionKey).
        Returns the number of deleted events.
        """
        raw_table = self.service.get_table_client(RAW_TABLE)
        pk        = f"{user}_{date}"
        entities  = list(raw_table.query_entities(
            f"PartitionKey eq '{pk}'",
            select=["PartitionKey", "RowKey"],
        ))

        deleted = self._delete_entities(raw_table, entities)
        _cache.invalidate(f"raw:{user}:{date}")
        return deleted

    @staticmethod
    def _delete_entities(table_client, entities: list) -> int:
        """Batch-delete a list of {PartitionKey, RowKey} dicts (max 100 per PK)."""
        if not entities:
            return 0

        # Group by PartitionKey — Azure batch requires same PK
        pk_groups: Dict[str, list] = {}
        for e in entities:
            pk_groups.setdefault(e["PartitionKey"], []).append(e)

        deleted = 0
        for pk, ents in pk_groups.items():
            for i in range(0, len(ents), 100):
                chunk      = ents[i:i + 100]
                operations = [("delete", {"PartitionKey": e["PartitionKey"], "RowKey": e["RowKey"]}) for e in chunk]
                _submit_with_retry(table_client, operations)
                deleted += len(chunk)
        return deleted

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_raw_events(self, user: str, date: str) -> List[Dict[str, Any]]:
        """
        Fetch all raw events for a user on a given date, sorted chronologically.

        Cached: 2 min for today's data, 30 min for historical dates.
        All three analytics endpoints share this cached result — only one
        Table Storage round-trip per user+date per cache window.
        """
        cache_key = f"raw:{user}:{date}"
        cached, hit = _cache.get(cache_key)
        if hit:
            return cached

        table    = self.service.get_table_client(RAW_TABLE)
        pk       = f"{user}_{date}"
        entities = table.query_entities(f"PartitionKey eq '{pk}'")

        events: List[Dict[str, Any]] = []
        for e in entities:
            events.append({
                "timestamp": e.get("timestamp") or e["RowKey"][:26],
                "app":       e.get("app",    "Unknown"),
                "domain":    e.get("domain", ""),
                "active":    bool(e.get("active", False)),
                "locked":    bool(e.get("locked", False)),
                "duration":  int(e.get("duration", 0)),
                "device":    e.get("device", ""),
            })
        events.sort(key=lambda x: x["timestamp"])

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ttl   = 120 if date == today else 1800
        _cache.set(cache_key, events, ttl)
        return events

    def get_all_users(self) -> List[str]:
        """
        Sorted list of known users via UserIndex — O(users), not O(events).
        Cached for 5 minutes; invalidated automatically on each ingest.
        """
        cached, hit = _cache.get("users")
        if hit:
            return cached

        table    = self.service.get_table_client(USER_INDEX_TABLE)
        entities = table.query_entities("PartitionKey eq 'users'", select=["RowKey"])
        users    = sorted(e["RowKey"] for e in entities)
        _cache.set("users", users, 300)
        return users
