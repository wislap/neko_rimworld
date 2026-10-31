from ..workspace.journal import Journal


def snapshot(epoch, sampled_at, started_at):
    return {'connection_epoch': 'connection', 'colony_id': 'colony', 'load_epoch': epoch,
            'game_tick': sampled_at, 'snapshot_sequence': 0, 'cursor': 1, 'kind': 'session_snapshot',
            'data': {'cursor': 1, 'kind': 'session_snapshot', 'origin': 'loaded_game', 'start_tick': started_at}}


def test_reload_retains_actual_start_and_original_snapshot_sample_separately(tmp_path):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_load('colony', 'old', 10)
        journal.ingest_game_event('colony', 'old', 50, 'connection', snapshot('old', 50, 10))
        assert len(journal.events_by_kind('colony', ('colony.load',))) == 1
        original = snapshot('new', 100, 20)
        assert journal.ingest_game_event('colony', 'new', 100, 'connection', original)
        loaded = journal.latest_event('colony', 'colony.load')
        raw = journal.latest_event('colony', 'game.event')
        assert loaded['load_epoch'] == 'new' and loaded['game_tick'] == 20
        assert raw['game_tick'] == 100 and raw['payload']['event'] == original
        assert not journal.ingest_game_event('colony', 'new', 100, 'connection', original)
        assert len(journal.events_by_kind('colony', ('colony.load',))) == 2
    finally:
        journal.close()


def test_old_progress_after_observed_load_does_not_duplicate_new_load(tmp_path):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_load('colony', 'old', 10)
        journal.ingest_game_event('colony', 'old', 50, 'connection', snapshot('old', 50, 10))
        journal.observe_epoch('colony', 'new', 100)
        journal.record('colony', 'old', 50, 'planner', 'planner.progress', {'start_seq': 1})
        journal.ingest_game_event('colony', 'new', 100, 'connection', snapshot('new', 100, 20))
        assert [e['load_epoch'] for e in journal.events_by_kind('colony', ('colony.load',))] == ['old', 'new']
    finally:
        journal.close()
