import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import backfill as b
import backfill_integrity as v

TODAY = dt.date(2026, 10, 30)
MONTH = dt.date(2026, 10, 1)


def sample():
    rows = [{'date': '2026-10-01', 'close': 10.0}, {'date': '2026-10-02', 'close': 11.0}]
    return {'code': '1101', 'market': 'TWSE', 'series': rows, 'errors': [],
            'month_checks': {'2026-10': {'status': 'verified', 'count': 2,
            'sha256': v.digest(rows), 'checked_on': TODAY.isoformat()}}}


class FixedOctoberDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 30, tzinfo=tz)


class FixedNovemberDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 1, tzinfo=tz)


class IntegrityTests(unittest.TestCase):
    def test_valid_and_legacy(self):
        self.assertEqual(v.inspect(sample(), '1101', [MONTH], TODAY), [])
        data = sample(); del data['month_checks']
        self.assertTrue(v.inspect(data, '1101', [MONTH], TODAY))

    def test_not_started_current_month(self):
        month = dt.date(2026, 11, 1)
        today = month
        data = sample()
        data['month_checks']['2026-11'] = {'status': 'not_started', 'checked_on': today.isoformat()}
        self.assertFalse(v.has_reached_first_weekday(month, today))
        self.assertEqual(v.inspect(data, '1101', [MONTH, month], today), [])
        self.assertTrue(v.has_reached_first_weekday(month, dt.date(2026, 11, 2)))
        self.assertTrue(v.inspect(data, '1101', [MONTH, month], dt.date(2026, 11, 2)))

    def test_corrupt_cases(self):
        for mutation in ('duplicate', 'nan', 'negative', 'future', 'unsorted', 'truncated', 'identity', 'errors', 'missing_month', 'stale'):
            with self.subTest(mutation=mutation):
                data = sample()
                if mutation == 'duplicate': data['series'].append(data['series'][0])
                if mutation == 'nan': data['series'][0]['close'] = float('nan')
                if mutation == 'negative': data['series'][0]['close'] = -1
                if mutation == 'future': data['series'][0]['date'] = '2027-01-01'
                if mutation == 'unsorted': data['series'].reverse()
                if mutation == 'truncated': data['series'].pop()
                if mutation == 'identity': data['code'] = '2330'
                if mutation == 'errors': data['errors'] = [{'reason': 'timeout'}]
                if mutation == 'missing_month': data['series'] = []
                if mutation == 'stale': data['month_checks']['2026-10']['checked_on'] = '2026-10-29'
                self.assertTrue(v.inspect(data, '1101', [MONTH], TODAY))

    def test_malformed_and_absent(self):
        for data in (None, [], {}, {'series': [None]}):
            self.assertTrue(v.inspect(data, '1101', [MONTH], TODAY))

    def test_planner_retries_without_starvation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            stocks = [{'code': '1101'}, {'code': '2330'}]
            self.assertEqual(v.plan(stocks, root, [MONTH], TODAY, 1), 0)
            failed = sample(); failed['errors'] = ['timeout']; failed['last_attempt'] = '2026-10-30T01:00'
            (root / '1101.json').write_text(json.dumps(failed))
            self.assertEqual(v.plan(stocks, root, [MONTH], TODAY, 1), 1)
            (root / '1101.json').write_text(json.dumps(sample()))
            other = sample(); other['code'] = '2330'
            (root / '2330.json').write_text(json.dumps(other))
            self.assertEqual(v.plan(stocks, root, [MONTH], TODAY, 1), -1)

    def test_atomic_failure_preserves_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'x.json'; path.write_text('previous')
            with self.assertRaises(ValueError): b.atomic_write(path, {'x': float('nan')})
            self.assertEqual(path.read_text(), 'previous')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_parser_rejects_nonfinite(self):
        self.assertIsNone(b.parse_close(['115/10/01', 0, 0, 0, 0, 0, 'NaN']))

    def test_mocked_backfill_and_failed_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [{'code': '1101', 'market': 'TWSE', 'common_stock_candidate': True}]}))
            response = {'stat': 'OK', 'date': '20261001', 'title': '台泥 1101 各日成交資訊',
                        'data': [['115/10/01', 0, 0, 0, 0, 0, '10']]}
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), patch.object(sys, 'argv', ['backfill', '--months', '1']), patch.object(b.time, 'sleep'), patch.object(b.dt, 'datetime', FixedOctoberDateTime), patch.object(b, 'fetch', return_value=response):
                b.main()
                saved = v.read(dest / '1101.json')
                self.assertTrue(saved['validation']['complete'])
                response['data'].append(['bad'])
                b.main()
                failed = v.read(dest / '1101.json')
                self.assertFalse(failed['validation']['complete'])
                self.assertEqual(saved['series'], failed['series'])
                self.assertTrue(failed['errors'])
                self.assertEqual(v.read(dest / 'index.json')['complete_stocks'], 0)

    def test_fetch_exception_remains_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [{'code': '1101', 'market': 'TWSE', 'common_stock_candidate': True}]}))
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), patch.object(sys, 'argv', ['backfill', '--months', '1']), patch.object(b.time, 'sleep'), patch.object(b.dt, 'datetime', FixedOctoberDateTime), patch.object(b, 'fetch', side_effect=RuntimeError('network timeout')):
                b.main()
            saved = v.read(dest / '1101.json')
            self.assertEqual(saved['errors'][0]['reason'], 'network timeout')
            self.assertFalse(saved['validation']['complete'])
            self.assertIn('fetch_errors', saved['validation']['issues'])

    def test_empty_response_before_first_weekday_is_not_a_fetch_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [{'code': '1101', 'market': 'TWSE', 'common_stock_candidate': True}]}))
            dest.mkdir()
            (dest / '1101.json').write_text(json.dumps(sample()))
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), patch.object(sys, 'argv', ['backfill', '--months', '1']), patch.object(b.dt, 'datetime', FixedNovemberDateTime), patch.object(b.time, 'sleep'), patch.object(b, 'fetch', side_effect=AssertionError('Must not fetch before first weekday')):
                b.main()
            saved = v.read(dest / '1101.json')
            self.assertEqual(saved['month_checks']['2026-11']['status'], 'not_started')
            self.assertEqual(saved['errors'], [])
            self.assertTrue(saved['validation']['complete'])

if __name__ == '__main__': unittest.main()
