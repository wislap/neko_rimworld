import pytest

from ..workspace.journal import Journal
from .test_attention_journal import CHOICES, native


def test_cadence_stale_projection_deduplicates_and_epoch_watermarks_are_separate(tmp_path):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_autoplay_activation('c', 'e', 0, CHOICES)
        section = {'status': 'ok', 'rows': []}
        snapshot = {'sample_tick': 0, 'map_id': 'map', 'alerts': section, 'conditions': section, 'threats': section}
        journal.ingest_game_event('c', 'e', 0, 'conn', native(1, 0, 'attention_snapshot', snapshot), attention_available=True)
        fact = {'id': 'condition1', 'def': 'Eclipse', 'scope': 'map'}
        for cursor, tick, edge in [(2, 10, 'appeared'), (3, 100, 'disappeared')]:
            data = {'sample_tick': tick, 'map_id': 'map', 'category': 'conditions', 'source_id': 'condition1',
                    'edge': edge, 'current': fact if edge == 'appeared' else None,
                    'previous': fact if edge == 'disappeared' else None}
            journal.ingest_game_event('c', 'e', tick, 'conn', native(cursor, tick, 'attention_changed', data), attention_available=True)
        assert journal.attention_state('c', 'e', 2700)['level'] == 'steady'
        assert journal.attention_state('c', 'e', 2650)['level'] == 'steady'
        journal.attention_state('c', 'e', 2800)
        changes = [e for e in journal.events('c') if e['kind'] == 'cadence.changed']
        assert [e['payload']['level'] for e in changes] == ['focus', 'steady']
        assert len({e['payload']['id'] for e in changes}) == len(changes)
        journal.record_load('c', 'new', 5)
        new_state = journal.attention_state('c', 'new', 5)
        assert new_state['active'] and new_state['level'] == 'steady'
        assert max(e['game_tick'] for e in journal.events('c') if e['load_epoch'] == 'new') == 5
    finally:
        journal.close()


def test_planner_finish_retry_preserves_saved_tick_after_later_observation(tmp_path):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_autoplay_activation('c', 'e', 0, CHOICES)
        start = journal.record('c', 'e', 10, 'planner', 'planner.started', {'wake_seqs': [], 'work_ids': [], 'signal_seqs': []})
        result = {'status': 'ok'}
        seq = journal.finish_planner('c', 'e', 11, start, 0, result, {})
        journal.ingest_game_event('c', 'e', 20, 'conn', native(1, 20, 'heartbeat', {}))
        assert journal.finish_planner('c', 'e', 11, start, 0, result, {}) == seq
        with pytest.raises(ValueError, match='conflicts'):
            journal.finish_planner('c', 'e', 12, start, 0, result, {})
    finally:
        journal.close()


def test_cadence_still_rejects_recorded_out_of_order_inputs(tmp_path):
    journal = Journal(tmp_path / 'events.sqlite')
    try:
        journal.record_autoplay_activation('c', 'e', 0, CHOICES)
        journal.record('c', 'e', 100, 'planner', 'planner.started', {'wake_seqs': [], 'work_ids': [], 'signal_seqs': []})
        with pytest.raises(ValueError, match='Event ticks must be ordered'):
            journal.record('c', 'e', 90, 'planner', 'planner.received', {'work_ids': [], 'signal_seqs': []})
        assert not any(e['kind'] == 'planner.received' for e in journal.events('c'))
    finally:
        journal.close()
