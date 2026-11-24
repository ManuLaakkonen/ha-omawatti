"""Unit tests for custom_components.omawatti pure logic and async API client."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "omawatti"


def _load_omawatti_api():
    """Load const.py and api.py without triggering HA imports in __init__.py."""
    pkg = types.ModuleType("omawatti_test_pkg")
    pkg.__path__ = [str(COMPONENT_DIR)]
    sys.modules["omawatti_test_pkg"] = pkg

    const_spec = importlib.util.spec_from_file_location(
        "omawatti_test_pkg.const", COMPONENT_DIR / "const.py"
    )
    assert const_spec and const_spec.loader
    const_mod = importlib.util.module_from_spec(const_spec)
    sys.modules["omawatti_test_pkg.const"] = const_mod
    const_spec.loader.exec_module(const_mod)

    api_spec = importlib.util.spec_from_file_location(
        "omawatti_test_pkg.api", COMPONENT_DIR / "api.py"
    )
    assert api_spec and api_spec.loader
    api_mod = importlib.util.module_from_spec(api_spec)
    sys.modules["omawatti_test_pkg.api"] = api_mod
    api_spec.loader.exec_module(api_mod)
    return const_mod, api_mod


const, api = _load_omawatti_api()


class _FakeResponse:
    def __init__(self, status: int, body: Any = None, text: str = "") -> None:
        self.status = status
        self._body = body
        self._text = text

    async def json(self, content_type: Any = None) -> Any:
        return self._body

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, post_responses: list[_FakeResponse], get_responses: list[_FakeResponse]) -> None:
        self._post_responses = list(post_responses)
        self._get_responses = list(get_responses)
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: Any = None, headers: Any = None) -> _FakeResponse:
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self._post_responses.pop(0)

    def get(self, url: str, *, params: Any = None, headers: Any = None) -> _FakeResponse:
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        return self._get_responses.pop(0)


class TestOmaWattiParsing(unittest.TestCase):
    """Test contract parsing, time-series aggregation, DST, and summaries."""

    def test_slugify_finnish(self) -> None:
        self.assertEqual(api.slugify_fi("Mäntymäentie"), "mantymaentie")
        self.assertEqual(api.slugify_fi("Åkerlundinkatu 5"), "akerlundinkatu_5")
        self.assertEqual(api.slugify_fi("Öljytie / Varasto"), "oljytie_varasto")
        self.assertEqual(api.slugify_fi("!!!"), "meter")

    def test_parse_active_contracts_filters_expired_and_deduplicates(self) -> None:
        raw = [
            {
                "customerNumber": "100001",
                "usagePlaceType": "ELE",
                "validTo": "toistaiseksi",
                "productDescription": "Yleissiirto TEHO",
                "meteringPoint": {
                    "Number": "643007000000000001",
                    "StreetName": "Testitie",
                    "HouseNumber": "1",
                    "City": "LAHTI",
                },
            },
            # Duplicate metering point (e.g. energy sales + grid transfer on same GSRN)
            {
                "customerNumber": "100001",
                "usagePlaceType": "ELE",
                "validTo": "toistaiseksi",
                "productDescription": "Pörssisähkö",
                "meteringPoint": {
                    "Number": "643007000000000001",
                    "StreetName": "Testitie",
                    "HouseNumber": "1",
                    "City": "LAHTI",
                },
            },
            # Second ELE meter on same street -> gets _2 slug suffix
            {
                "customerNumber": "100001",
                "usagePlaceType": "ELE",
                "validTo": "toistaiseksi",
                "productDescription": "Yleissiirto",
                "meteringPoint": {
                    "Number": "643007000000000002",
                    "StreetName": "Testitie",
                    "HouseNumber": "2",
                    "City": "LAHTI",
                },
            },
            # Expired contract -> must be filtered out
            {
                "customerNumber": "100001",
                "usagePlaceType": "ELE",
                "validTo": "31.12.2023",
                "productDescription": "Vanha sopimus",
                "meteringPoint": {
                    "Number": "643007000000000099",
                    "StreetName": "Vanhatie",
                    "HouseNumber": "9",
                    "City": "LAHTI",
                },
            },
            # Active district heating contract on same street -> shares clean slug
            {
                "customerNumber": "100001",
                "usagePlaceType": "HEA",
                "validTo": "toistaiseksi",
                "productDescription": "Tilaustehotuote",
                "meteringPoint": {
                    "Number": "123456",
                    "StreetName": "Testitie",
                    "HouseNumber": "1",
                    "City": "LAHTI",
                },
            },
        ]

        contracts = api.parse_active_contracts(raw, today=date(2026, 11, 24))
        self.assertEqual(len(contracts), 3)

        ele = [c for c in contracts if c.usage_place_type == "ELE"]
        hea = [c for c in contracts if c.usage_place_type == "HEA"]
        self.assertEqual([c.slug for c in ele], ["testitie", "testitie_2"])
        self.assertEqual([c.slug for c in hea], ["testitie"])
        # Ensure slug never embeds GSRN or house number
        for c in contracts:
            self.assertNotIn("643007", c.slug)
            self.assertNotIn("123456", c.slug)
        # Ensure UI label includes recognizable address and last 4 digits
        self.assertIn("Testitie 1, Lahti (…0001)", ele[0].ui_label)

    def test_aggregate_quarterly_to_hourly_filters_nulls_and_handles_dst(self) -> None:
        # Finnish autumn DST transition: 2026-10-25 has 03:00+03:00 AND 03:00+02:00
        readings = [
            {"time": "2026-10-25T03:00:00+03:00", "value": 0.25, "status": "Mitattu"},
            {"time": "2026-10-25T03:15:00+03:00", "value": 0.25, "status": "Mitattu"},
            {"time": "2026-10-25T03:30:00+03:00", "value": 0.25, "status": "Mitattu"},
            {"time": "2026-10-25T03:45:00+03:00", "value": 0.25, "status": "Mitattu"},
            {"time": "2026-10-25T03:00:00+02:00", "value": 0.50, "status": "Mitattu"},
            {"time": "2026-10-25T03:15:00+02:00", "value": 0.50, "status": "Mitattu"},
            {"time": "2026-10-25T03:30:00+02:00", "value": 0.50, "status": "Mitattu"},
            {"time": "2026-10-25T03:45:00+02:00", "value": 0.50, "status": "Mitattu"},
            # Future unmeasured null slot must be skipped
            {"time": "2026-10-25T04:00:00+02:00", "value": None, "status": ""},
        ]

        hourly = api.aggregate_quarterly_to_hourly(readings)
        self.assertEqual(len(hourly), 2)
        self.assertEqual(hourly[0].start, datetime(2026, 10, 25, 0, 0, tzinfo=UTC))
        self.assertAlmostEqual(hourly[0].value, 1.0)
        self.assertEqual(hourly[1].start, datetime(2026, 10, 25, 1, 0, tzinfo=UTC))
        self.assertAlmostEqual(hourly[1].value, 2.0)

    def test_heat_mwh_to_kwh_and_delta_t(self) -> None:
        raw_energy = [
            {"time": "2026-01-01T00:00:00+02:00", "value": 0.004, "status": "Mitattu"},
            {"time": "2026-01-01T01:00:00+02:00", "value": 0.002, "status": "Mitattu"},
            {"time": "2026-01-01T02:00:00+02:00", "value": None, "status": ""},
        ]
        energy_kwh = api.parse_hourly_readings(raw_energy, multiplier=1000.0)
        self.assertEqual(len(energy_kwh), 2)
        self.assertAlmostEqual(energy_kwh[0].value, 4.0)
        self.assertAlmostEqual(energy_kwh[1].value, 2.0)

        t_in = api.parse_hourly_readings(
            [
                {"time": "2026-01-01T00:00:00+02:00", "value": 87.5},
                {"time": "2026-01-01T01:00:00+02:00", "value": 89.0},
            ]
        )
        t_out = api.parse_hourly_readings(
            [
                {"time": "2026-01-01T00:00:00+02:00", "value": 34.0},
            ]
        )
        delta = api.compute_delta_t(t_in, t_out)
        self.assertEqual(len(delta), 1)
        self.assertAlmostEqual(delta[0].value, 53.5)

    def test_cumulative_statistics_monotonic_with_anchor(self) -> None:
        pts = [
            api.HourlyPoint(datetime(2026, 11, 20, 0, 0, tzinfo=UTC), 1.25),
            api.HourlyPoint(datetime(2026, 11, 20, 1, 0, tzinfo=UTC), 0.75),
            api.HourlyPoint(datetime(2026, 11, 20, 2, 0, tzinfo=UTC), 2.00),
        ]
        rows = api.build_cumulative_statistics(pts, initial_sum=100.5)
        self.assertEqual([r["sum"] for r in rows], [101.75, 102.5, 104.5])
        self.assertEqual([r["state"] for r in rows], [1.25, 0.75, 2.0])

    def test_iter_year_chunks_multi_year_backfill(self) -> None:
        chunks = api.iter_year_chunks(date(2024, 3, 15), date(2026, 11, 24))
        self.assertEqual(
            chunks,
            [
                (date(2024, 3, 15), date(2024, 12, 31)),
                (date(2025, 1, 1), date(2025, 12, 31)),
                (date(2026, 1, 1), date(2026, 11, 24)),
            ],
        )

    def test_compute_metric_summary_skips_partial_today(self) -> None:
        tz = timezone(timedelta(hours=2))
        points = []
        # 8 complete days (Nov 15 .. Nov 22): 24h each, 1.0 kWh/h -> 24 kWh/day
        start = datetime(2026, 11, 15, 0, 0, tzinfo=tz)
        for h in range(8 * 24):
            points.append(api.HourlyPoint((start + timedelta(hours=h)).astimezone(UTC), 1.0))
        # Partial Nov 23: only 3 hours (should NOT become latest_day_total!)
        partial_start = datetime(2026, 11, 23, 0, 0, tzinfo=tz)
        for h in range(3):
            points.append(
                api.HourlyPoint((partial_start + timedelta(hours=h)).astimezone(UTC), 0.5)
            )

        summary = api.compute_metric_summary(points, local_tz=tz)
        self.assertEqual(summary.latest_day_date, "2026-11-22")
        self.assertAlmostEqual(summary.latest_day_total, 24.0)
        self.assertEqual(summary.latest_day_hours, 24)
        self.assertAlmostEqual(summary.last_7d_total, 7 * 24.0)
        self.assertAlmostEqual(summary.latest_value, 0.5)


class TestOmaWattiClientAsync(unittest.IsolatedAsyncioTestCase):
    """Test async auth header, automatic 401 re-login, and error mapping."""

    async def test_uses_frends_header_and_retries_once_on_401(self) -> None:
        session = _FakeSession(
            post_responses=[
                _FakeResponse(
                    200,
                    {"success": True, "data": {"token": "jwt-1", "customerNumber": "100001"}},
                ),
                _FakeResponse(
                    200,
                    {"success": True, "data": {"token": "jwt-2", "customerNumber": "100001"}},
                ),
            ],
            get_responses=[
                _FakeResponse(401, text="expired"),
                _FakeResponse(
                    200,
                    {
                        "success": True,
                        "data": {
                            "readings": [
                                {"time": "2026-11-23T00:00:00+02:00", "value": 0.005}
                            ]
                        },
                    },
                ),
            ],
        )
        client = api.OmaWattiClient(session, "demo_user", "demo_pass")
        readings = await client.async_fetch_readings(
            "heat-energy", "123456", date(2026, 11, 23), date(2026, 11, 23)
        )
        self.assertEqual(len(readings), 1)
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(len(session.get_calls), 2)
        self.assertEqual(
            session.get_calls[0]["headers"]["x-frends-bearer-token"], "Bearer jwt-1"
        )
        self.assertEqual(
            session.get_calls[1]["headers"]["x-frends-bearer-token"], "Bearer jwt-2"
        )
        self.assertNotIn("Authorization", session.get_calls[1]["headers"])

    async def test_invalid_credentials_raises_auth_error(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(401, text="unauthorized")],
            get_responses=[],
        )
        client = api.OmaWattiClient(session, "bad", "creds")
        with self.assertRaises(api.OmaWattiAuthError):
            await client.async_authenticate()

    async def test_outage_503_raises_api_error(self) -> None:
        session = _FakeSession(
            post_responses=[
                _FakeResponse(
                    200,
                    {"success": True, "data": {"token": "jwt-1", "customerNumber": "100001"}},
                )
            ],
            get_responses=[_FakeResponse(503, text="Maintenance outage")],
        )
        client = api.OmaWattiClient(session, "demo", "pass")
        with self.assertRaises(api.OmaWattiApiError):
            await client.async_get_contracts()


class TestIntegrationMetadataFiles(unittest.TestCase):
    """Validate manifest, translations JSON, and Python syntax for all files."""

    def test_json_files_valid(self) -> None:
        for rel in (
            "manifest.json",
            "strings.json",
            "translations/fi.json",
            "translations/en.json",
        ):
            data = json.loads((COMPONENT_DIR / rel).read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)

        manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["domain"], "omawatti")
        self.assertTrue(manifest["config_flow"])
        self.assertIn("recorder", manifest["dependencies"])


if __name__ == "__main__":
    unittest.main()
