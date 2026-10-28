
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                colony_id TEXT NOT NULL,
                load_epoch TEXT NOT NULL,
                game_tick INTEGER NOT NULL,
                wall_time REAL NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX events_colony_sequence ON events(colony_id, seq);
            CREATE INDEX events_kind_colony_sequence ON events(kind, colony_id, seq);
            CREATE INDEX events_utterance_reference ON events(colony_id, kind, json_extract(payload, '$.utterance_seq'), seq);
            CREATE TABLE board_revisions (
                entry_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                colony_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL REFERENCES events(seq),
                kind TEXT NOT NULL,
                author TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                parent TEXT,
                source TEXT NOT NULL,
                evidence TEXT NOT NULL,
                review_at_tick INTEGER,
                changesets TEXT NOT NULL,
                body TEXT NOT NULL,
                PRIMARY KEY (entry_id, revision)
            );
            CREATE INDEX revisions_colony ON board_revisions(colony_id, entry_id, revision);
            CREATE TRIGGER sealed_event_update
                BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT,'Event log is append-only'); END;
            CREATE TRIGGER sealed_event_delete
                BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT,'Event log is append-only'); END;
            CREATE TRIGGER sealed_revision_update
                BEFORE UPDATE ON board_revisions
                BEGIN SELECT RAISE(ABORT,'Board revisions are append-only'); END;
            CREATE TRIGGER sealed_revision_delete
                BEFORE DELETE ON board_revisions
                BEGIN SELECT RAISE(ABORT,'Board revisions are append-only'); END;
