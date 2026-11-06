"""Invalidate incremental coverage for every affected day, including late edits."""


def install_coverage_tracking(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS daily_coverage_totals(
            day_id TEXT PRIMARY KEY,duration REAL NOT NULL,
            covered_until REAL NOT NULL,ready_end REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_gap_dirty(day_id TEXT PRIMARY KEY,start REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_segment_dirty(day_id TEXT PRIMARY KEY,start REAL NOT NULL);
    """)
    for event, references in (
        ("INSERT", ("NEW",)),
        ("UPDATE", ("OLD", "NEW")),
        ("DELETE", ("OLD",)),
    ):
        segment_statements = []
        gap_statements = []
        for ref in references:
            segment_statements.append(f"""
                INSERT INTO daily_segment_dirty VALUES({ref}.day_id,{ref}.start)
                ON CONFLICT(day_id) DO UPDATE SET start=min(start,excluded.start);
            """)
            gap_statements.append(f"""
                INSERT INTO daily_gap_dirty
                SELECT id,{ref}.start FROM daily_recordings WHERE camera={ref}.camera
                AND CAST(strftime('%s',json_extract(body,'$.window_start_at')) AS REAL)<{ref}.end
                AND CAST(strftime('%s',json_extract(body,'$.window_end_at')) AS REAL)>{ref}.start
                ON CONFLICT(day_id) DO UPDATE SET start=min(start,excluded.start);
            """)
        db.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS coverage_segment_{event.lower()}
            AFTER {event} ON daily_segments BEGIN {"".join(segment_statements)} END;
            CREATE TRIGGER IF NOT EXISTS coverage_gap_{event.lower()}
            AFTER {event} ON daily_gaps BEGIN {"".join(gap_statements)} END;
        """)
