"""Opt-in Linux probe with synthetic JPEGs and a new isolated store directory."""

import argparse
import asyncio
import json
import resource
import time
from pathlib import Path

from camera_media_service.pipeline import LiveFrameHub
from camera_media_service.recording import RecordingService
from camera_media_service.store import LatestFrameStore
from tools.recording_priority_probe import images


def process_io():
    return {
        k.rstrip(":"): int(v)
        for k, v in (line.split() for line in Path("/proc/self/io").read_text().splitlines())
    }


async def run(args):
    root = args.root.resolve()
    if root.exists() or not root.name.startswith("frame-store-probe-"):
        raise ValueError("Use a new isolated frame-store-probe-* directory")
    root.mkdir(parents=True)
    source = images()
    hub = LiveFrameHub(LatestFrameStore())
    # Omitting the new option in files mode allows measuring an older image too.
    options = {"frame_storage_mode": "packed"} if args.mode == "packed" else {}
    owner = RecordingService(root, {"camera-main"}, hub, **options)
    commits = []
    owner.db.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
    await owner.start()
    before, io_before = resource.getrusage(resource.RUSAGE_SELF), process_io()
    began = time.monotonic()
    sent = []
    try:
        for i in range(args.seconds * 30):
            await asyncio.sleep(max(0, began + i / 30 - time.monotonic()))
            sent.append(hub.publish("camera-main", source[i % len(source)]))
        await owner.queue.wait_received()
        elapsed = time.monotonic() - began
        after, io_after = resource.getrusage(resource.RUSAGE_SELF), process_io()
        async with owner.lock:
            if hasattr(owner, "frame_packs"):
                rows = owner.db.execute(
                    "SELECT f.*,r.pack,r.offset_bytes FROM frames f "
                    "LEFT JOIN frame_ranges r ON r.path=f.path "
                    "ORDER BY f.timestamp,f.path"
                ).fetchall()
                stored = owner.frame_packs.descriptors(rows)
                payloads = [owner.frame_packs.read(f) for f in stored]
            else:
                rows = owner.db.execute("SELECT * FROM frames ORDER BY timestamp,path").fetchall()
                stored = rows
                payloads = [(owner.buffer / r["path"]).read_bytes() for r in rows]
            assert [r["timestamp"] for r in rows] == [f.received_at for f in sent]
            assert payloads == [f.jpeg for f in sent]
        files = list(owner.buffer.glob("*.jpg")) + list(owner.buffer.glob("*.jpack"))
        assert sum(p.stat().st_size for p in files) == sum(len(f.jpeg) for f in sent)
        diagnostics = owner.diagnostics.snapshot()
        assert not diagnostics["loss_counts"].get("ingest_queue_full", 0)
        result = {
            "state": "passed",
            "mode": args.mode,
            "paced_fps": 30,
            "frames": len(sent),
            "all_receipts_and_payloads_preserved": True,
            "buffer_files": len(files),
            "database_commits": len(commits),
            "payload_bytes": sum(len(f.jpeg) for f in sent),
            "allocated_buffer_bytes": sum(p.stat().st_blocks * 512 for p in files),
            "wall_seconds": elapsed,
            "cpu_seconds": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
            "io_delta": {k: io_after[k] - io_before[k] for k in io_before},
            "diagnostics": diagnostics,
        }
    finally:
        await owner.close()
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "diagnostics"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mode", choices=("files", "packed"), default="packed")
    parser.add_argument("--seconds", type=int, choices=range(3, 61), default=10)
    asyncio.run(run(parser.parse_args()))
