"""Tests against a throwaway database built from Flighty's real table schema
(tests/schema.sql: CREATE TABLE statements only, no data).

Run: uv run python -m unittest discover tests
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flighty  # noqa: E402
import formatting  # noqa: E402
import main  # noqa: E402

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
OWNER = "owner-uuid"


class FixtureDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "flighty.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO Account (id, rawKind, rawStatus, authToken, created, lastUpdated)"
            " VALUES (7, 'main', 'enabled', 'x', 0, 0)"
        )
        for code, tz in (("LHR", "Europe/London"), ("MEX", "America/Mexico_City")):
            conn.execute(
                "INSERT INTO Airport (id, iata, name, city, country, countryCode, latitude,"
                " longitude, timeZoneIdentifier, relevance, created, lastUpdated)"
                " VALUES (?, ?, ?, 'c', 'c', 'cc', 0, 0, ?, 0, 0, 0)",
                [code, code, code, tz],
            )
        conn.execute(
            "INSERT INTO Airline (id, iata, name, relevance, created, lastUpdated)"
            " VALUES ('BA', 'BA', 'British Airways', 0, 0, 0)"
        )
        conn.commit()
        conn.close()
        patcher = mock.patch.object(flighty, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def add_owner_flight(self, number: str, days_ahead: float) -> None:
        dep = int((datetime.now(timezone.utc) + timedelta(days=days_ahead)).timestamp())
        conn = sqlite3.connect(self.db)
        fid = f"f-{number}-{days_ahead}"
        conn.execute(
            "INSERT INTO Flight (id, number, departureAirportId, scheduledArrivalAirportId,"
            " actualArrivalAirportId, airlineId, isCancelled, hasOfficialData, distance,"
            " lastKnownDepartureDate, lastKnownArrivalDate, departureScheduleGateOriginal,"
            " arrivalScheduleGateOriginal, created, lastUpdated)"
            " VALUES (?, ?, 'LHR', 'MEX', 'MEX', 'BA', 0, '1', 0, ?, ?, ?, ?, 0, 0)",
            [fid, number, dep, dep + 40000, dep, dep + 40000],
        )
        conn.execute(
            "INSERT INTO UserFlight (accountId, userId, flightId, isRandom, isProUpgrade,"
            " isMyFlight, isArchived, importSource, created, lastUpdated)"
            " VALUES (7, ?, ?, 0, 0, 1, 0, 'EMAIL', 0, 0)",
            [OWNER, fid],
        )
        conn.commit()
        conn.close()


class ListFlightsTest(FixtureDB):
    def test_upcoming_is_soonest_first_and_says_when_capped(self):
        """1 Nov 2026: limit=10 newest-first cut off the 3 and 6 Nov Mexico legs, so
        they were reported missing. Upcoming must start with the next flight, and a
        capped list must say more exist (upstream's text output now carries that note)."""
        for d in range(1, 13):
            self.add_owner_flight(f"{100 + d}", d)
        flights = flighty.list_flights(upcoming_only=True, limit=10)
        self.assertEqual(len(flights), 10)
        self.assertEqual(flights[0]["flight_number"], "101")
        self.assertIn("More may exist", formatting.format_flight_list(flights, limit=10))

    def test_no_note_when_everything_fits(self):
        self.add_owner_flight("201", 2)
        flights = flighty.list_flights(upcoming_only=True, limit=10)
        self.assertEqual(len(flights), 1)
        self.assertNotIn("More may exist", formatting.format_flight_list(flights, limit=10))


class SearchFlightsTest(FixtureDB):
    def test_friend_rows_are_labelled_and_own_rows_are_not(self):
        """19 Nov 2026: search_flights listed amr*'s DL1999 beside Joseph's AA6957
        with no owner label, and it was reported to him as a clash."""
        self.add_owner_flight("243", 2)
        conn = sqlite3.connect(self.db)
        dep = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
        conn.execute(
            "INSERT INTO Flight (id, number, departureAirportId, scheduledArrivalAirportId,"
            " actualArrivalAirportId, airlineId, isCancelled, hasOfficialData, distance,"
            " lastKnownDepartureDate, lastKnownArrivalDate, departureScheduleGateOriginal,"
            " arrivalScheduleGateOriginal, created, lastUpdated)"
            " VALUES ('f-friend', '999', 'LHR', 'MEX', 'MEX', 'BA', 0, '1', 0, ?, ?, ?, ?, 0, 0)",
            [dep, dep + 40000, dep, dep + 40000],
        )
        conn.execute(
            "INSERT INTO UserFlight (accountId, userId, flightId, isRandom, isProUpgrade,"
            " isMyFlight, isArchived, importSource, created, lastUpdated)"
            " VALUES (7, 'friend-uuid', 'f-friend', 0, 0, 0, 0, 'CONNECTED_FRIEND', 0, 0)"
        )
        conn.execute(
            "INSERT INTO Profile (accountId, userId, fullName, lastUpdated)"
            " VALUES (7, 'friend-uuid', 'amr*', 0)"
        )
        conn.commit()
        conn.close()
        flights = {f["flight_number"]: f for f in flighty.search_flights(airline="BA")}
        self.assertIsNone(flights["243"]["friend_name"])
        self.assertEqual(flights["999"]["friend_name"], "amr*")
        text = formatting.format_flight_list(list(flights.values()))
        self.assertIn("BA999 — British Airways · LHR → MEX (friend: amr*)", text)
        self.assertNotIn("BA243 — British Airways · LHR → MEX (friend", text)


class GetFlightTest(FixtureDB):
    def test_full_code_matches_airline_plus_number(self):
        """17 Nov 2026: get_flight("AA2123") returned None for a flight list_flights
        showed, because Flighty stores number "2123" and airline AA apart."""
        self.add_owner_flight("243", 2)
        for code in ("BA243", "ba 243", "BA-243", "243"):
            with self.subTest(code=code):
                out = flighty.get_flight(flight_number=code)
                self.assertIsNotNone(out)
                self.assertEqual(out["flight_number"], "243")
        self.assertIsNotNone(flighty.get_flight_status("BA243"))

    def test_other_airline_same_number_does_not_match(self):
        self.add_owner_flight("243", 2)
        self.assertIsNone(flighty.get_flight(flight_number="UA243"))

    def test_repeated_route_reads_the_next_leg(self):
        """Audit 18 Nov 2026: a number flown weekly (or booked twice) read back its
        furthest-out leg, so status and delay forecast described the wrong day."""
        for days in (-14, -7, 7, 14):
            self.add_owner_flight("243", days)
        self.assertEqual(flighty.get_flight(flight_number="BA243")["id"], "f-243-7")
        status = flighty.get_flight_status("BA243")
        self.assertEqual(
            status["scheduled_departure"], flighty.get_flight(flight_id="f-243-7")["departureScheduleGateOriginal"]
        )

    def test_repeated_route_in_the_air_beats_next_week(self):
        self.add_owner_flight("243", -0.1)  # departed ~2h ago, lands ~9h from now
        self.add_owner_flight("243", 7)
        self.assertEqual(flighty.get_flight(flight_number="BA243")["id"], "f-243--0.1")

    def test_repeated_route_all_past_reads_the_latest(self):
        for days in (-21, -7, -14):
            self.add_owner_flight("243", days)
        self.assertEqual(flighty.get_flight(flight_number="BA243")["id"], "f-243--7")


class AddFlightTest(FixtureDB):
    def test_inserts_with_the_owners_account_id(self):
        self.add_owner_flight("999", -30)
        date = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        out = flighty.add_flight("BA243", date, "LHR", "MEX", "10:00", "14:00")
        self.assertEqual(out["status"], "created")
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute("SELECT accountId FROM UserFlight WHERE flightId = ?", [out["flight_id"]]).fetchone()[0],
            7,
        )

    def test_existing_flight_same_number_and_day_is_not_duplicated(self):
        self.add_owner_flight("243", 5)
        dep = datetime.now(timezone.utc) + timedelta(days=5)
        out = flighty.add_flight("BA243", dep.strftime("%Y-%m-%d"), "LHR", "MEX")
        self.assertEqual(out["status"], "exists")
        text = main.add_flight("BA243", dep.strftime("%Y-%m-%d"), "LHR", "MEX")
        self.assertTrue(text.startswith("Already in Flighty, nothing added"), text)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM Flight WHERE number = '243'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
