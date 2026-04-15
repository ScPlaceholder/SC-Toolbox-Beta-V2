"""Refinery Locations sub-tab.

Shows every in-game refinery grouped by system → parent body, supports
fuzzy search over both refinery names and parent bodies, a "near me"
ranking using the most recent player location detected by the log
scanner, and copy-to-clipboard on click so the user can paste the
name into the in-game MobiGlas.
"""

from __future__ import annotations

from typing import Callable, Optional
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject, Slot, QMetaObject, Q_ARG
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QCheckBox,
)

from shared.qt.theme import P
from shared.qt.search_bar import SCSearchBar

from services.refinery_locations import (
    REFINERIES, RefineryLocation,
    group_by_system_parent, rank_by_proximity,
    lookup_player_location,
)
from services.refinery_yields import RefineryYieldData
from services.refinery_distances import nearest_refineries, fmt_distance
from .refinery_detail_popup import show_refinery_popup


ACCENT = "#33dd88"   # matches the rest of Mining Signals


def _fuzzy_match(needle: str, haystack: str) -> bool:
    """Case-insensitive subsequence match (same rules as Mining Chart)."""
    if not needle:
        return True
    n = "".join(c for c in needle.lower() if c.isalnum())
    if not n:
        return True
    h = haystack.lower()
    i = 0
    for ch in h:
        if ch == n[i]:
            i += 1
            if i == len(n):
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# A single clickable refinery row.  Emits nothing — the caller passes a
# plain callback so we don't need a dedicated signal class.
# ─────────────────────────────────────────────────────────────────────────────


class _RefineryButton(QPushButton):
    """Full-width row button that looks like a list item."""

    def __init__(self, ref: RefineryLocation, on_click: Callable[[RefineryLocation], None]) -> None:
        # The visible label is the canonical name + type; the clipboard
        # value is just the ``name`` so the user can paste it straight
        # into MobiGlas.
        super().__init__(f"{ref.name}   \u2022  {ref.loc_type}")
        self._ref = ref
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(
            f"Click to copy '{ref.name}' to the clipboard "
            "(paste it into the MobiGlas destination selector)"
        )
        self.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                font-family: Consolas, monospace;
                font-size: 9pt; font-weight: bold;
                color: {P.fg}; background: {P.bg_card};
                border: 1px solid {P.border};
                border-left: 3px solid {P.border};
                border-radius: 2px;
                padding: 6px 10px;
                margin-bottom: 2px;
            }}
            QPushButton:hover {{
                background: rgba(51, 221, 136, 0.15);
                color: {P.fg_bright};
                border-left: 3px solid {ACCENT};
            }}
            QPushButton:pressed {{
                background: rgba(51, 221, 136, 0.30);
            }}
        """)
        self.clicked.connect(self._emit)

    def _emit(self) -> None:
        self._on_click(self._ref)


# ─────────────────────────────────────────────────────────────────────────────
# Main widget
# ─────────────────────────────────────────────────────────────────────────────


class RefineryLocationsTab(QWidget):
    """Sub-tab widget listing all refineries with search + near-me."""

    def __init__(
        self,
        parent: QWidget,
        player_location_provider: Callable[[], str],
        status_label: QLabel | None = None,
    ) -> None:
        """Create the tab.

        Parameters
        ----------
        player_location_provider
            Zero-arg callable that returns the most recently observed
            player location (human-readable string).  We call it every
            time the user enables "Near me" or presses refresh.
        status_label
            Optional shared status label (already in the Refinery tab)
            used to flash a short message after clicking a row.  If
            ``None`` the tab shows its own inline status instead.
        """
        super().__init__(parent)
        self._player_location_provider = player_location_provider
        self._shared_status = status_label
        self._yield_data: RefineryYieldData | None = None
        self._filter_text: str = ""
        self._near_me: bool = False
        self._row_container: QWidget | None = None
        self._row_layout: QVBoxLayout | None = None
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._clear_status)

        self._build_ui()
        self._rebuild_rows()

    # ── build ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(6)

        # ── Controls row: search + near-me + refresh ──
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 0, 0, 0)
        ctrl_row.setSpacing(8)

        find_lbl = QLabel("Find:")
        find_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"font-weight: bold; color: {P.fg_dim}; background: transparent;"
        )
        ctrl_row.addWidget(find_lbl)

        self._search = SCSearchBar(
            placeholder="Fuzzy search refineries or parent bodies (hurl1, babb, lorv)...",
            debounce_ms=120, parent=self,
        )
        self._search.setFixedHeight(24)
        self._search.search_changed.connect(self._on_search_changed)
        ctrl_row.addWidget(self._search, 1)

        self._near_cb = QCheckBox("Near me")
        self._near_cb.setCursor(Qt.PointingHandCursor)
        self._near_cb.setToolTip(
            "Sort refineries by proximity to the most recent player "
            "location detected in Game.log"
        )
        self._near_cb.setStyleSheet(
            f"QCheckBox {{ color: {P.fg}; font-family: Consolas, monospace; "
            f"font-size: 8pt; font-weight: bold; background: transparent; }}"
            f"QCheckBox::indicator {{ width: 14px; height: 14px; }}"
            f"QCheckBox::indicator:unchecked {{ "
            f"background: {P.bg_card}; border: 1px solid {P.border}; }}"
            f"QCheckBox::indicator:checked {{ "
            f"background: {ACCENT}; border: 1px solid {ACCENT}; }}"
        )
        self._near_cb.stateChanged.connect(self._on_near_toggled)
        ctrl_row.addWidget(self._near_cb)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.setToolTip(
            "Re-read the current player location and rebuild the list"
        )
        btn_refresh.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas, monospace;
                font-size: 8pt; font-weight: bold;
                color: {ACCENT}; background: transparent;
                border: 1px solid {ACCENT}; border-radius: 3px;
                padding: 3px 10px;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.15); }}
        """)
        btn_refresh.clicked.connect(self._rebuild_rows)
        ctrl_row.addWidget(btn_refresh)

        root.addLayout(ctrl_row)

        # ── Inline status line (used when no shared status label) ──
        self._inline_status = QLabel("")
        self._inline_status.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        root.addWidget(self._inline_status)

        # ── Scrollable body: headers + row buttons ──
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {P.bg_primary};
                border: 1px solid {P.border};
            }}
        """)
        self._row_container = QWidget()
        self._row_container.setStyleSheet(f"background: {P.bg_primary};")
        self._row_layout = QVBoxLayout(self._row_container)
        self._row_layout.setContentsMargins(12, 8, 12, 8)
        self._row_layout.setSpacing(2)
        scroll.setWidget(self._row_container)
        root.addWidget(scroll, 1)

    # ── data helpers ──

    def _current_player_location(self) -> str:
        try:
            return self._player_location_provider() or ""
        except Exception:
            return ""

    def _filtered_refineries(self) -> list[RefineryLocation]:
        needle = self._filter_text
        if not needle:
            return list(REFINERIES)
        out: list[RefineryLocation] = []
        for r in REFINERIES:
            hay = " ".join((r.name, r.parent, r.system, r.loc_type, *r.aliases))
            if _fuzzy_match(needle, hay):
                out.append(r)
        return out

    # ── row rebuild ──

    def _clear_rows(self) -> None:
        if self._row_layout is None:
            return
        while self._row_layout.count() > 0:
            item = self._row_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _rebuild_rows(self) -> None:
        if self._row_layout is None:
            return
        self._clear_rows()

        filtered = self._filtered_refineries()
        if not filtered:
            empty = QLabel("No refineries match your search.")
            empty.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 9pt; "
                f"color: {P.fg_dim}; background: transparent; padding: 16px;"
            )
            empty.setAlignment(Qt.AlignCenter)
            self._row_layout.addWidget(empty)
            self._row_layout.addStretch(1)
            self._update_header_status(0)
            return

        if self._near_me:
            self._populate_near_me(filtered)
        else:
            self._populate_grouped(filtered)

        self._row_layout.addStretch(1)
        self._update_header_status(len(filtered))

    def _populate_grouped(self, filtered: list[RefineryLocation]) -> None:
        assert self._row_layout is not None
        grouped = group_by_system_parent(filtered)
        for system, parents in grouped.items():
            self._row_layout.addWidget(self._system_header(system))
            for parent, refs in parents.items():
                self._row_layout.addWidget(self._parent_header(parent))
                for r in refs:
                    self._row_layout.addWidget(
                        _RefineryButton(r, self._on_row_clicked)
                    )

    def _populate_near_me(self, filtered: list[RefineryLocation]) -> None:
        assert self._row_layout is not None
        player_loc = self._current_player_location()

        # Header showing where the player is.
        if player_loc:
            resolved = lookup_player_location(player_loc)
            if resolved is None:
                hint = f"Player location: {player_loc} (unknown body)"
            else:
                sys_, parent, _loc_type = resolved
                hint = f"Player location: {player_loc} ({parent}, {sys_})"
        else:
            hint = (
                "No player location detected yet — launch SC so "
                "Game.log records a RequestLocationInventory event"
            )
        hdr = QLabel(hint)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent; padding: 2px 4px;"
        )
        self._row_layout.addWidget(hdr)

        if not player_loc:
            return

        # Show a loading indicator while distances are fetched from
        # the UEX API (up to 17 HTTP requests on a cold cache).
        loading = QLabel("Calculating distances...")
        loading.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"color: {P.fg_dim}; background: transparent; padding: 8px;"
        )
        self._row_layout.addWidget(loading)

        # Run the distance fetch on a background thread so the UI
        # doesn't freeze.
        filter_names = {r.name for r in filtered}

        def _fetch():
            return nearest_refineries(player_loc, n=3)

        def _done(results):
            # Filter to only refineries matching the search, then
            # rebuild the rows on the main thread.
            results = [(ref, d) for ref, d in results if ref.name in filter_names]
            QMetaObject.invokeMethod(
                self, "_show_near_me_results",
                Qt.QueuedConnection,
                Q_ARG("QVariant", results),
            )

        def _bg():
            try:
                res = _fetch()
                _done(res)
            except Exception:
                pass

        threading.Thread(target=_bg, daemon=True).start()

    @Slot("QVariant")
    def _show_near_me_results(self, results) -> None:
        """Called on the main thread once distance data is ready."""
        if not self._near_me or self._row_layout is None:
            return

        # Remove the "Calculating distances..." label (last widget before stretch)
        for i in range(self._row_layout.count()):
            item = self._row_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if isinstance(w, QLabel) and "Calculating" in (w.text() or ""):
                    self._row_layout.removeWidget(w)
                    w.deleteLater()
                    break

        if not results:
            empty = QLabel("No matching refineries found nearby.")
            empty.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 9pt; "
                f"color: {P.fg_dim}; background: transparent; padding: 8px;"
            )
            self._row_layout.insertWidget(self._row_layout.count() - 1, empty)
            return

        self._row_layout.insertWidget(
            self._row_layout.count() - 1,
            self._system_header("CLOSEST REFINERIES"),
        )

        for ref, dist_gm in results:
            if dist_gm is not None:
                dist_str = fmt_distance(dist_gm)
                btn = _RefineryButton(ref, self._on_row_clicked)
                btn.setText(f"{ref.name}   \u2022  {dist_str}")
                btn.setToolTip(
                    f"{ref.name}\n{ref.system} \u00b7 {ref.loc_type}\n"
                    f"Distance: {dist_str}\n\n"
                    f"Click to view yield details"
                )
            else:
                btn = _RefineryButton(ref, self._on_row_clicked)
                btn.setText(f"{ref.name}   \u2022  distance unknown")
            self._row_layout.insertWidget(
                self._row_layout.count() - 1, btn
            )

    def _system_header(self, text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; "
            f"font-size: 10pt; font-weight: bold; "
            f"color: {ACCENT}; background: transparent; "
            f"padding: 10px 2px 4px 2px;"
        )
        return lbl

    def _parent_header(self, text: str) -> QLabel:
        lbl = QLabel(f"  \u25cf  {text}")
        lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {P.fg_bright}; background: transparent; "
            f"padding: 4px 2px 2px 2px;"
        )
        return lbl

    # ── events ──

    def _on_search_changed(self, text: str) -> None:
        self._filter_text = (text or "").strip()
        self._rebuild_rows()

    def _on_near_toggled(self, state: int) -> None:
        # PySide6 stateChanged emits int(2) but Qt.Checked is an enum;
        # ``int == enum`` returns False in strict-enum builds. Use bool.
        self._near_me = bool(state)
        self._rebuild_rows()

    def _on_row_clicked(self, ref: RefineryLocation) -> None:
        """Show a detail popup with yield values and a copy button."""
        # Find the button widget that was clicked so we can anchor
        # the popup next to it.
        sender = self.sender()
        anchor = sender if isinstance(sender, QWidget) else None
        show_refinery_popup(
            parent=self.window() or self,
            ref=ref,
            yield_data=self._yield_data,
            anchor=anchor,
        )

    def _show_status(self, msg: str) -> None:
        if self._shared_status is not None:
            self._shared_status.setText(msg)
        self._inline_status.setText(msg)
        self._flash_timer.start(3000)

    def _clear_status(self) -> None:
        self._inline_status.setText("")
        if self._shared_status is not None:
            # Don't clobber other status messages that were written
            # after ours — only clear if it still matches.
            if "Copied" in (self._shared_status.text() or ""):
                self._shared_status.setText("")

    def _update_header_status(self, count: int) -> None:
        if self._filter_text or self._near_me:
            suffix = ""
            if self._near_me:
                loc = self._current_player_location()
                if loc:
                    suffix = f"  \u00b7  near {loc}"
                else:
                    suffix = "  \u00b7  no player location yet"
            self._inline_status.setText(
                f"{count} refineries{suffix}"
            )
        else:
            self._inline_status.setText(
                f"{len(REFINERIES)} refineries total"
            )

    # ── public API ──

    def set_yield_data(self, data: RefineryYieldData | None) -> None:
        """Provide refinery yield data so the detail popup can show
        per-mineral bonuses/penalties when a location is clicked."""
        self._yield_data = data

    def notify_player_location_changed(self) -> None:
        """Called when the parent app sees a new player location.

        Rebuilds the list only when "Near me" is active, since plain
        alphabetical order doesn't depend on player location.
        """
        if self._near_me:
            self._rebuild_rows()
