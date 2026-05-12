"""Unit tests for raccoon_cli/commands/create.py — mission number assignment."""

from __future__ import annotations

import pytest

from raccoon_cli.commands.create import _get_next_mission_number


# ---------------------------------------------------------------------------
# Empty / no existing missions
# ---------------------------------------------------------------------------

class TestNextMissionNumberEmpty:
    def test_empty_list_returns_ten(self):
        # M000 is reserved for setup, so first user mission is M010
        assert _get_next_mission_number([]) == 10

    def test_list_with_no_m_prefix_entries_returns_ten(self):
        # entries without a numeric prefix should be ignored
        assert _get_next_mission_number(["HelloMission", "ShutdownMission"]) == 10


# ---------------------------------------------------------------------------
# Normal sequential numbering
# ---------------------------------------------------------------------------

class TestNextMissionNumberSequential:
    def test_single_mission_m000(self):
        assert _get_next_mission_number([{"M000SetupMission": None}]) == 10

    def test_two_missions_m000_m010(self):
        missions = [{"M000SetupMission": None}, {"M010DriveMission": None}]
        assert _get_next_mission_number(missions) == 20

    def test_gap_in_numbers_uses_highest(self):
        # M000 and M030 exist → next should be M040
        missions = [{"M000SetupMission": None}, {"M030CollectMission": None}]
        assert _get_next_mission_number(missions) == 40

    def test_string_entries_also_parsed(self):
        # missions can be plain strings instead of dicts
        assert _get_next_mission_number(["M000SetupMission", "M010DriveMission"]) == 20

    def test_lowercase_m_prefix_is_accepted(self):
        assert _get_next_mission_number(["m020CollectMission"]) == 30


# ---------------------------------------------------------------------------
# Reserved numbers are skipped (M999 = shutdown, always last)
# ---------------------------------------------------------------------------

class TestNextMissionNumberReserved:
    def test_only_shutdown_returns_ten(self):
        """M999 alone → should not influence numbering → next is M010 (M000 reserved for setup)."""
        assert _get_next_mission_number([{"M999ShutdownMission": None}]) == 10

    def test_shutdown_does_not_inflate_next_number(self):
        """M020 and M999 present → next should be M030, not M1009."""
        missions = [
            {"M000SetupMission": None},
            {"M020CollectConesMission": None},
            {"M999ShutdownMission": None},
        ]
        assert _get_next_mission_number(missions) == 30

    def test_only_reserved_in_list_returns_ten(self):
        """Only reserved missions → next user mission is M010."""
        assert _get_next_mission_number([{"M999ShutdownMission": None}]) == 10

    def test_realistic_clawbot_scenario(self):
        """Reproduces the exact bug from the Ecer2026/clawbot project."""
        missions = [
            {"M000SetupMission": None},
            {"M010DriveDownRampMission": None},
            {"M020CollectConesMission": None},
            {"M999ShutdownMission": None},
        ]
        result = _get_next_mission_number(missions)
        assert result == 30, f"Expected 30, got {result} (bug would return 1009)"
