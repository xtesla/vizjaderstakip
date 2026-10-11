import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_monitor as m

SAMPLE = {
    "id": "1234", "title": "Mathematics",
    "start": "2026-10-14T08:00:00+02:00", "end": "2026-10-14T09:40:00+02:00",
    "start_ms": 1791957600000,
    "rooms": ["Teams Teams"], "teachers": ["Tutor"],
    "groups": ["W1"], "status": None,
}
STARTS = m.week_starts(datetime(2026, 10, 11, tzinfo=ZoneInfo("Europe/Warsaw")))


def remote(item):
    r = m.format_google(item)
    r['id'] = 'google-generated-id'
    return r

class MonitorTests(unittest.TestCase):
    def test_timestamp_format(self):
        body = m.format_google(SAMPLE)
        self.assertEqual(body['start']['dateTime'], '2026-10-14T08:00:00+02:00')
        self.assertEqual(body['extendedProperties']['private']['vizjaLessonId'], '1234')

    def test_migration_from_old_event_description_no_duplicate(self):
        r = remote(SAMPLE)
        r['extendedProperties'] = {'private': {'vizjaLessonId': '1234'}}
        writes, deletions, alerts = m.plan_changes({'1234': SAMPLE}, [r], STARTS)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], 'update')
        self.assertFalse(deletions)
        self.assertFalse(alerts)

    def test_repeat_no_actions(self):
        w, d, n = m.plan_changes({'1234': SAMPLE}, [remote(SAMPLE)], STARTS)
        self.assertEqual((w,d,n),([],[],[]))

    def test_existing_event_change_notifies(self):
        old = remote(SAMPLE)
        new = {**SAMPLE, 'title': 'Advanced Mathematics'}
        w, d, n = m.plan_changes({'1234': new}, [old], STARTS)
        self.assertEqual(w[0][0], 'update')
        self.assertEqual(len(n), 1)

    def test_removal_notifies(self):
        old = remote(SAMPLE)
        other = {**SAMPLE, 'id': '9999', 'start_ms': 1792044000000}
        w, d, n = m.plan_changes({'9999': other}, [old], STARTS)
        self.assertEqual(len(d), 1)
        self.assertEqual(len(n), 2)  # New course and removed course in an already-covered week

    def test_initial_no_notification(self):
        w, d, n = m.plan_changes({'1234': SAMPLE}, [], STARTS)
        self.assertEqual(w[0][0], 'create')
        self.assertEqual(n, [])

    def test_status_changes(self):
        c = {**SAMPLE, 'status': 'ODWOŁANE'}
        body = m.format_google(c)
        self.assertTrue(body['summary'].startswith('[İPTAL]'))
        self.assertEqual(body['transparency'], 'transparent')

    def test_dst(self):
        dates = m.week_starts(datetime(2026, 10, 26, tzinfo=ZoneInfo('Europe/Warsaw')))
        self.assertEqual(datetime.fromtimestamp(dates[0]/1000, ZoneInfo('Europe/Warsaw')).isoformat(), '2026-10-26T00:00:00+01:00')

    def test_not_delete_unrelated_calendar_item(self):
        unrelated = {"id": "personal", "summary": "Dentist", "start": {"dateTime": SAMPLE['start']}}
        w, d, n = m.plan_changes({'1234': SAMPLE}, [unrelated], STARTS)
        self.assertFalse(d)
        self.assertEqual(w[0][0], 'create')

if __name__ == '__main__':
    unittest.main()
