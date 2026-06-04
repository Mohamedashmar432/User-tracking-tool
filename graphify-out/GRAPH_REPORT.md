# Graph Report - .  (2026-06-02)

## Corpus Check
- 35 files · ~57,679 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 483 nodes · 1087 edges · 23 communities (20 shown, 3 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 16 edges (avg confidence: 0.7)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Telemetry UI (Windows)|Telemetry UI (Windows)]]
- [[_COMMUNITY_Telemetry UI (Linux)|Telemetry UI (Linux)]]
- [[_COMMUNITY_Telemetry Agent (Windows)|Telemetry Agent (Windows)]]
- [[_COMMUNITY_Telemetry Agent (Linux)|Telemetry Agent (Linux)]]
- [[_COMMUNITY_Authentication Module|Authentication Module]]
- [[_COMMUNITY_Dependencies & Groups|Dependencies & Groups]]
- [[_COMMUNITY_Data Aggregation|Data Aggregation]]
- [[_COMMUNITY_Storage Layer|Storage Layer]]
- [[_COMMUNITY_State Engine Tests|State Engine Tests]]
- [[_COMMUNITY_User Management|User Management]]
- [[_COMMUNITY_State Machine Plan|State Machine Plan]]
- [[_COMMUNITY_Agent Configuration|Agent Configuration]]
- [[_COMMUNITY_Linux Install Script|Linux Install Script]]
- [[_COMMUNITY_Build & Deploy Config|Build & Deploy Config]]
- [[_COMMUNITY_Product & Version|Product & Version]]
- [[_COMMUNITY_Deb Build Script|Deb Build Script]]
- [[_COMMUNITY_Linux Requirements|Linux Requirements]]

## God Nodes (most connected - your core abstractions)
1. `DashboardWindow` - 34 edges
2. `DashboardWindow` - 34 edges
3. `main()` - 33 edges
4. `main()` - 28 edges
5. `str` - 23 edges
6. `TelemetryStorage` - 22 edges
7. `str` - 21 edges
8. `str` - 16 edges
9. `bool` - 14 edges
10. `StateEngine` - 13 edges

## Surprising Connections (you probably didn't know these)
- `_detect_dark_mode()` --calls--> `_run()`  [INFERRED]
  linux_telemetry_ui.py → linux_telemetry_agent.py
- `ProdAnalytics Dashboard` --conceptually_related_to--> `Backend Python Dependencies`  [INFERRED]
  index.html → backend/requirements.txt
- `Root Python Dependencies` --shares_data_with--> `Backend Python Dependencies`  [INFERRED]
  requirements.txt → backend/requirements.txt
- `Request` --uses--> `TelemetryStorage`  [INFERRED]
  backend/deps.py → backend/storage.py
- `Request` --uses--> `UserStorage`  [INFERRED]
  backend/deps.py → backend/users.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **State-Machine Activity Tracking Fix** — StateEngine, ActivityState, monotonic_clock, active_invariant, event_duration_measured, ingest_validation, startup_gap_86400_cap [EXTRACTED 1.00]
- **ProdAnalytics Deployment Stack** — azure_pipelines_yml, requirements_txt, runtime_txt, backend_requirements_txt [EXTRACTED 0.90]

## Communities (23 total, 3 thin omitted)

### Community 0 - "Telemetry UI (Windows)"
Cohesion: 0.07
Nodes (31): Misc, aggregate_backup(), _apply_dwm_effects(), build_display_data(), check_for_update(), DashboardWindow, _detect_dark_mode(), _fade_in() (+23 more)

### Community 1 - "Telemetry UI (Linux)"
Cohesion: 0.07
Nodes (30): Icon, aggregate_backup(), build_display_data(), build_tray_icon(), check_for_update(), DashboardWindow, _detect_dark_mode(), fetch_server() (+22 more)

### Community 2 - "Telemetry Agent (Windows)"
Cohesion: 0.09
Nodes (47): BaseException, _accumulate(), _acquire_singleton(), aggregate_events(), _backup_dir(), _backup_files(), _base_url(), check_connection() (+39 more)

### Community 3 - "Telemetry Agent (Linux)"
Cohesion: 0.09
Nodes (42): Connection, Enum, _accumulate(), _acquire_singleton(), ActivityState, aggregate_events(), _base_url(), check_connection() (+34 more)

### Community 4 - "Authentication Module"
Cohesion: 0.09
Nodes (35): create_token(), _env(), get_current_user(), str, require_admin(), verify_agent_key(), str, str (+27 more)

### Community 5 - "Dependencies & Groups"
Cohesion: 0.10
Nodes (19): _public_base(), Request, str, _resolve_device_user(), verify_device_key(), verify_ingest_key(), GroupStorage, bool (+11 more)

### Community 6 - "Data Aggregation"
Cohesion: 0.14
Nodes (32): _agg_apps(), _agg_summary(), aggregate_all(), aggregate_apps(), aggregate_summary(), build_timeline(), _build_timeline_from_merged(), categorize() (+24 more)

### Community 7 - "Storage Layer"
Cohesion: 0.20
Nodes (11): Any, bool, int, str, _resolve_conn_str(), _submit_with_retry(), TelemetryStorage, _TTLCache (+3 more)

### Community 8 - "State Engine Tests"
Cohesion: 0.12
Nodes (5): _validate_events(), _import_validate_events(), test_ingest_validation_batch_cap(), test_ingest_validation_clamps_huge_duration(), test_ingest_validation_removes_zero_duration()

### Community 9 - "User Management"
Cohesion: 0.30
Nodes (5): _hash(), bool, str, UserStorage, _verify()

### Community 10 - "State Machine Plan"
Cohesion: 0.50
Nodes (8): State-Machine Activity Tracking Implementation Plan, ActivityState Enum, StateEngine Class, Active Time Session Invariant, Measured Event Duration, Server-Side Ingest Duration Validation, Monotonic Clock Timing, Startup Gap 86400s Cap

### Community 11 - "Agent Configuration"
Cohesion: 0.25
Nodes (7): api_key, batch_size, flush_interval, idle_threshold, ingest_url, log_interval, tick_interval

### Community 12 - "Linux Install Script"
Cohesion: 0.50
Nodes (7): install.sh script, die(), _fix_dir(), info(), PATH, success(), warn()

### Community 13 - "Build & Deploy Config"
Cohesion: 0.40
Nodes (5): Azure CI/CD Pipeline, Backend Python Dependencies, ProdAnalytics Dashboard, Root Python Dependencies, Python Runtime Specification

## Knowledge Gaps
- **24 isolated node(s):** `ingest_url`, `api_key`, `idle_threshold`, `tick_interval`, `log_interval` (+19 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `_run()` connect `Telemetry Agent (Linux)` to `Telemetry UI (Linux)`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Why does `_detect_dark_mode()` connect `Telemetry UI (Linux)` to `Telemetry Agent (Linux)`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **What connects `ingest_url`, `api_key`, `idle_threshold` to the rest of the system?**
  _24 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Telemetry UI (Windows)` be split into smaller, more focused modules?**
  _Cohesion score 0.06584723441615452 - nodes in this community are weakly interconnected._
- **Should `Telemetry UI (Linux)` be split into smaller, more focused modules?**
  _Cohesion score 0.07115384615384615 - nodes in this community are weakly interconnected._
- **Should `Telemetry Agent (Windows)` be split into smaller, more focused modules?**
  _Cohesion score 0.08858166922683051 - nodes in this community are weakly interconnected._
- **Should `Telemetry Agent (Linux)` be split into smaller, more focused modules?**
  _Cohesion score 0.08771929824561403 - nodes in this community are weakly interconnected._