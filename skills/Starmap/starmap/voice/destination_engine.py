"""DestinationPhoneticEngine - ported from Elah's WingmanAI set_route skill.

Resolves a spoken destination phrase to one of the Star Citizen
destinations in ``destinations.json`` (with its hand-built alias list) via:

  1. exact match          4. special SC patterns (SPAL-N, CRU L1, Astro 042)
  2. learned aliases      5. fuzzy match over names + aliases (rapidfuzz,
  3. user aliases            ``difflib`` fallback when it is not installed)

Learning / blacklist / alias management carry over unchanged.

Differences from the WingmanAI original:
  * no ``services.file`` dependency - the data dir defaults to the toolbox's
    ``tools/set_route_ai/data`` (so learning stays shared with the Wingman
    skill) and falls back to the copy packaged with the Starmap tool;
  * ``rapidfuzz`` is optional: without it the fuzzy phase uses ``difflib``.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

try:
    from rapidfuzz import process, fuzz  # type: ignore
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False
    import difflib

_TOOLBOX_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
#  voice -> starmap -> Starmap -> skills -> SC_Toolbox_Beta_V1.2


def default_data_dir() -> str:
    """Prefer the live set_route_ai data (shared learning); else packaged copy."""
    live = os.path.join(_TOOLBOX_ROOT, "tools", "set_route_ai", "data")
    if os.path.isdir(live):
        return live
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "data", "set_route")


class DestinationPhoneticEngine:

    def __init__(self, data_dir: Optional[str] = None):
        self.base_path = data_dir or default_data_dir()

        self.destinations_file = os.path.join(self.base_path, "destinations.json")
        self.learning_file = os.path.join(self.base_path, "phonetic_learning.json")
        self.blacklist_file = os.path.join(self.base_path, "blacklist.json")

        self.destinations: Dict[str, Dict] = self._load_json(self.destinations_file)
        self.learning: Dict[str, str] = self._load_json(self.learning_file)
        self.blacklist: Dict[str, bool] = self._load_json(self.blacklist_file)

        # Pre-calculate keys for faster lookup during fuzzy matching
        self.destination_keys = list(self.destinations.keys())

    # ------------------------------------------------------------
    #                        JSON HELPERS
    # ------------------------------------------------------------
    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_json(self, path: str, data: dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------
    #                SIMPLE PHONETIC NORMALIZATION
    # ------------------------------------------------------------
    def normalize(self, text: str) -> str:
        text = text.lower()

        # Remove accents/diacritics
        text = (
            text.replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )

        # Remove non-useful characters (keep only alphanumeric, spaces, hyphens)
        text = re.sub(r"[^a-z0-9\s-]", "", text)

        # Convert multiple spaces to a single space
        text = re.sub(r"\s+", " ", text).strip()

        return text

    # ------------------------------------------------------------
    #                    COMMON ROOT DETECTION
    # ------------------------------------------------------------
    def share_root(self, a: str, b: str) -> bool:
        """Two names are considered equivalent if they share a main root word."""
        na = self.normalize(a)
        nb = self.normalize(b)

        ta = na.split()
        tb = nb.split()

        # At least one word in common implies they share a root
        return any(t in tb for t in ta)

    # ------------------------------------------------------------
    #                        PHASE 1: EXACT MATCH
    # ------------------------------------------------------------
    def find_exact(self, phrase: str) -> Optional[str]:
        key = self.normalize(phrase)
        if key in self.destinations:
            return key

        # Check normalized keys in destinations
        # (Useful if keys in JSON are not perfectly normalized)
        for d in self.destinations:
            if key == self.normalize(d):
                return d
        return None

    # ------------------------------------------------------------
    #                        PHASE 2: LEARNING
    # ------------------------------------------------------------
    def find_learning_alias(self, phrase: str) -> Optional[str]:
        key = self.normalize(phrase)
        return self.learning.get(key)

    # ------------------------------------------------------------
    #                        PHASE 3: USER ALIASES
    # ------------------------------------------------------------
    def find_alias(self, phrase: str) -> Optional[str]:
        n = self.normalize(phrase)

        for dest, data in self.destinations.items():
            aliases = data.get("aliases", [])
            for a in aliases:
                # Aliases are stored normalized, but we normalize again for safety
                if n == self.normalize(a):
                    return dest
        return None

    # ------------------------------------------------------------
    #                 PHASE 4: SPECIFIC SC PATTERNS
    # ------------------------------------------------------------
    def detect_special_patterns(self, phrase: str) -> Optional[str]:
        n = self.normalize(phrase)

        # SPAL-X
        spal = re.search(r"spal\s*-?\s*(\d+)", n)
        if spal:
            num = spal.group(1)
            return f"spal {num}"

        # Lagrange nodes (CRU L1, ARC L2, MIC L5...)
        lagr = re.search(r"(cru|crew|mic|hur|arc)[ -]?l[ -]?(\d+)", n)
        if lagr:
            prefix = lagr.group(1)
            num = lagr.group(2)
            prefix = "cru" if prefix in ["cru", "crew"] else prefix  # crew->cru typo fix
            return f"{prefix} l{num}"

        # Astro 042 (CryAstro)
        astro = re.search(r"(astro|cryastro|crias|crias tro)[ -]?(\d+)", n)
        if astro:
            num = astro.group(2)
            return f"astro {num}"

        return None

    # ------------------------------------------------------------
    #                 PHASE 5: FUZZY MATCHING (EXPANDED)
    # ------------------------------------------------------------
    def fuzzy_match(self, phrase: str) -> List[str]:
        """
        Searches both primary destinations AND aliases.
        Returns a list of primary destination keys (up to 20).
        """
        normalized_phrase = self.normalize(phrase)

        # 1. Build a combined search list and a lookup map:
        #    {alias_name: primary_destination}
        search_choices = list(self.destinations.keys())
        alias_to_dest = {}

        for dest, data in self.destinations.items():
            aliases = data.get("aliases", [])
            for alias in aliases:
                norm_alias = self.normalize(alias)
                search_choices.append(norm_alias)
                alias_to_dest[norm_alias] = dest

        if not search_choices:
            return []

        if _HAS_RAPIDFUZZ:
            # 2. Fuzzy search across all names and aliases
            results = process.extract(
                normalized_phrase,
                search_choices,
                scorer=fuzz.WRatio,
                score_cutoff=60.0,
                limit=20,
                processor=None
            )
        else:
            # difflib fallback - close enough ranking, different scale.
            close = difflib.get_close_matches(normalized_phrase, search_choices,
                                              n=20, cutoff=0.60)
            results = [(c, 0, 0) for c in close]

        if results:
            matches = []
            seen_primary = set()
            for best_match, score, index in results:
                # 3. Resolve the match back to the primary destination
                primary_destination = alias_to_dest.get(best_match, best_match)
                if primary_destination not in seen_primary:
                    matches.append(primary_destination)
                    seen_primary.add(primary_destination)
            return matches

        return []

    # ------------------------------------------------------------
    #                        MAIN FUNCTION
    # ------------------------------------------------------------
    def find_destination(self, phrase: str) -> Tuple[Optional[str], List[str]]:
        """
        Returns:
           (normalized_destination, alternatives[])
        If destination is None and alternatives has >1 item -> Multiple matches found.
        If destination is None and alternatives is empty -> Unknown destination.
        """
        if not phrase:
            return None, []

        normalized_phrase = self.normalize(phrase)

        # 1. Exact Match (User defined name)
        exact = self.find_exact(normalized_phrase)
        if exact:
            return exact, []

        # 2. Learned Automatic Aliases
        alias_learn = self.find_learning_alias(normalized_phrase)
        if alias_learn:
            return alias_learn, []

        # 3. User Defined Aliases
        alias_user = self.find_alias(normalized_phrase)
        if alias_user:
            return alias_user, []

        # 4. Special SC Patterns
        special = self.detect_special_patterns(normalized_phrase)
        if special:
            return special, []

        # 5. Fuzzy Matching
        fuzzy_matches = self.fuzzy_match(normalized_phrase)
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], []
        elif len(fuzzy_matches) > 1:
            return None, fuzzy_matches

        # 6. Nothing found -> Unknown destination
        return None, []

    # ------------------------------------------------------------
    #            LEARNING AND BLACKLIST (ADJUSTED)
    # ------------------------------------------------------------
    def learn(self, phrase: str, destination: str):
        """Adds a smart learned alias."""
        key = self.normalize(phrase)
        dest = self.normalize(destination)

        # Protection: Only link if they share a root word
        if self.share_root(key, dest):
            self.learning[key] = dest
            self._save_json(self.learning_file, self.learning)
            return

        # If they do not share a root, DO NOT learn
        return

    def add_to_blacklist(self, phrase: str):
        key = self.normalize(phrase)
        self.blacklist[key] = True
        self._save_json(self.blacklist_file, self.blacklist)

    # ------------------------------------------------------------
    #                 MANUAL ALIAS MANAGEMENT
    # ------------------------------------------------------------
    def add_alias(self, destination: str, alias: str) -> str:
        """
        Adds an alias to a destination.
        Returns:
          - "destination_not_found"
          - "alias_exists"
          - "ok"
        """
        dest_key = self.normalize(destination)
        if dest_key not in self.destinations:
            return "destination_not_found"

        alias_key = self.normalize(alias)

        aliases: List[str] = self.destinations[dest_key].get("aliases", [])
        # Avoid duplicates via normalization
        if any(self.normalize(a) == alias_key for a in aliases):
            return "alias_exists"

        aliases.append(alias_key)
        self.destinations[dest_key]["aliases"] = aliases
        self._save_json(self.destinations_file, self.destinations)
        return "ok"

    def remove_alias(self, destination: str, alias: str) -> str:
        """
        Removes an alias from a destination.
        Returns:
          - "destination_not_found"
          - "alias_not_found"
          - "ok"
        """
        dest_key = self.normalize(destination)
        if dest_key not in self.destinations:
            return "destination_not_found"

        alias_key = self.normalize(alias)
        aliases: List[str] = self.destinations[dest_key].get("aliases", [])
        new_aliases = [a for a in aliases if self.normalize(a) != alias_key]

        if len(new_aliases) == len(aliases):
            return "alias_not_found"

        self.destinations[dest_key]["aliases"] = new_aliases
        self._save_json(self.destinations_file, self.destinations)
        return "ok"

    def get_aliases(self, destination: str) -> Optional[List[str]]:
        """
        Returns the list of aliases for a destination,
        or None if the destination does not exist.
        """
        dest_key = self.normalize(destination)
        if dest_key not in self.destinations:
            return None
        return self.destinations[dest_key].get("aliases", [])

    def get_all_aliases(self) -> Dict[str, List[str]]:
        """
        Returns a dictionary:
          { destination: [aliases...] } only for those that have aliases.
        """
        result: Dict[str, List[str]] = {}
        for dest, data in self.destinations.items():
            aliases = data.get("aliases", [])
            if aliases:
                result[dest] = aliases
        return result
