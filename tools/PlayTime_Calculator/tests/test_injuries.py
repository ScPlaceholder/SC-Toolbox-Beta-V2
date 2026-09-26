"""Tests for core.injuries — parsing real Game.log injury/med-bed line formats
and aggregating them into per-part / per-tier counts.

Fixture lines are copied from real Star Citizen Game.logs (player name
anonymised).  One injury produces one ``Added notification`` line plus several
echoes; only the former may be counted.
"""
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

from core import injuries as inj

ADDED = ('<2026-09-08T01:17:05.579Z> [Notice] <SHUDEvent_OnNotification> Added notification '
         '"Severe Injury Detected - Head - Tier 1 Treatment Required : " [51] to queue. '
         'New queue size: 1, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
         '[Team_CoreGameplayFeatures][Missions][Comms]')
ADDED_SPAM = ('<2026-03-27T03:06:58.857Z> [SPAM 54][Notice] <SHUDEvent_OnNotification> Added '
              'notification "Minor Injury Detected - Right leg - Tier 3 Treatment Required : " '
              '[5961] to queue. New queue size: 5806, MissionId: '
              '[00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
              '[Team_CoreGameplayFeatures][Missions][Comms]')
QUEUE_ECHO = ('<2026-09-08T01:17:05.579Z>    "Severe Injury Detected - Head - Tier 1 '
              'Treatment Required : " [51]')
UPDATE_ECHO = ('<2026-09-08T01:17:09.101Z> [Notice] <UpdateNotificationItem> Notification '
               '"Moderate Injury Detected - Left arm - Tier 2 Treatment Required : " [250], '
               'Action: StartFade [Team_CoreGameplayFeatures][Missions][Comms]')
SURGERY = ('<2025-08-21T22:40:12.000Z> [Notice] <MED BED HEAL> Actor: Pilot '
           '(Non-Authoritative CLIENT: Pilot) | [CEntityComponentMedBed::HandleComponentEvent:1042]'
           ' | -> Perform surgery event Success, med bed name: '
           'Bed_Single_Medical_Apollo_Tier_1-Left, vehicle name: @vehicle_NameRSI_Apollo_Triage, '
           'head: false torso: true leftArm: true rightArm: false leftLeg: false rightLeg: false '
           '[Team_ActorFeatures][Actor]')


# ── line parsing ─────────────────────────────────────────────────────────────

class TestParseInjuryLine:
    def test_added_notification(self):
        assert inj.parse_injury_line(ADDED) == ("2026-09-08T01:17:05.579", "head", 1)

    def test_spam_prefixed_variant(self):
        assert inj.parse_injury_line(ADDED_SPAM) == ("2026-03-27T03:06:58.857", "right_leg", 3)

    def test_two_word_part_is_normalised(self):
        line = ADDED.replace("Severe", "Moderate").replace("Head", "Left arm").replace("Tier 1", "Tier 2")
        assert inj.parse_injury_line(line)[1:] == ("left_arm", 2)

    def test_queue_dump_echo_is_ignored(self):
        assert inj.parse_injury_line(QUEUE_ECHO) is None

    def test_update_notification_echo_is_ignored(self):
        assert inj.parse_injury_line(UPDATE_ECHO) is None

    def test_unrelated_notification_is_ignored(self):
        line = ADDED.replace("Severe Injury Detected - Head - Tier 1 Treatment Required : ",
                             "Medical Bed: The bed has restored your health.")
        assert inj.parse_injury_line(line) is None


class TestParseSurgeryLine:
    def test_healed_parts(self):
        assert inj.parse_surgery_line(SURGERY) == ("2025-08-21T22:40:12.000", ["torso", "left_arm"])

    def test_non_surgery_line(self):
        assert inj.parse_surgery_line(ADDED) is None


class TestScanText:
    def test_counts_one_per_injury_despite_echoes(self):
        text = "\n".join([ADDED, QUEUE_ECHO, QUEUE_ECHO, UPDATE_ECHO, ADDED_SPAM, SURGERY])
        out = inj.scan_text(text)
        assert out["inj"] == [["2026-09-08T01:17:05.579", "head", 1],
                              ["2026-03-27T03:06:58.857", "right_leg", 3]]
        assert out["surg"] == [["2025-08-21T22:40:12.000", ["torso", "left_arm"]]]

    def test_empty_text(self):
        assert inj.scan_text("") == {"inj": [], "surg": []}


# ── aggregation ──────────────────────────────────────────────────────────────

def _records():
    return [
        {"inj": [["2026-09-08T01:17:05.579", "head", 1],
                 ["2026-09-08T01:17:05.580", "left_arm", 1],
                 ["2026-09-08T02:00:00.000", "left_arm", 3]],
         "surg": [["2026-09-08T03:00:00.000", ["left_arm"]]]},
        {"inj": [], "surg": []},
        {"inj": [["2026-09-20T12:00:00.000", "left_arm", 2],
                 ["2026-09-20T12:05:00.000", "torso", 3]],
         "surg": []},
    ]


class TestAggregate:
    def test_counts_per_part_and_tier(self):
        s = inj.aggregate(_records())
        assert s.total == 5
        assert s.by_part == Counter({"left_arm": 3, "head": 1, "torso": 1})
        assert s.by_tier == Counter({1: 2, 3: 2, 2: 1})
        assert s.by_part_tier["left_arm"] == Counter({1: 1, 3: 1, 2: 1})
        assert s.severe == 2
        assert s.most_hit == ("left_arm", 3)
        assert s.sessions_with_injuries == 2

    def test_surgeries(self):
        s = inj.aggregate(_records())
        assert s.surgeries == 1
        assert s.parts_healed == Counter({"left_arm": 1})

    def test_week_series_includes_zero_weeks(self):
        s = inj.aggregate(_records())
        weeks = s.week_series()
        # 12 days apart -> first week, possibly a gap week, last week; all contiguous.
        assert sum(n for _, n in weeks) == 5
        for (a, _), (b, _) in zip(weeks, weeks[1:]):
            assert (b - a).days == 7

    def test_most_hit_tie_breaks_by_display_order(self):
        s = inj.aggregate([{"inj": [["2026-01-01T00:00:00.000", "torso", 3],
                                    ["2026-01-01T00:00:01.000", "head", 3]]}])
        assert s.most_hit == ("head", 1)

    def test_empty(self):
        s = inj.aggregate([])
        assert s.is_empty and s.most_hit is None and s.week_series() == []

    def test_tolerates_missing_keys(self):
        s = inj.aggregate([{}, None, {"inj": None}])
        assert s.is_empty


class TestPerHour:
    def _sess(self, iso: str, hours: float):
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone()
        return SimpleNamespace(start_local=dt, duration_seconds=hours * 3600)

    def test_only_counts_play_time_since_first_injury(self):
        s = inj.aggregate(_records())
        sessions = [self._sess("2025-01-01T12:00:00", 100),   # before injuries were logged
                    self._sess("2026-09-08T12:00:00", 4),
                    self._sess("2026-09-20T12:00:00", 6)]
        assert inj.injuries_per_hour(s, sessions) == 5 / 10

    def test_none_without_data(self):
        assert inj.injuries_per_hour(inj.aggregate([]), []) is None
        assert inj.injuries_per_hour(inj.aggregate(_records()), []) is None


# ── wiring into the shared full-content scan ─────────────────────────────────

class TestFunStatsIntegration:
    def test_scan_file_and_aggregate_carry_injuries(self, tmp_path):
        from core import fun_stats
        log = tmp_path / "Game.log"
        log.write_text("\n".join([ADDED, QUEUE_ECHO, UPDATE_ECHO, ADDED_SPAM, SURGERY]) + "\n",
                       encoding="utf-8")
        rec = fun_stats._scan_file(str(log))
        assert len(rec["injuries"]["inj"]) == 2
        fs = fun_stats._aggregate([rec])
        assert fs.injuries.total == 2
        assert fs.injuries.by_part == Counter({"head": 1, "right_leg": 1})
        assert fs.injuries.surgeries == 1

    def test_old_cache_records_without_injuries_still_aggregate(self):
        from core import fun_stats
        fs = fun_stats._aggregate([{"ships": {}, "weapons": {}}])
        assert fs.injuries.is_empty
