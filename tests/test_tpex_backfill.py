import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import backfill_integrity as integrity
import tpex_backfill as tpex

TODAY = dt.date(2026, 10, 30)


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 30, tzinfo=tz)


def month_response():
    rows = [
        ["115/10/29", "4,687", "723,393", "156.00", "157.00",
         "153.00", "153.00", "-3.00", "4,555"],
        ["115/10/30", "6,636", "986,574", "149.50", "151.00",
         "147.00", "149.00", "-4.00", "5,327"],
    ]
    return {
        "stat": "ok",
        "date": "20261001",
        "code": "8069",
        "name": "元太",
        "tables": [{
            "title": "個股日成交資訊",
            "date": "20261001",
            "data": rows,
            "totalCount": len(rows),
        }],
    }


class TpexBackfillTests(unittest.TestCase):
    def test_candidate_roster_is_scoped_to_tpex(self):
        payload = {
            "records": [
                {"code": "1101", "market": "TWSE", "common_stock_candidate": True},
                {"code": "8069", "market": "TPEx", "common_stock_candidate": True},
                {"code": "00679B", "market": "TPEx", "common_stock_candidate": False},
            ]
        }
        self.assertEqual(
            [row["code"] for row in integrity.candidates(payload, market="TPEx")],
            ["8069"],
        )

    def test_parser_requires_valid_weekday_and_positive_close(self):
        valid = [
            "115/10/30", "1,000", "149,000", "149.00", "151.00",
            "147.00", "149.50", "0", "1"
        ]
        self.assertEqual(
            tpex.parse_monthly_row(valid),
            ("2026-10-30", 149.5),
        )
        self.assertIsNone(tpex.parse_monthly_row(
            ["115/10/31", "1,000", "149,000", "149.00", "151.00",
             "147.00", "149.50", "0", "1"]
        ))
        self.assertIsNone(tpex.parse_monthly_row(
            ["115/10/30", "1,000", "149,000", "149.00", "151.00",
             "147.00", "NaN", "0", "1"]
        ))
        self.assertIsNone(tpex.parse_monthly_row(
            ["115/10/30", "1,000", "149,000", "149.00", "151.00",
             "147.00", "--", "0", "1"]
        ))
        self.assertIsNone(tpex.parse_monthly_row(
            ["115/10/30", "1,,000", "149,000", "149.00", "151.00",
             "147.00", "149.50", "0", "1"]
        ))

    def test_month_response_checks_market_identity_and_rows(self):
        with patch.object(tpex, "fetch", return_value=month_response()):
            rows, name = tpex.fetch_month("8069", dt.date(2026, 10, 1))
        self.assertEqual(name, "元太")
        self.assertEqual(rows, {
            "2026-10-29": 153.0,
            "2026-10-30": 149.0,
        })

        payload = month_response()
        payload["code"] = "2330"
        with patch.object(tpex, "fetch", return_value=payload):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                tpex.fetch_month("8069", dt.date(2026, 10, 1))

    def test_batch_preserves_verified_past_month_and_fetches_current_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = pathlib.Path(tmp) / "tpex"
            destination.mkdir()
            september = dt.date(2026, 9, 1)
            october = dt.date(2026, 10, 1)
            old_series = [{"date": "2026-09-30", "close": 140.0}]
            old = {
                "code": "8069",
                "name": "元太",
                "market": "TPEx",
                "series": old_series,
                "errors": [],
                "month_checks": {
                    "2026-09": {
                        "status": "verified",
                        "checked_on": TODAY.isoformat(),
                        "count": 1,
                        "row_count": 1,
                        "no_trade_days": [],
                        "no_price_days": [],
                        "sha256": integrity.digest(old_series),
                    }
                },
            }
            (destination / "8069.json").write_text(json.dumps(old))

            with (
                patch.object(tpex, "DEST", destination),
                patch.object(
                    tpex,
                    "fetch_month",
                    return_value=({"2026-10-30": 149.0}, "元太"),
                ) as fetch_month,
                patch.object(tpex.time, "sleep"),
            ):
                output = tpex.update_stock(
                    {"code": "8069", "name": "元太"},
                    [september, october],
                    TODAY,
                    [september, october],
                )

            fetch_month.assert_called_once_with("8069", october)
            self.assertEqual(
                output["series"],
                [
                    {"date": "2026-09-30", "close": 140.0},
                    {"date": "2026-10-30", "close": 149.0},
                ],
            )
            self.assertTrue(output["validation"]["complete"])

    def test_failed_month_preserves_quotes_and_updates_tpex_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "data.json"
            destination = root / "historical" / "tpex"
            source.write_text(json.dumps({
                "fetched_at": "2026-10-30T21:00:00+08:00",
                "sources": {"TPEx": {"url": "https://official.example/quotes"}},
                "records": [{
                    "code": "8069",
                    "name": "元太",
                    "market": "TPEx",
                    "common_stock_candidate": True,
                }],
            }))

            with (
                patch.object(tpex, "SOURCE", source),
                patch.object(tpex, "DEST", destination),
                patch.object(tpex.dt, "datetime", FixedDateTime),
                patch.object(tpex.time, "sleep"),
                patch.object(tpex, "fetch", return_value=month_response()),
            ):
                tpex.main([
                    "--code", "8069", "--month", "2026-10", "--months", "7"
                ])

            stock_path = destination / "8069.json"
            first = json.loads(stock_path.read_text())
            original_series = first["series"]
            self.assertEqual(first["market"], "TPEx")
            self.assertEqual(first["month_checks"]["2026-10"]["status"], "verified")
            self.assertEqual(first["validation"]["complete"], False)
            self.assertTrue((destination / "stocks.json").exists())
            self.assertTrue((destination / "index.json").exists())

            with (
                patch.object(tpex, "SOURCE", source),
                patch.object(tpex, "DEST", destination),
                patch.object(tpex.dt, "datetime", FixedDateTime),
                patch.object(tpex.time, "sleep"),
                patch.object(tpex, "fetch", side_effect=RuntimeError("API timeout")),
            ):
                tpex.main([
                    "--code", "8069", "--month", "2026-10", "--months", "7"
                ])

            retried = json.loads(stock_path.read_text())
            self.assertEqual(retried["series"], original_series)
            self.assertEqual(retried["month_checks"]["2026-10"]["status"], "error")
            self.assertIn("API timeout", retried["errors"][-1]["reason"])
            self.assertFalse(retried["validation"]["complete"])
            index = json.loads((destination / "index.json").read_text())
            self.assertEqual(index["market"], "TPEx")
            self.assertEqual(index["complete_stocks"], 0)

    def test_integrity_market_scoping_and_weekend_rejection(self):
        month = dt.date(2026, 10, 1)
        data = {
            "code": "8069",
            "market": "TPEx",
            "series": [{"date": "2026-10-30", "close": 149.0}],
            "errors": [],
            "month_checks": {
                "2026-10": {
                    "status": "verified",
                    "checked_on": TODAY.isoformat(),
                    "count": 1,
                    "sha256": integrity.digest([
                        {"date": "2026-10-30", "close": 149.0}
                    ]),
                }
            },
        }
        self.assertEqual(
            integrity.inspect(data, "8069", [month], TODAY, market="TPEx"),
            [],
        )
        self.assertIn(
            "identity_mismatch",
            integrity.inspect(data, "8069", [month], TODAY),
        )
        data["series"][0]["date"] = "2026-10-31"
        data["month_checks"]["2026-10"]["sha256"] = integrity.digest(
            [{"date": "2026-10-31", "close": 149.0}]
        )
        self.assertIn(
            "invalid_duplicate_or_unsorted_row",
            integrity.inspect(data, "8069", [month], TODAY, market="TPEx"),
        )


if __name__ == "__main__":
    unittest.main()
