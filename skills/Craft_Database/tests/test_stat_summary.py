"""Regression tests for issue #20 -- the STAT SUMMARY showed a MEAN.

``_update_stat_label`` averaged the percentage modifiers of every slot
contributing to one stat, so a shield with two slots giving +5% each reported
the crafted total as +5%, and the reported case of +10% and +5% showed "+8%".
The arithmetic now lives in ``domain.models.combine_stat_effects`` and SUMS;
that function's docstring carries the reasoning for sum over product.

Reproduced on the real 4.10.1 payload with ``5CA 'Akura'`` (two slots, each
+5% Max. Shield Strength at quality 750).  The fixtures here mirror it.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from domain.models import Blueprint, QualityEffect, combine_stat_effects  # noqa: E402


def _pct_effect(stat, at_max):
    """A linear percentage effect: no change at quality 0, `at_max`% at 1000."""
    return QualityEffect(stat=stat, quality_min=0, quality_max=1000,
                         modifier_at_min=1.0, modifier_at_max=1.0 + at_max / 100.0)


def _additive_effect(stat, at_max):
    return QualityEffect(stat=stat, quality_min=0, quality_max=1000,
                         modifier_at_min=0.0, modifier_at_max=at_max,
                         additive=True)


# ── combine_stat_effects ─────────────────────────────────────────────────


class TestCombineStatEffects:
    def test_single_slot_is_that_slot(self):
        pct, additive = combine_stat_effects([(_pct_effect("Integrity", 20), 1000)])
        assert pct == pytest.approx(20.0)
        assert additive is False

    def test_two_slots_sum_not_mean(self):
        """The issue: 10 and 5 read '+8%' (the mean). It must read +15%."""
        pairs = [(_pct_effect("Max. Shield Strength", 10), 1000),
                 (_pct_effect("Max. Shield Strength", 5), 1000)]
        pct, _ = combine_stat_effects(pairs)
        assert pct == pytest.approx(15.0)
        assert pct != pytest.approx(7.5)      # the mean
        assert pct != pytest.approx(15.5)     # the multiplicative composition

    def test_akura_case_two_equal_slots(self):
        """5CA 'Akura': two slots at +5% each -> +10%, never +5%."""
        pairs = [(_pct_effect("Max. Shield Strength", 5), 1000)] * 2
        pct, _ = combine_stat_effects(pairs)
        assert pct == pytest.approx(10.0)

    def test_combined_is_never_below_its_largest_part(self):
        """The property that ruled the mean out, checked over many shapes."""
        for contributions in ([10, 5], [5, 5], [20, 1, 1], [-10, -5], [12, 0]):
            pairs = [(_pct_effect("S", v), 1000) for v in contributions]
            pct, _ = combine_stat_effects(pairs)
            assert abs(pct) >= max(abs(v) for v in contributions) - 1e-9

    def test_additive_effects_still_sum(self):
        pairs = [(_additive_effect("Power Pips", 2), 1000),
                 (_additive_effect("Power Pips", 3), 1000)]
        pct, additive = combine_stat_effects(pairs)
        assert pct == pytest.approx(5.0)
        assert additive is True

    def test_mixed_additive_and_pct_is_not_additive(self):
        pairs = [(_additive_effect("Odd", 2), 1000), (_pct_effect("Odd", 10), 1000)]
        _, additive = combine_stat_effects(pairs)
        assert additive is False

    def test_empty_is_zero_and_not_additive(self):
        assert combine_stat_effects([]) == (0.0, False)

    def test_per_slot_quality_is_respected(self):
        """Each pair carries ITS OWN slot's quality, not a shared one."""
        e = _pct_effect("S", 20)
        pct, _ = combine_stat_effects([(e, 1000), (e, 500)])
        assert pct == pytest.approx(30.0)

    def test_no_division_by_the_slot_count(self):
        """Adding a slot that contributes nothing must not dilute the total."""
        pairs = [(_pct_effect("S", 10), 1000)]
        one, _ = combine_stat_effects(pairs)
        pairs.append((_pct_effect("S", 0), 1000))
        two, _ = combine_stat_effects(pairs)
        assert one == pytest.approx(two)


# ── Rendered through the real popup ──────────────────────────────────────


AKURA_LIKE = {
    "id": 1,
    "blueprint_id": "BP_CRAFT_SHLD_TEST_AKURA_LIKE",
    "name": "Test 'Akura'",
    "category": "Ship Components",
    "obtainable": True,
    "ingredients": [
        {"slot": "Field Array", "name": "Beryl", "quantity": 1,
         "options": [{"name": "Beryl", "quantity": 1}],
         "quality_effects": [{"stat": "Max. Shield Strength", "quality_min": 0,
                              "quality_max": 1000, "modifier_at_min": 1.0,
                              "modifier_at_max": 1.1}]},
        {"slot": "Frequency Controller", "name": "Copper", "quantity": 1,
         "options": [{"name": "Copper", "quantity": 1}],
         "quality_effects": [{"stat": "Max. Shield Strength", "quality_min": 0,
                              "quality_max": 1000, "modifier_at_min": 1.0,
                              "modifier_at_max": 1.05}]},
    ],
}


class TestStatSummaryRendering:
    def test_summary_row_is_the_sum_of_the_visible_tags(self):
        """The summary must equal the per-slot tags a reader can see added up."""
        from PySide6.QtWidgets import QApplication
        from ui.detail_panel import BlueprintPopup

        app = QApplication.instance() or QApplication([])   # noqa: F841
        popup = BlueprintPopup(Blueprint.from_dict(AKURA_LIKE))
        try:
            popup._on_quality_changed(1000)
            assert len(popup._stat_labels) == 1
            lbl, stat, qe_list = popup._stat_labels[0]
            assert stat == "Max. Shield Strength"
            assert len(qe_list) == 2
            tags = [round(qe.pct_at(popup._slot_qualities[i])) for qe, i in qe_list]
            assert sorted(tags) == [5, 10]
            assert lbl.text() == "+15%"      # not "+8%", the reported number
        finally:
            popup.deleteLater()
