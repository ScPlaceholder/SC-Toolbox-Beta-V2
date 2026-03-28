"""Tests for shared.data_enrichment.enrich_component_stats."""

import pytest
from shared.data_enrichment import enrich_component_stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw(
    cls="military",
    grade="A",
    health=500,
    power=120,
    em=30,
    ir=45,
):
    """Return a minimal raw erkul ``entry["data"]`` dict."""
    return {
        "class": cls,
        "grade": grade,
        "health": health,
        "resource": {
            "online": {
                "consumption": {"powerSegment": power},
                "signatureParams": {
                    "em": {"nominalSignature": em},
                    "ir": {"nominalSignature": ir},
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Basic enrichment
# ---------------------------------------------------------------------------

class TestEnrichComponentStats:
    """Core behaviour of enrich_component_stats."""

    def test_all_fields_populated(self):
        stats = {}
        raw = _make_raw()
        result = enrich_component_stats(stats, raw)
        assert result is stats  # mutates in-place and returns same dict
        assert stats["class"] == "military"
        assert stats["grade"] == "A"
        assert stats["hp"] == 500.0
        assert stats["power_draw"] == 120.0
        assert stats["power_max"] == 120.0
        assert stats["em_max"] == 30.0
        assert stats["ir_max"] == 45.0

    def test_setdefault_preserves_existing(self):
        """Values already in stats must NOT be overwritten."""
        stats = {
            "class": "civilian",
            "grade": "C",
            "hp": 999.0,
            "power_draw": 1.0,
            "power_max": 2.0,
            "em_max": 3.0,
            "ir_max": 4.0,
        }
        enrich_component_stats(stats, _make_raw())
        assert stats["class"] == "civilian"
        assert stats["grade"] == "C"
        assert stats["hp"] == 999.0
        assert stats["power_draw"] == 1.0
        assert stats["power_max"] == 2.0
        assert stats["em_max"] == 3.0
        assert stats["ir_max"] == 4.0

    def test_empty_raw_data(self):
        """An empty raw dict should yield safe zero defaults."""
        stats = {}
        enrich_component_stats(stats, {})
        assert stats["class"] == ""
        assert stats["grade"] == "?"
        assert stats["hp"] == 0.0
        assert stats["power_draw"] == 0.0
        assert stats["power_max"] == 0.0
        assert stats["em_max"] == 0.0
        assert stats["ir_max"] == 0.0


# ---------------------------------------------------------------------------
# Health edge cases
# ---------------------------------------------------------------------------

class TestHealthParsing:
    """The health field can be a number, a dict with 'hp', or missing."""

    def test_health_as_plain_number(self):
        stats = {}
        enrich_component_stats(stats, {"health": 250})
        assert stats["hp"] == 250.0

    def test_health_as_dict(self):
        stats = {}
        enrich_component_stats(stats, {"health": {"hp": 750}})
        assert stats["hp"] == 750.0

    def test_health_missing_falls_back_to_hp_key(self):
        stats = {}
        enrich_component_stats(stats, {"hp": 100})
        assert stats["hp"] == 100.0

    def test_health_as_string(self):
        stats = {}
        enrich_component_stats(stats, {"health": "300"})
        assert stats["hp"] == 300.0


# ---------------------------------------------------------------------------
# Power consumption edge cases
# ---------------------------------------------------------------------------

class TestPowerParsing:
    """consumption may be missing, non-dict, or use 'power' instead of 'powerSegment'."""

    def test_power_key_fallback(self):
        raw = {
            "resource": {
                "online": {
                    "consumption": {"power": 55},
                    "signatureParams": {},
                },
            },
        }
        stats = {}
        enrich_component_stats(stats, raw)
        assert stats["power_draw"] == 55.0

    def test_consumption_non_dict(self):
        """If consumption is a non-dict value (corrupt data), treat as zero."""
        raw = {
            "resource": {
                "online": {
                    "consumption": "bad",
                    "signatureParams": {},
                },
            },
        }
        stats = {}
        enrich_component_stats(stats, raw)
        assert stats["power_draw"] == 0.0

    def test_consumption_none(self):
        raw = {
            "resource": {
                "online": {
                    "consumption": None,
                    "signatureParams": {},
                },
            },
        }
        stats = {}
        enrich_component_stats(stats, raw)
        assert stats["power_draw"] == 0.0


# ---------------------------------------------------------------------------
# Signature edge cases
# ---------------------------------------------------------------------------

class TestSignatureParsing:
    """signatureParams or its sub-dicts may be None / missing."""

    def test_signature_params_none(self):
        raw = {
            "resource": {
                "online": {
                    "consumption": {},
                    "signatureParams": None,
                },
            },
        }
        stats = {}
        enrich_component_stats(stats, raw)
        assert stats["em_max"] == 0.0
        assert stats["ir_max"] == 0.0

    def test_em_ir_dicts_none(self):
        raw = {
            "resource": {
                "online": {
                    "consumption": {},
                    "signatureParams": {"em": None, "ir": None},
                },
            },
        }
        stats = {}
        enrich_component_stats(stats, raw)
        assert stats["em_max"] == 0.0
        assert stats["ir_max"] == 0.0
