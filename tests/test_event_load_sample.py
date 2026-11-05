import pytest

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


@pytest.mark.parametrize('first', ['native', 'frontdesk', 'observed'])
def test_reload_identity_and_daily_schedule_ignore_frontdesk_arrival_order(tmp_path, first):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_autoplay_activation('colony', 'old', 1, {
            'goal': 'manage', 'constraints': 'none', 'goal_edited': False, 'constraints_edited': False})
        journal.ingest_game_event('colony', 'old', 1, 'connection', snapshot('old', 1, 1))
        start = journal.record('colony', 'old', 1, 'planner', 'planner.started', {})
        journal.ingest_game_event('colony', 'old', 240001, 'connection', {
            **snapshot('old', 240001, 1), 'cursor': 2, 'kind': 'heartbeat', 'data': {}})
        if first == 'frontdesk':
            journal.record('colony', 'new', 270001, 'frontdesk', 'frontdesk.stale_wake', {})
        elif first == 'observed':
            journal.observe_epoch('colony', 'new', 270001)
        loaded = snapshot('new', 270001, 270001)
        journal.ingest_game_event('colony', 'new', 270001, 'connection', loaded)
        journal.finish_planner('colony', 'old', 1, start, start, {'status': 'degraded'}, {})
        journal.ingest_game_event('colony', 'new', 360001, 'connection', {
            **snapshot('new', 360001, 270001), 'cursor': 2, 'kind': 'heartbeat', 'data': {}})
        before = journal.events('colony')
        assert not journal.ingest_game_event('colony', 'new', 270001, 'connection', loaded)
        assert journal.events('colony') == before
        loads = journal.events_by_kind('colony', ('colony.load',))
        assert [(row['load_epoch'], row['game_tick']) for row in loads] == [('new', 270001)]
        due = journal.events_by_kind('colony', ('cadence.review_due',))
        daily = [row for row in due if row['payload'].get('review_type') == 'daily']
        assert [(row['load_epoch'], row['payload']['due_tick']) for row in daily] == [
            ('old', 60001), ('old', 120001), ('old', 180001), ('old', 240001),
            ('new', 300001), ('new', 360001)]
        assert len({row['payload']['id'] for row in daily}) == 6
        assert journal.attention_state('colony', 'new', 360001)['active']
        assert journal.finish_planner('colony', 'old', 1, start, start, {'status': 'degraded'}, {})
        assert journal.events('colony') == before
    finally:
        journal.close()


def test_observe_epoch_uses_confirmed_identity_after_frontdesk_writes_new_scope(tmp_path):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_autoplay_activation('colony', 'old', 1, {
            'goal': 'manage', 'constraints': 'none', 'goal_edited': False, 'constraints_edited': False})
        journal.record_frontdesk_wake('colony', 'new', 10, 'user', 'continue', request_id='request')
        journal.observe_epoch('colony', 'new', 10)
        journal.record('colony', 'old', 100, 'planner', 'planner.progress', {})
        assert journal.observe_epoch('colony', 'new', 10) is None
        assert [row['load_epoch'] for row in journal.events_by_kind('colony', ('colony.load',))] == ['new']
    finally:
        journal.close()
