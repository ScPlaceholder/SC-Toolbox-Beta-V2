"""Regression tests for issue #22 -- the Requirements tab, three defects.

(a) A raw GUID rendered where a mission name belongs.  The other half of the
    join shipped in the cache and was read by no ``.py`` file: ``completionTags``
    on a contract lists the tags it GRANTS.  76 of 80 required tags on 4.10.1
    resolve.
(b) Four boolean flags were hardcoded "No" because they were read from
    ``availabilityPools``, which scmdb.net ships as the single-element list
    ``[{}]``.  The facts are on the contract record and disagreed in 897 of
    6,132 cells.  The same read also broke the "unique" availability filter and
    the rank planner's one-time/repeatable split.
(c) REQUIRED STANDING and COOLDOWN were never rendered at all, though 1,310
    contracts carry a minStanding and 1,452 a nonzero personalCooldownTime.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.manager import MissionDataManager  # noqa: E402
from data.models import FilterState, contract_availability  # noqa: E402
from services.filtering import filter_contracts  # noqa: E402
from services.indexing import index_completion_tags, index_contracts  # noqa: E402


TAG_GRANTED = "4b034350-9ef2-43f8-806c-2d5656fc1206"
TAG_ORPHAN = "f4ed0b50-1a8c-4b8a-a718-01329a694549"
TAG_EXCLUDED = "e794ab67-aeb0-4385-9a90-db683692b2bb"

# Grants TAG_GRANTED on completion; title is a plain string.
C_GRANTS = {
    "id": "c1",
    "title": "A Simple Task",
    "debugName": "Vaughn_Simple",
    "completionTags": [{"count": 1, "tag": TAG_GRANTED}],
    "availabilityIndex": 0,
}

# Also grants it, but its title is an untranslated localisation key.
C_GRANTS_LOCKEY = {
    "id": "c2",
    "title": "@mission_Vaughn_Alliance_Title",
    "debugName": "Destroying the Alliance",
    "completionTags": [{"tag": TAG_GRANTED}, {"tag": TAG_EXCLUDED}],
    "availabilityIndex": 0,
}

# The contract under test: requires one tag, excludes another, has real flags,
# a standing requirement and a cooldown -- all of #22's symptoms at once.
C_UNDER_TEST = {
    "id": "c3",
    "title": "A Basic Task",
    "debugName": "Vaughn_Basic",
    "availabilityIndex": 0,
    "prerequisites": {
        "completedContractTags": {
            "requiredCountValue": 1,
            "tags": [TAG_GRANTED, TAG_ORPHAN],
            "excludedTags": [TAG_EXCLUDED],
        },
    },
    "canBeShared": True,
    "illegal": True,
    "onceOnly": True,
    "canReacceptAfterAbandoning": True,
    "canReacceptAfterFailing": False,
    "availableInPrison": False,
    "hasPersonalCooldown": True,
    "personalCooldownTime": 45,
    "abandonedCooldownTime": 5,
    "minStanding": {"name": "Jr. Contractor", "minReputation": 800, "rankIndex": 1},
    "maxStanding": {"name": "Elite Contractor", "minReputation": 95250, "rankIndex": 6},
}

# A record whose cooldown time and flag disagree, which 922 real contracts do.
C_TIME_NO_FLAG = {
    "id": "c4",
    "title": "Errand",
    "availabilityIndex": 0,
    "hasPersonalCooldown": False,
    "personalCooldownTime": 15,
}

CONTRACTS = [C_GRANTS, C_GRANTS_LOCKEY, C_UNDER_TEST, C_TIME_NO_FLAG]

# scmdb.net 4.10.1, verbatim: one element, and it is empty.
AVAILABILITY_POOLS = [{}]


def _mgr():
    m = MissionDataManager()
    m._apply_index(index_contracts({
        "contracts": [dict(c) for c in CONTRACTS],
        "availabilityPools": AVAILABILITY_POOLS,
    }), mark_loaded=True)
    return m


# ── (a) the GUID -> mission name join ────────────────────────────────────

class TestCompletionTagIndex:
    def test_maps_tag_to_granting_contracts(self):
        idx = index_completion_tags(CONTRACTS)
        assert len(idx[TAG_GRANTED]) == 2

    def test_a_contract_with_no_completion_tags_is_skipped(self):
        assert index_completion_tags([{"id": "x"}]) == {}

    def test_junk_entries_do_not_raise(self):
        idx = index_completion_tags([{"completionTags": [None, {}, {"tag": "t"}]},
                                     "not a dict"])
        assert list(idx) == ["t"]

    def test_index_is_built_by_index_contracts(self):
        assert TAG_GRANTED in _mgr().completion_tag_contracts


class TestDescribeCompletionTag:
    def test_resolves_to_mission_names_not_a_guid(self):
        got = _mgr().describe_completion_tag(TAG_GRANTED)
        assert got != TAG_GRANTED
        assert "A Simple Task" in got

    def test_localisation_key_title_falls_back_to_debug_name(self):
        got = _mgr().describe_completion_tag(TAG_GRANTED)
        assert "Destroying the Alliance" in got
        assert "@" not in got

    def test_unresolved_tag_keeps_its_guid_rather_than_vanishing(self):
        """4 of 80 real required tags resolve to nothing. Say so, don't hide it."""
        assert _mgr().describe_completion_tag(TAG_ORPHAN) == TAG_ORPHAN

    def test_empty_tag_is_empty(self):
        assert _mgr().get_contracts_granting_tag("") == []

    def test_many_names_are_truncated_with_a_count(self):
        m = _mgr()
        m.completion_tag_contracts = {
            "t": [{"title": f"Mission {i}"} for i in range(6)]}
        got = m.describe_completion_tag("t")
        assert got.endswith("+3 more")


class TestContractDisplayTitle:
    def test_plain_title(self):
        assert _mgr().contract_display_title(C_GRANTS) == "A Simple Task"

    def test_localisation_key_uses_debug_name(self):
        assert _mgr().contract_display_title(C_GRANTS_LOCKEY) == "Destroying the Alliance"

    def test_nothing_at_all_is_a_question_mark(self):
        assert _mgr().contract_display_title({}) == "?"


# ── (b) the flags that were hardcoded "No" ───────────────────────────────

class TestContractAvailability:
    def test_the_pool_read_yields_nothing(self):
        """The defect, pinned: [{}] means every flag reads False."""
        m = _mgr()
        pool = m.get_availability(C_UNDER_TEST["availabilityIndex"])
        assert pool == {}
        assert pool.get("onceOnly", False) is False

    def test_the_record_read_yields_the_truth(self):
        avail = _mgr().get_contract_availability(C_UNDER_TEST)
        assert avail["onceOnly"] is True
        assert avail["canReacceptAfterAbandoning"] is True
        assert avail["canReacceptAfterFailing"] is False
        assert avail["availableInPrison"] is False

    def test_a_populated_pool_is_still_honoured_underneath(self):
        """If scmdb.net ever fills the pools again, they must not be ignored."""
        avail = contract_availability({"availabilityIndex": 1},
                                      [{}, {"availableInPrison": True}])
        assert avail["availableInPrison"] is True

    def test_the_record_wins_over_the_pool(self):
        avail = contract_availability({"availabilityIndex": 0, "onceOnly": True},
                                      [{"onceOnly": False}])
        assert avail["onceOnly"] is True

    def test_a_bad_index_does_not_raise(self):
        assert contract_availability({"availabilityIndex": 99}, [{}]) == {}
        assert contract_availability({"availabilityIndex": None}, [{}]) == {}
        assert contract_availability(None, [{}]) == {}

    def test_absent_field_is_absent_not_false(self):
        """So a future populated pool can still supply what the record omits."""
        assert "onceOnly" not in contract_availability({"id": "x"}, [{}])


class TestAvailabilityFilter:
    def _run(self, m, availability):
        return filter_contracts(m.contracts, FilterState(availability=availability),
                                m.faction_by_guid, m.blueprint_pools,
                                m.availability_pools, m.HIDDEN_LOCATIONS)

    def test_unique_now_matches_the_once_only_contract(self):
        """Through the pool this returned NOTHING for every contract."""
        got = self._run(_mgr(), "unique")
        assert [c["id"] for c in got] == ["c3"]

    def test_repeatable_excludes_it(self):
        got = self._run(_mgr(), "repeatable")
        assert "c3" not in [c["id"] for c in got]
        assert len(got) == len(CONTRACTS) - 1


# ── (c) standing and cooldown ────────────────────────────────────────────

class TestStandingLabel:
    def _label(self, standing):
        from ui.modals.mission_detail import MissionDetailModal
        return MissionDetailModal._standing_label(None, standing)

    def test_name_and_reputation(self):
        assert self._label(C_UNDER_TEST["minStanding"]) == "Jr. Contractor (800 rep)"

    def test_thousands_separator(self):
        assert self._label(C_UNDER_TEST["maxStanding"]) == "Elite Contractor (95,250 rep)"

    def test_zero_reputation_shows_the_name_only(self):
        assert self._label({"name": "Neutral", "minReputation": 0}) == "Neutral"

    def test_localisation_key_name_is_stripped(self):
        assert self._label({"name": "@RepScope_Contractor_Rank1"}) == "Rank1"

    def test_missing_standing_is_empty(self):
        assert self._label(None) == ""
        assert self._label({}) == ""


class TestRequirementsRendering:
    def _texts(self, contract):
        from PySide6.QtWidgets import QApplication, QWidget, QLabel
        from ui.modals.mission_detail import MissionDetailModal
        app = QApplication.instance() or QApplication([])   # noqa: F841
        m = _mgr()
        parent = QWidget()
        modal = MissionDetailModal(parent, contract, m)
        scroll = modal._build_requirements()    # outside __init__'s try/except
        return [lbl.text().strip() for lbl in scroll.findChildren(QLabel)
                if lbl.text().strip()]

    def test_no_raw_guid_survives_in_the_chain_section(self):
        joined = "\n".join(self._texts(C_UNDER_TEST))
        assert "A Simple Task" in joined
        assert f" {TAG_GRANTED} " not in joined

    def test_the_unresolved_tag_is_shown_and_marked(self):
        joined = "\n".join(self._texts(C_UNDER_TEST))
        assert TAG_ORPHAN in joined
        assert "no mission in cache grants this" in joined

    def test_excluded_tags_are_rendered(self):
        joined = "\n".join(self._texts(C_UNDER_TEST))
        assert "BLOCKED BY COMPLETION OF:" in joined

    def test_flags_are_no_longer_all_no(self):
        texts = self._texts(C_UNDER_TEST)
        once_only = texts[texts.index("ONCE ONLY") + 1]
        reaccept = texts[texts.index("RE-ACCEPT AFTER ABANDON") + 1]
        failed = texts[texts.index("RE-ACCEPT AFTER FAIL") + 1]
        assert (once_only, reaccept, failed) == ("Yes", "Yes", "No")

    def test_required_standing_is_rendered(self):
        joined = "\n".join(self._texts(C_UNDER_TEST))
        assert "REQUIRED STANDING" in joined
        assert "Minimum: Jr. Contractor (800 rep)" in joined
        assert "Offered up to: Elite Contractor (95,250 rep)" in joined

    def test_cooldown_is_rendered(self):
        joined = "\n".join(self._texts(C_UNDER_TEST))
        assert "COOLDOWN" in joined
        assert "Personal: 45m" in joined
        assert "After abandoning: 5m" in joined

    def test_a_time_without_the_flag_is_shown_and_qualified(self):
        """922 real contracts carry a time with hasPersonalCooldown off."""
        joined = "\n".join(self._texts(C_TIME_NO_FLAG))
        assert "Personal: 15m" in joined
        assert "personal cooldown flag is off" in joined

    def test_a_contract_with_none_of_it_renders_without_those_sections(self):
        joined = "\n".join(self._texts(C_GRANTS))
        assert "REQUIRED STANDING" not in joined
        assert "COOLDOWN" not in joined
        assert "MISSION CHAIN" not in joined
