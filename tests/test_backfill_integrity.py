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


class FixedAugustDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 31, tzinfo=tz)


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

    def test_not_listed_requires_official_date_before_month(self):
        data = {
            'code': '2237',
            'market': 'TWSE',
            'series': [{'date': '2026-08-11', 'close': 27.05}],
            'errors': [],
            'month_checks': {
                '2026-07': {
                    'status': 'not_listed',
                    'checked_on': '2026-10-30',
                    'listing_date': '2026-07-31',
                    'listing_source': v.LISTING_SOURCE,
                },
            },
        }
        issues = v.inspect(data, '2237', [dt.date(2026, 7, 1)], TODAY)
        self.assertIn('2026-07:invalid_not_listed_evidence', issues)

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

    def test_parser_records_verified_zero_trade_row_and_special_change_symbol(self):
        no_trade = ['115/08/12', '0', '0', '--', '--', '--', '--', ' 0.00', '0', '']
        no_price = ['115/07/13', '2', '106', '--', '--', '--', '--', ' 0.00', '2', '']
        special_change = ['115/08/13', '10,559,012', '110,645,098',
                          '10.45', '10.85', '10.10', '10.15', 'X0.00', '3,526', '']
        self.assertEqual(b.parse_monthly_row(no_trade), ('2026-08-12', None, 'no_trade'))
        self.assertEqual(b.parse_monthly_row(no_price), ('2026-07-13', None, 'no_price'))
        self.assertIsNone(b.parse_close(no_trade))
        self.assertEqual(b.parse_close(special_change), ('2026-08-13', 10.15))
        self.assertIsNone(b.parse_monthly_row(
            ['115/08/12', '1', '0', '--', '--', '--', '--', ' 0.00', '0', '']
        ))
        self.assertIsNone(b.parse_monthly_row(
            ['115/07/13', '2', '106', '--', '--', '--', '--', ' 0.05', '2', '']
        ))
        self.assertIsNone(b.parse_monthly_row(
            ['115/07/13', '1,,06', '106', '--', '--', '--', '--', ' 0.00', '2', '']
        ))

    def test_listing_dates_are_read_from_official_roster(self):
        response = [
            {'公司代號': '2237', '上市日期': '20260810'},
            {'公司代號': '2254', '上市日期': '20231020'},
            {'公司代號': '2321', '上市日期': '19911108'},
        ]
        with patch.object(b, 'fetch', return_value=response):
            self.assertEqual(b.fetch_listing_dates(['2237', '2254', '2321']), {
                '2237': dt.date(2026, 8, 10),
                '2254': dt.date(2023, 10, 20),
                '2321': dt.date(1991, 11, 8),
            })

    def test_prelisting_month_uses_official_date_and_clears_its_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [
                {'code': '2237', 'name': '華德動能-創', 'market': 'TWSE',
                 'common_stock_candidate': True},
            ]}))
            dest.mkdir()
            (dest / '2237.json').write_text(json.dumps({
                'code': '2237', 'market': 'TWSE',
                'series': [{'date': '2026-08-11', 'close': 27.05}],
                'errors': [{'month': '2026-07-01', 'reason': 'No matching data'}],
            }))

            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', [
                     'backfill', '--code', '2237', '--month', '2026-07', '--months', '1'
                 ]), \
                 patch.object(b.dt, 'datetime', FixedOctoberDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={
                     '2237': dt.date(2026, 8, 10),
                 }), \
                 patch.object(b.time, 'sleep'), \
                 patch.object(b, 'fetch', side_effect=AssertionError(
                     'A confirmed pre-listing month must not request STOCK_DAY'
                 )) as fetch:
                b.main()

            saved = v.read(dest / '2237.json')
            self.assertEqual(saved['month_checks']['2026-07'], {
                'status': 'not_listed',
                'checked_on': '2026-10-30',
                'listing_date': '2026-08-10',
                'listing_source': v.LISTING_SOURCE,
            })
            self.assertEqual(saved['errors'], [])
            self.assertEqual(
                v.inspect(saved, '2237', [dt.date(2026, 7, 1)], TODAY), []
            )
            fetch.assert_not_called()

    def test_unknown_listing_date_does_not_hide_non_ok_monthly_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [
                {'code': '2237', 'market': 'TWSE', 'common_stock_candidate': True},
            ]}))
            response = {
                'stat': '很抱歉，沒有符合條件的資料!',
                'total': 0,
            }
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', [
                     'backfill', '--code', '2237', '--month', '2026-07', '--months', '1'
                 ]), \
                 patch.object(b.dt, 'datetime', FixedOctoberDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={}), \
                 patch.object(b.time, 'sleep'), \
                 patch.object(b, 'fetch', return_value=response):
                b.main()

            saved = v.read(dest / '2237.json')
            self.assertEqual(saved['month_checks']['2026-07']['status'], 'error')
            self.assertEqual(saved['errors'][0]['reason'], f'Non-OK response: {response}')
            self.assertIn('fetch_errors', saved['validation']['issues'])

    def test_unresolved_month_errors_outside_window_are_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [
                {'code': '1101', 'market': 'TWSE', 'common_stock_candidate': True},
            ]}))
            dest.mkdir()
            (dest / '1101.json').write_text(json.dumps({
                'code': '1101', 'market': 'TWSE', 'series': [],
                'errors': [{'month': '2026-07-01', 'reason': 'previous timeout'}],
            }))

            def month_response(url):
                month = url.split('date=')[1][:6]
                year = int(month[:4])
                number = int(month[4:6])
                roc_day = year - 1911
                return {
                    'stat': 'OK',
                    'date': month + '01',
                    'title': f'{roc_day}年{number:02}月 1101 台泥 各日成交資訊',
                    'data': [[
                        f'{roc_day}/{number:02}/01', '100', '1000',
                        '10', '10', '10', '10', '0.00', '1', '',
                    ]],
                }

            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', ['backfill', '--months', '1']), \
                 patch.object(b.dt, 'datetime', FixedOctoberDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={}), \
                 patch.object(b.time, 'sleep'), \
                 patch.object(b, 'fetch', side_effect=month_response) as fetch:
                b.main()

            saved = v.read(dest / '1101.json')
            self.assertEqual(saved['errors'], [])
            self.assertEqual(saved['month_checks']['2026-07']['status'], 'verified')
            self.assertTrue(saved['validation']['complete'])
            self.assertEqual(fetch.call_count, 2)

    def test_positive_volume_no_price_rows_are_accounted_without_faking_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [
                {'code': '2254', 'market': 'TWSE', 'common_stock_candidate': True},
            ]}))
            dest.mkdir()
            (dest / '2254.json').write_text(json.dumps({
                'code': '2254', 'market': 'TWSE',
                'series': [
                    {'date': '2026-06-30', 'close': 100.0},
                    {'date': '2026-07-10', 'close': 101.0},
                ],
                'errors': [{'month': '2026-07-01', 'reason': 'Unparseable monthly row'}],
            }))
            response = {
                'stat': 'OK',
                'date': '20260701',
                'title': '115年07月 2254 巨鎧精密 各日成交資訊',
                'data': [
                    ['115/07/13', '2', '106', '--', '--', '--', '--', ' 0.00', '2', ''],
                    ['115/07/14', '100', '10,000', '100', '102', '99', '101', '1.00', '3', ''],
                    ['115/07/21', '0', '0', '--', '--', '--', '--', ' 0.00', '0', ''],
                ],
            }
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', [
                     'backfill', '--code', '2254', '--month', '2026-07', '--months', '1'
                 ]), \
                 patch.object(b.dt, 'datetime', FixedOctoberDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={
                     '2254': dt.date(2023, 10, 20),
                 }), \
                 patch.object(b.time, 'sleep'), patch.object(b, 'fetch', return_value=response):
                b.main()

            saved = v.read(dest / '2254.json')
            check = saved['month_checks']['2026-07']
            self.assertEqual(check['status'], 'verified')
            self.assertEqual(check['count'], 1)
            self.assertEqual(check['row_count'], 3)
            self.assertEqual(check['no_trade_days'], ['2026-07-21'])
            self.assertEqual(check['no_price_days'], ['2026-07-13'])
            self.assertEqual(saved['series'], [
                {'date': '2026-06-30', 'close': 100.0},
                {'date': '2026-07-14', 'close': 101.0},
            ])
            self.assertEqual(saved['errors'], [])
            self.assertEqual(
                v.inspect(saved, '2254', [dt.date(2026, 7, 1)], TODAY), []
            )

    def test_zero_trade_row_is_recorded_and_other_month_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [{'code': '1909', 'market': 'TWSE', 'common_stock_candidate': True}]}))
            dest.mkdir()
            old_series = [
                {'date': '2026-07-31', 'close': 9.85},
                {'date': '2026-08-11', 'close': 10.10},
            ]
            (dest / '1909.json').write_text(json.dumps({
                'code': '1909', 'market': 'TWSE', 'series': old_series, 'errors': [],
            }))
            (dest / 'index.json').write_text(json.dumps({
                'generated_at': '2026-08-30T00:00:00+08:00',
                'complete_stocks': 0,
                'stocks': [
                    {'code': '1909', 'complete': False, 'issues': ['fetch_errors'],
                     'sessions': 2, 'errors': 1},
                    {'code': '2330', 'complete': False, 'issues': ['other'],
                     'sessions': 3, 'errors': 0},
                ],
            }))
            response = {
                'stat': 'OK',
                'date': '20260801',
                'title': '115年08月 1909 榮成 各日成交資訊',
                'data': [
                    ['115/08/12', '0', '0', '--', '--', '--', '--', ' 0.00', '0', ''],
                    ['115/08/13', '10,559,012', '110,645,098',
                     '10.45', '10.85', '10.10', '10.15', 'X0.00', '3,526', ''],
                ],
            }

            class FixedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 8, 31, 12, tzinfo=tz)

            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', [
                     'backfill', '--code', '1909', '--month', '2026-08', '--months', '1'
                 ]), \
                 patch.object(b.dt, 'datetime', FixedDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={}), \
                 patch.object(b.time, 'sleep'), patch.object(b, 'fetch', return_value=response):
                b.main()

            saved = v.read(dest / '1909.json')
            check = saved['month_checks']['2026-08']
            self.assertEqual(check['status'], 'verified')
            self.assertEqual(check['count'], 1)
            self.assertEqual(check['row_count'], 2)
            self.assertEqual(check['no_trade_days'], ['2026-08-12'])
            self.assertEqual(saved['series'], [
                {'date': '2026-07-31', 'close': 9.85},
                {'date': '2026-08-13', 'close': 10.15},
            ])
            self.assertEqual(saved['errors'], [])
            self.assertTrue(saved['validation']['complete'])
            index = v.read(dest / 'index.json')
            self.assertEqual(index['complete_stocks'], 1)
            self.assertEqual(index['stocks'][0]['complete'], True)
            self.assertEqual(index['stocks'][1], {
                'code': '2330', 'complete': False, 'issues': ['other'],
                'sessions': 3, 'errors': 0,
            })

    def test_targeted_backfill_preserves_other_month_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [
                {'code': '1909', 'market': 'TWSE', 'common_stock_candidate': True},
                {'code': '2330', 'market': 'TWSE', 'common_stock_candidate': True},
            ]}))
            dest.mkdir()
            old_error = {'month': '2026-07-01', 'reason': 'existing July error'}
            (dest / '1909.json').write_text(json.dumps({
                'code': '1909', 'market': 'TWSE',
                'series': [{'date': '2026-07-31', 'close': 9.85}],
                'errors': [old_error],
            }))
            response = {
                'stat': 'OK',
                'date': '20260801',
                'title': '115年08月 1909 榮成 各日成交資訊',
                'data': [
                    ['115/08/13', '10,559,012', '110,645,098',
                     '10.45', '10.85', '10.10', '10.15', 'X0.00', '3,526', ''],
                ],
            }

            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', [
                     'backfill', '--code', '1909', '--month', '2026-08', '--months', '1'
                 ]), \
                 patch.object(b.dt, 'datetime', FixedAugustDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={}), \
                 patch.object(b.time, 'sleep'), patch.object(b, 'fetch', return_value=response) as fetch:
                b.main()

            fetch.assert_called_once()
            saved = v.read(dest / '1909.json')
            self.assertEqual(saved['errors'], [old_error])
            self.assertEqual(saved['series'], [
                {'date': '2026-07-31', 'close': 9.85},
                {'date': '2026-08-13', 'close': 10.15},
            ])
            self.assertFalse((dest / '2330.json').exists())

    def test_malformed_zero_trade_row_fails_without_overwriting_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [{'code': '1909', 'market': 'TWSE', 'common_stock_candidate': True}]}))
            dest.mkdir()
            old_series = [
                {'date': '2026-07-31', 'close': 9.85},
                {'date': '2026-08-11', 'close': 10.10},
            ]
            (dest / '1909.json').write_text(json.dumps({
                'code': '1909', 'market': 'TWSE', 'series': old_series, 'errors': [],
            }))
            response = {
                'stat': 'OK',
                'date': '20260801',
                'title': '115年08月 1909 榮成 各日成交資訊',
                'data': [
                    ['115/08/12', '1', '0', '--', '--', '--', '--', ' 0.00', '0', ''],
                ],
            }

            class FixedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 8, 31, 12, tzinfo=tz)

            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', ['backfill', '--months', '1']), \
                 patch.object(b.dt, 'datetime', FixedDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={}), \
                 patch.object(b.time, 'sleep'), patch.object(b, 'fetch', return_value=response):
                b.main()

            saved = v.read(dest / '1909.json')
            self.assertEqual(saved['series'], old_series)
            self.assertEqual(saved['errors'][0]['reason'], 'Unparseable monthly row')
            self.assertEqual(saved['month_checks']['2026-08']['status'], 'error')
            self.assertFalse(saved['validation']['complete'])

    def test_mocked_backfill_and_failed_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); source = root / 'data.json'; dest = root / 'twse'
            source.write_text(json.dumps({'records': [{'code': '1101', 'market': 'TWSE', 'common_stock_candidate': True}]}))
            response = {
                'stat': 'OK',
                'date': '20261001',
                'title': '台泥 1101 各日成交資訊',
                'data': [['115/10/01', '100', '1000', '10', '10', '10', '10', '0.00', '1', '']],
            }
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), \
                 patch.object(sys, 'argv', ['backfill', '--months', '1']), \
                 patch.object(b.dt, 'datetime', FixedOctoberDateTime), \
                 patch.object(b, 'fetch_listing_dates', return_value={}), \
                 patch.object(b.time, 'sleep'), patch.object(b, 'fetch', return_value=response):
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
            with patch.object(b, 'SOURCE', source), patch.object(b, 'DEST', dest), patch.object(sys, 'argv', ['backfill', '--months', '1']), patch.object(b.time, 'sleep'), patch.object(b.dt, 'datetime', FixedOctoberDateTime), patch.object(b, 'fetch_listing_dates', return_value={}), patch.object(b, 'fetch', side_effect=RuntimeError('network timeout')):
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
