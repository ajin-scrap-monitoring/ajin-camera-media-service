# Event-time Clips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract available footage around an explicit event timestamp without event-time video encoding.

**Architecture:** Add an event-at adapter using the existing recording job catalog, daily TS catalog and asynchronous jobs. Freeze available media and retain source days until stream-copy completes. Preserve legacy calls without event_at.

**Tech Stack:** Python 3.13, SQLite, aiohttp, FFmpeg 7.1, pytest.

**Spec:** ../specs/2026-10-26-event-time-clips-design.md

## Global Constraints

- event_at is UTC RFC3339 with milliseconds and Z.
- Boundary expansion is at most 1 second; no encoder in the new extraction path.
- Requested pre/post settings remain 0..300 seconds with a positive total.
- Preserve existing no-event_at calls and stored clips.

## Review Focus

- Request centered in a gap cannot succeed using neighboring segments.
- Duplicate requests never expand an already frozen partial clip.
- Daily deletion/expiry cannot remove queued extraction inputs.
- Old GOP intervals must not silently violate the 1-second tolerance.
- Midnight, incomplete tails, and fractional frame intervals retain honest actual timestamps.

### Task 1: Selection and error contract

**Files:** new recorded_clips.py; new tests/test_recorded_clips.py; update http_api.py.
**Interfaces:** RecordedClipService(recording, daily).create_clip(camera,event,event_at) -> (body,created).

- [x] Add failing tests for timestamp validation, absent/future/pending center, partial prefix/suffix, internal gaps, duplicate snapshots and midnight.
- [x] Run targeted pytest and confirm missing feature failures.
- [x] Implement frozen selection and shared clip catalog insertion with explicit source leases.
- [x] Verify targeted tests pass.

### Task 2: Remux and lifecycle

**Files:** new clip_media.py; update encoding.py, daily_export.py, http_api.py; new tests/test_clip_media.py.
**Interfaces:** inspect_keyframes(ffmpeg,path) -> keyframe timestamps; remux_clip(ffmpeg,root,slices,output) -> media metadata.

- [x] Add actual FFmpeg failing tests for fractional cuts, 1-second boundaries, old GOP rejection and decode correctness.
- [x] Implement keyframe inspection, stream-copy and new 1-second GOP recording.
- [x] Integrate background extraction, original-data leases, cleanup, restart recovery, storage errors and ready/failed jobs.
- [x] Verify file Range/download/delete through the real HTTP API, including cleanup races.

### Task 3: Documentation and final verification

**Files:** BACKEND_API_CONTRACT.md, README.md and validation notes.

- [x] Document event_at, errors, partial ranges and no-time legacy behavior.
- [x] Run full pytest, ruff check and format; review the whole change independently.
- [x] Fix material findings with reproducing tests; report local verification and deployment status accurately.
