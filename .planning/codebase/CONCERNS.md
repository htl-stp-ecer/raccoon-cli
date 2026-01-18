# Codebase Concerns

**Analysis Date:** 2026-01-18

## Tech Debt

**In-Memory Command Tracking:**
- Issue: Active commands are stored in a module-level dictionary `_active_commands` with no persistence or cleanup
- Files: `raccoon/server/routes/commands.py` (line 65)
- Impact: Commands are lost on server restart; memory grows unbounded as old commands accumulate
- Fix approach: Add periodic cleanup of completed commands; consider Redis or SQLite for persistence

**Global Mutable State Patterns:**
- Issue: Multiple global singleton instances using module-level variables with no cleanup
- Files:
  - `raccoon/server/app.py` (line 16: `_config`)
  - `raccoon/server/services/lcm_spy.py` (lines 595-596: `_spy_service`, `_playback_service`)
  - `raccoon/client/connection.py` (line 303: `_connection_manager`)
- Impact: State leaks between tests; unpredictable behavior in long-running processes
- Fix approach: Implement dependency injection or use context managers for lifecycle control

**Hardcoded Parameter Mapping in Robot Generator:**
- Issue: `HARDWARE_REF_PARAMS` and `ODOMETRY_PARAM_HINTS` are hardcoded sets that must be maintained manually
- Files: `raccoon/codegen/generators/robot_generator.py` (lines 296-300, 528-549)
- Impact: Adding new motor configurations or odometry types requires code changes
- Fix approach: Use introspection to discover parameter types at runtime or load from config

**Duplicate Enum Definitions:**
- Issue: `CommandStatus` enum is defined twice with identical values
- Files:
  - `raccoon/server/routes/commands.py` (lines 25-33)
  - `raccoon/server/services/executor.py` (lines 11-18)
- Impact: Potential import confusion and maintenance burden
- Fix approach: Consolidate to single definition in a shared types module

## Known Bugs

**SSH Client Lifetime Management:**
- Symptoms: SSH connections may not be properly closed on errors
- Files: `raccoon/client/connection.py` (lines 198-219)
- Trigger: Exception during SSH operations leaves connection in inconsistent state
- Workaround: Manually call `disconnect()` in error handlers

**SFTP Sync Missing Remote Hash Comparison:**
- Symptoms: Files are always re-uploaded even if unchanged on remote
- Files: `raccoon/client/sftp_sync.py` (lines 326-327, comment: "Note: We don't have hash for remote files")
- Trigger: Any sync operation to existing project
- Workaround: None; results in unnecessary file transfers

## Security Considerations

**API Token Storage:**
- Risk: API token stored in plaintext at `~/.raccoon/api_token` with only file permissions protection
- Files: `raccoon/server/config.py` (lines 14, 81-101)
- Current mitigation: File permissions set to 0o600 (owner-only read/write)
- Recommendations: Consider encrypted storage or OS keychain integration

**SSH Auto-Add Host Keys:**
- Risk: `AutoAddPolicy()` used for SSH connections, vulnerable to MITM attacks
- Files:
  - `raccoon/client/connection.py` (line 102, 212)
  - `raccoon/client/ssh_keys.py`
- Current mitigation: None; accepts any host key
- Recommendations: Implement host key verification or known_hosts checking

**CORS Allow All Origins:**
- Risk: Server allows requests from any origin with credentials
- Files: `raccoon/server/app.py` (lines 67-73)
- Current mitigation: Documented as intentional for local network use
- Recommendations: Add option to restrict origins for production deployments

**WebSocket Token in URL:**
- Risk: API token passed in WebSocket URL query parameter
- Files:
  - `raccoon/client/api.py` (lines 247-249)
  - `raccoon/server/websocket/output_stream.py`
- Current mitigation: Server-side token validation
- Recommendations: Consider secure token exchange mechanism; tokens may appear in logs

## Performance Bottlenecks

**SFTP Hash Computation:**
- Problem: SHA256 hash computed for every local file on each sync
- Files: `raccoon/client/sftp_sync.py` (lines 353-359: `_hash_file`)
- Cause: No caching of file hashes between syncs
- Improvement path: Implement hash cache using mtime as invalidation key

**Robot Generator Import Resolution:**
- Problem: Type resolution attempts multiple import paths sequentially
- Files: `raccoon/codegen/introspection.py` (lines 141-158)
- Cause: Fallback logic tries multiple module paths on each resolution failure
- Improvement path: Build type registry at startup; cache successful resolutions

**LCM Message Decoding:**
- Problem: Every LCM message tries all known types to find fingerprint match
- Files: `raccoon/server/services/lcm_spy.py` (lines 62-75)
- Cause: Linear search through all registered exlcm types
- Improvement path: Build fingerprint-to-type lookup dict at startup

## Fragile Areas

**Code Generation Pipeline:**
- Files:
  - `raccoon/codegen/generators/robot_generator.py` (988 lines)
  - `raccoon/codegen/introspection.py`
  - `raccoon/codegen/builder.py`
- Why fragile: Relies heavily on pybind11 docstring parsing which can change between libstp versions
- Safe modification: Always test with actual libstp library; mock libstp for unit tests
- Test coverage: No automated tests for codegen

**Remote Execution Flow:**
- Files:
  - `raccoon/commands/run.py` (_run_remote function)
  - `raccoon/client/output_handler.py`
  - `raccoon/server/websocket/output_stream.py`
- Why fragile: Multiple async components with error handling that silently ignores failures
- Safe modification: Test full laptop-to-Pi execution flow; add integration tests
- Test coverage: No automated tests for remote execution

**Calibration Commands:**
- Files: `raccoon/commands/calibrate.py` (874 lines)
- Why fragile: Direct hardware interaction with timing-sensitive measurements
- Safe modification: Cannot be easily tested without physical hardware
- Test coverage: No tests possible without hardware mocking

## Scaling Limits

**Command Output Buffering:**
- Current capacity: 1000 lines per command (configurable)
- Limit: Long-running commands with verbose output may truncate early output
- Scaling path: Implement log rotation or streaming to file; configurable via `buffer_size` parameter in `raccoon/server/services/executor.py` line 32

**Project Directory Scanning:**
- Current capacity: Iterates all directories in projects folder on each API call
- Limit: Performance degrades with many projects
- Scaling path: Add caching with inotify-based invalidation; index project metadata

## Dependencies at Risk

**libstp (External Dependency):**
- Risk: Core hardware library not bundled with raccoon; version compatibility not enforced
- Impact: Codegen, calibration, and robot execution require specific libstp version
- Migration plan: Add libstp version checking; document compatibility matrix

**LCM Library:**
- Risk: LCM import is optional with graceful fallback, but functionality silently degraded
- Impact: LCM spy features unavailable without explicit error message to user
- Files: `raccoon/server/services/lcm_spy.py` (lines 22-28)
- Migration plan: Add explicit feature availability check in CLI; warn users on `lcm spy` if unavailable

## Missing Critical Features

**No Rollback on Failed Sync:**
- Problem: SFTP sync with `delete_remote=True` can leave remote in inconsistent state on partial failure
- Blocks: Safe deployment updates
- Files: `raccoon/client/sftp_sync.py`

**No Project Locking:**
- Problem: Concurrent operations on same project can cause conflicts
- Blocks: Multiple users working on same Pi

**No Configuration Validation Schema:**
- Problem: `raccoon.project.yml` validated only at runtime with cryptic error messages
- Blocks: Early error detection in IDE/editor

## Test Coverage Gaps

**No Automated Tests:**
- What's not tested: Entire codebase has zero unit tests, integration tests, or end-to-end tests
- Files: No `tests/` directory, no pytest config, no test files in `raccoon/`
- Risk: Any refactoring or bug fix can introduce regressions undetected
- Priority: High - this is the most critical concern

**Hardware-Dependent Code Untestable:**
- What's not tested: Calibration, motor control, sensor reading
- Files:
  - `raccoon/commands/calibrate.py`
  - `raccoon/server/routes/hardware.py`
- Risk: Hardware interaction bugs only discoverable on physical device
- Priority: Medium - requires hardware abstraction layer for testing

**Async Code Coverage:**
- What's not tested: WebSocket handlers, async API endpoints, remote execution
- Files:
  - `raccoon/server/websocket/`
  - `raccoon/client/output_handler.py`
  - `raccoon/client/api.py`
- Risk: Concurrency bugs and race conditions
- Priority: High - async code is particularly prone to subtle bugs

---

*Concerns audit: 2026-01-18*
