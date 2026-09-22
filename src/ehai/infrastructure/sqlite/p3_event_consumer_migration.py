"""Forward-only durable event consumer tables, installed by the migration registry."""

EVENT_CONSUMER_MIGRATION: tuple[str, ...] = (
    """
    CREATE TABLE event_consumers (
        consumer_id TEXT PRIMARY KEY,
        acknowledged_offset INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged_offset >= 0),
        acknowledged_event_id TEXT REFERENCES event_log(event_id),
        created_at TEXT NOT NULL,
        CHECK ((acknowledged_offset = 0 AND acknowledged_event_id IS NULL)
            OR (acknowledged_offset > 0 AND acknowledged_event_id IS NOT NULL))
    )
    """,
    """
    CREATE TABLE event_consumer_batches (
        batch_token TEXT PRIMARY KEY,
        consumer_id TEXT NOT NULL REFERENCES event_consumers(consumer_id),
        after_offset INTEGER NOT NULL CHECK (after_offset >= 0),
        through_offset INTEGER NOT NULL REFERENCES event_log(event_offset),
        after_event_id TEXT REFERENCES event_log(event_id),
        through_event_id TEXT NOT NULL REFERENCES event_log(event_id),
        event_count INTEGER NOT NULL CHECK (event_count > 0),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        CHECK (through_offset > after_offset)
    )
    """,
    """
    CREATE UNIQUE INDEX event_consumer_pending_batch_idx
    ON event_consumer_batches(consumer_id) WHERE acknowledged = 0
    """,
)
