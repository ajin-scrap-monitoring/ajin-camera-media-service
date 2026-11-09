"""Read archive workspace requirements without using the live writer connection."""

import json
import sqlite3


def workspace_requirement(root, floor):
    connection = sqlite3.connect(
        (root / "catalog.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=0.1
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        required = floor
        rows = connection.execute(
            "SELECT d.id,coalesce(s.body,d.body) FROM daily_recordings d "
            "LEFT JOIN daily_states s ON s.day_id=d.id "
            "WHERE d.status IN ('recording','finalizing','closed')"
        ).fetchall()
        for key, raw in rows:
            if json.loads(raw).get("archive_state") == "mp4":
                continue
            size, count = connection.execute(
                "SELECT coalesce(sum(json_extract(body,'$.size_bytes')),0),count(*) "
                "FROM daily_segments WHERE day_id=? AND status='ready'",
                (key,),
            ).fetchone()
            if count:
                required = max(required, int(size * 1.2) + 1048576)
        return required
    finally:
        connection.close()
