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
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import logging

from azure.data.tables import TableServiceClient
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

_LOG = logging.getLogger("telemetry.storage")

RAW_TABLE        = "RawTelemetry"
USER_INDEX_TABLE = "UserIndex"
SETTINGS_TABLE   = "AgentSettings"


# ── In-memory TTL cache ─────────────────────────────────────────────────────────
# Prevents the three analytics endpoints (summary / apps / timeline) from each
# making an independent Table Storage round-trip for the same user+date data.
#
# TTL policy:
#   today's data    → 2 minutes  (agent batches every 5 min, 2 min is fresh enough)
#   historical data → 30 minutes (past days rarely change)
#   user list       → 5 minutes

class _TTLCache:
    """LRU TTL cache with a bounded size to prevent unbounded memory growth."""

    def __init__(self, maxsize: int = 512):
        self._store: OrderedDict[str, Tuple[Any, float]] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str) -> Tuple[Optional[Any], bool]:
        """Returns (value, hit). Expired entries are evicted on access."""
        if key not in self._store:
            return None, False
        value, expires = self._store[key]
        if time.monotonic() < expires:
            self._store.move_to_end(key)
            return value, True
        del self._store[key]
        return None, False

    def set(self, key: str, value: Any, ttl: int) -> None:
        if key in self._store:
            del self._store[key]
        elif len(self._store) >= self._maxsize:
            self._store.popitem(last=False)  # evict LRU entry
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
        self.service.create_table_if_not_exists(SETTINGS_TABLE)

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
        _cache.invalidate("users_with_aliases")
        _cache.invalidate("users_with_details")

        # Keep UserIndex in sync — one row per user for O(1) listing.
        # Use merge-update (no read needed) to preserve existing created_at.
        # On first ingest (ResourceNotFoundError) fall back to a full upsert.
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                index_table.update_entity({
                    "PartitionKey": "users",
                    "RowKey":       user,
                    "last_seen":    now_iso,
                }, mode="merge")
            except ResourceNotFoundError:
                index_table.upsert_entity({
                    "PartitionKey": "users",
                    "RowKey":       user,
                    "last_seen":    now_iso,
                    "created_at":   now_iso,
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
        _cache.invalidate("users_with_aliases")
        _cache.invalidate("users_with_details")

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

    def rename_user(self, old_name: str, new_name: str) -> dict:
        """
        Rename a tracked employee in all tables.

        Because Azure Table Storage uses PartitionKeys that embed the username
        (e.g. "alice_2026-04-17"), renaming requires:
            1. Fetch every raw event for old_name
            2. Re-insert them under new_name partition keys
            3. Delete the old rows
            4. Swap the UserIndex entry

        Raises ValueError if new_name is already in use.
        Returns {"old_name", "new_name", "migrated": event_count}.
        """
        old = old_name.strip()
        new = new_name.strip().lower()

        if old == new:
            return {"old_name": old, "new_name": new, "migrated": 0}

        raw_table   = self.service.get_table_client(RAW_TABLE)
        index_table = self.service.get_table_client(USER_INDEX_TABLE)

        # Reject if new name already exists in UserIndex
        existing = self.get_all_users()
        if new in [u.lower() for u in existing]:
            raise ValueError(f"Username '{new}' is already in use")

        # Prefix-scan for every RawTelemetry row belonging to old_name
        lo = f"{old}_"
        hi = f"{old}_\uffff"
        old_entities = list(raw_table.query_entities(
            f"PartitionKey ge '{lo}' and PartitionKey lt '{hi}'"
        ))

        # Build clean copies with new PartitionKey
        new_entities: List[Dict[str, Any]] = []
        for e in old_entities:
            date = e["PartitionKey"][len(old) + 1:]   # "YYYY-MM-DD"
            new_entities.append({
                "PartitionKey": f"{new}_{date}",
                "RowKey":       e["RowKey"],
                "timestamp":    str(e.get("timestamp", "")),
                "app":          str(e.get("app",    "Unknown")),
                "domain":       str(e.get("domain", "")),
                "active":       bool(e.get("active", False)),
                "locked":       bool(e.get("locked", False)),
                "duration":     int(e.get("duration", 0)),
                "device":       str(e.get("device", "")),
            })

        # Insert under new name
        pk_groups: Dict[str, List] = {}
        for e in new_entities:
            pk_groups.setdefault(e["PartitionKey"], []).append(e)

        for pk, entities in pk_groups.items():
            for i in range(0, len(entities), 100):
                chunk = entities[i:i + 100]
                _submit_with_retry(raw_table, [("upsert", entity) for entity in chunk])

        # Delete old rows only after new rows are safely written
        self._delete_entities(raw_table, old_entities)

        # Swap UserIndex entry
        try:
            old_idx  = index_table.get_entity("users", old)
            last_seen = old_idx.get("last_seen", datetime.now(timezone.utc).isoformat())
        except ResourceNotFoundError:
            last_seen = datetime.now(timezone.utc).isoformat()

        index_table.upsert_entity({
            "PartitionKey": "users",
            "RowKey":       new,
            "last_seen":    last_seen,
        })
        try:
            index_table.delete_entity("users", old)
        except Exception:
            pass

        # Wipe cache for both names
        stale = [k for k in list(_cache._store)
                 if k.startswith(f"raw:{old}:") or k.startswith(f"raw:{new}:")]
        for k in stale:
            _cache.invalidate(k)
        _cache.invalidate("users")
        _cache.invalidate("users_with_aliases")

        _LOG.info("rename_user: %s → %s (%d events migrated)", old, new, len(old_entities))
        return {"old_name": old, "new_name": new, "migrated": len(old_entities)}

    def merge_users(self, source: str, target: str) -> dict:
        """
        Merge all RawTelemetry from `source` into `target`.

        Use case: an agent was reinstalled and picked up a different username
        (e.g. machine hostname changed), creating a second profile.  Admin
        merges the old profile into the current one so history is unified.

        Strategy:
            - Re-insert every source event under the target's PartitionKey
              using upsert, so if both users have events on the same date the
              rows are combined (not overwritten — RowKeys encode real timestamps
              so collisions are practically impossible between two distinct agents).
            - Delete all source rows after the new rows are safely written.
            - Remove source from UserIndex.

        Raises ValueError if source == target or either user is not found.
        Returns {"source", "target", "migrated": event_count}.
        """
        src = source.strip()
        tgt = target.strip()

        if src.lower() == tgt.lower():
            raise ValueError("Source and target must be different users")

        raw_table   = self.service.get_table_client(RAW_TABLE)
        index_table = self.service.get_table_client(USER_INDEX_TABLE)

        # Verify both users exist in UserIndex
        existing = self.get_all_users()
        if src not in existing:
            raise ValueError(f"Source user '{src}' not found")
        if tgt not in existing:
            raise ValueError(f"Target user '{tgt}' not found")

        # Fetch every raw event for source user (prefix scan)
        lo = f"{src}_"
        hi = f"{src}_\uffff"
        source_entities = list(raw_table.query_entities(
            f"PartitionKey ge '{lo}' and PartitionKey lt '{hi}'"
        ))

        # Build clean copies under the target's PartitionKey prefix
        migrated_entities: List[Dict[str, Any]] = []
        for e in source_entities:
            date = e["PartitionKey"][len(src) + 1:]   # "YYYY-MM-DD"
            migrated_entities.append({
                "PartitionKey": f"{tgt}_{date}",
                "RowKey":       e["RowKey"],           # timestamp-based — unique across agents
                "timestamp":    str(e.get("timestamp", "")),
                "app":          str(e.get("app",    "Unknown")),
                "domain":       str(e.get("domain", "")),
                "active":       bool(e.get("active", False)),
                "locked":       bool(e.get("locked", False)),
                "duration":     int(e.get("duration", 0)),
                "device":       str(e.get("device", "")),
            })

        # Upsert migrated events into target (grouped by PartitionKey, chunks ≤ 100)
        pk_groups: Dict[str, List] = {}
        for e in migrated_entities:
            pk_groups.setdefault(e["PartitionKey"], []).append(e)

        for pk, entities in pk_groups.items():
            for i in range(0, len(entities), 100):
                chunk = entities[i:i + 100]
                _submit_with_retry(raw_table, [("upsert", entity) for entity in chunk])

        # Delete source rows only after target rows are safely written
        self._delete_entities(raw_table, source_entities)

        # Remove source from UserIndex
        try:
            index_table.delete_entity("users", src)
        except Exception:
            pass

        # Invalidate cache for both users
        stale = [k for k in list(_cache._store)
                 if k.startswith(f"raw:{src}:") or k.startswith(f"raw:{tgt}:")]
        for k in stale:
            _cache.invalidate(k)
        _cache.invalidate("users")
        _cache.invalidate("users_with_aliases")

        _LOG.info("merge_users: %s -> %s (%d events migrated)", src, tgt, len(source_entities))
        return {"source": src, "target": tgt, "migrated": len(source_entities)}

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
        entities = table.query_entities(
            f"PartitionKey eq '{pk}'",
            select=["timestamp", "app", "domain", "active", "locked", "duration"],
        )

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

    def set_alias(self, username: str, alias: str) -> bool:
        """
        Set a display alias for an employee without touching RawTelemetry.
        The alias is shown in the dashboard; data is still keyed by the
        original username the agent uses.  Returns False if user not found.
        """
        index_table = self.service.get_table_client(USER_INDEX_TABLE)
        try:
            entity = index_table.get_entity("users", username)
            entity["alias"] = alias.strip()
            index_table.update_entity(entity, mode="replace")
            _cache.invalidate("users_with_aliases")
            return True
        except ResourceNotFoundError:
            return False

    def get_users_with_aliases(self) -> List[Dict[str, str]]:
        """
        Returns [{user, alias}] for all known employees.
        alias == user when no custom display name has been set.
        Cached for 5 minutes alongside the plain user list.
        """
        cached, hit = _cache.get("users_with_aliases")
        if hit:
            return cached

        table    = self.service.get_table_client(USER_INDEX_TABLE)
        entities = table.query_entities("PartitionKey eq 'users'", select=["RowKey", "alias", "last_seen"])
        result   = sorted(
            [{
                "user":      e["RowKey"],
                "alias":     e.get("alias") or e["RowKey"],
                "last_seen": e.get("last_seen") or "",
            } for e in entities],
            key=lambda x: x["alias"].lower(),
        )
        _cache.set("users_with_aliases", result, 300)
        return result

    # ── Per-user device key management ───────────────────────────────────────────
    # Each device gets a unique key generated at install time.
    # The key grants POST /ingest access + GET /api/me/* access for that user only.
    # The admin key is NEVER stored on the device — only used once during registration.

    def register_device_key(self, username: str, key: str) -> None:
        """
        Store a per-user agent key in UserIndex.
        Called once during device install (admin-authenticated).
        """
        table = self.service.get_table_client(USER_INDEX_TABLE)
        try:
            entity = table.get_entity("users", username)
        except ResourceNotFoundError:
            entity = {"PartitionKey": "users", "RowKey": username}
        entity["agent_key"] = key
        table.upsert_entity(entity)
        _cache.invalidate("device_keys")
        _cache.invalidate("users")
        _cache.invalidate("users_with_aliases")

    def get_device_key_map(self) -> Dict[str, str]:
        """
        Returns {agent_key: username} for every registered device.
        Cached for 5 minutes; invalidated on registration.
        Used by auth layer to validate per-user keys on every request.
        The cache ensures only one Table Storage scan per 5-minute window
        regardless of how frequently agents POST to /ingest.
        """
        cached, hit = _cache.get("device_keys")
        if hit:
            return cached
        table  = self.service.get_table_client(USER_INDEX_TABLE)
        result: Dict[str, str] = {}
        try:
            for entity in table.query_entities(
                "PartitionKey eq 'users'",
                select=["RowKey", "agent_key"],
            ):
                key = entity.get("agent_key", "")
                if key:
                    result[key] = entity["RowKey"]
        except Exception as exc:
            _LOG.warning("device_key_map scan failed: %s", exc)
        _cache.set("device_keys", result, 300)
        return result

    def get_all_users(self) -> List[str]:
        """
        Sorted list of canonical usernames via UserIndex — O(users), not O(events).
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

    def get_users_with_details(self) -> List[Dict[str, Any]]:
        """Return all users with created_at and last_seen timestamps. Cached 5 min."""
        cached, hit = _cache.get("users_with_details")
        if hit:
            return cached
        table = self.service.get_table_client(USER_INDEX_TABLE)
        users = []
        try:
            for e in table.query_entities(
                "PartitionKey eq 'users'",
                select=["RowKey", "last_seen", "created_at"],
            ):
                users.append({
                    "username":   e["RowKey"],
                    "last_seen":  e.get("last_seen",  ""),
                    "created_at": e.get("created_at", ""),
                })
        except Exception as exc:
            _LOG.error("get_users_with_details failed: %s", exc)
        _cache.set("users_with_details", users, 300)
        return users

    def get_oldest_data_date(self) -> Optional[str]:
        """
        Return the ISO date string of the oldest RawTelemetry row, or None.
        Cached for 10 minutes — called by the notifications endpoint.
        """
        cached, hit = _cache.get("oldest_data_date")
        if hit:
            return cached
        raw_table = self.service.get_table_client(RAW_TABLE)
        oldest: Optional[str] = None
        try:
            for entity in raw_table.list_entities(select=["PartitionKey"]):
                date_str = entity["PartitionKey"][-10:]
                if len(date_str) == 10 and (oldest is None or date_str < oldest):
                    oldest = date_str
        except Exception as exc:
            _LOG.error("get_oldest_data_date scan failed: %s", exc)
        _cache.set("oldest_data_date", oldest, 600)
        return oldest

    # ── Global settings ───────────────────────────────────────────────────────────

    def get_settings(self) -> Dict[str, Any]:
        """Return the global settings row. Returns defaults if not yet configured."""
        table = self.service.get_table_client(SETTINGS_TABLE)
        try:
            e = table.get_entity("global", "settings")
            return {
                "retention_enabled": bool(e.get("retention_enabled", True)),
                "retention_days":    int(e.get("retention_days", 90)),
                "last_purge":        e.get("last_purge", ""),
            }
        except ResourceNotFoundError:
            return {"retention_enabled": True, "retention_days": 90, "last_purge": ""}

    def save_settings(self, data: Dict[str, Any]) -> None:
        """Persist settings. Only known fields are written."""
        table = self.service.get_table_client(SETTINGS_TABLE)
        entity = {
            "PartitionKey":       "global",
            "RowKey":             "settings",
            "retention_enabled":  bool(data.get("retention_enabled", True)),
            "retention_days":     int(data.get("retention_days", 90)),
            "last_purge":         data.get("last_purge", ""),
        }
        table.upsert_entity(entity)

    # ── Retention / purge ─────────────────────────────────────────────────────────

    def purge_old_events(self, days: int) -> int:
        """
        Delete all RawTelemetry rows whose PartitionKey date is older than `days`.
        PartitionKey format: {username}_{YYYY-MM-DD}

        Strategy:
          1. Page through all entities with select=[PartitionKey, RowKey].
          2. Parse the date from the last segment of each PartitionKey.
          3. Collect (PartitionKey, RowKey) pairs for rows older than cutoff.
          4. Delete in batches of 100 (Azure Table batch limit).

        Returns the number of deleted rows.
        """
        from datetime import date, timedelta
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

        raw_table = self.service.get_table_client(RAW_TABLE)

        # Collect stale entities grouped by PartitionKey for efficient batch deletes
        stale: Dict[str, List[str]] = {}   # {pk: [rk, ...]}
        try:
            for entity in raw_table.list_entities(select=["PartitionKey", "RowKey"]):
                pk = entity["PartitionKey"]
                # PartitionKey = "username_YYYY-MM-DD" — date is the last 10 chars
                date_part = pk[-10:]
                if date_part < cutoff:   # strict: keep data from exactly retention_days ago
                    stale.setdefault(pk, []).append(entity["RowKey"])
        except Exception as exc:
            _LOG.error("purge scan failed: %s", exc)
            return 0

        deleted = 0
        for pk, row_keys in stale.items():
            for i in range(0, len(row_keys), 100):
                chunk = row_keys[i:i + 100]
                ops   = [("delete", {"PartitionKey": pk, "RowKey": rk}) for rk in chunk]
                try:
                    _submit_with_retry(raw_table, ops)
                    deleted += len(chunk)
                except Exception as exc:
                    _LOG.error("purge delete failed for pk=%s: %s", pk, exc)

        # Record last purge timestamp
        if deleted > 0 or True:   # always update last_purge so UI shows "never" correctly
            settings = self.get_settings()
            settings["last_purge"] = datetime.now(timezone.utc).isoformat()
            self.save_settings(settings)

        _LOG.info("purge_old_events: deleted %d rows older than %s", deleted, cutoff)
        return deleted
