""".anim parsing, sampling, and layered channel blending.

===============================================================================
THE .anim FORMAT, as frozen by PICO_CONTRACT.md section 3
===============================================================================

    {
      "name": "idle_breathe",
      "duration": 2.4,
      "loop": true,
      "channel": "BASE",
      "tracks": {
        "spine_upper": [{"t": 0.0, "rot": 0}, {"t": 1.2, "rot": -3},
                        {"t": 2.4, "rot": 0}]
      }
    }

  * A track keys a BONE. Slots are not bones and are not keyframeable.
  * A key carries only the fields it changes: any of `x`, `y`, `rot`, `scale`.
    Absent fields hold, so the four fields are FOUR INDEPENDENT TIMELINES that
    happen to share a list. A track with `x` keys at 0.0/1.0 and a single `rot`
    key at 0.5 is legal and means exactly what it looks like.
  * `t` is seconds from clip start. Interpolation is linear unless a key says
    `"ease": "in" | "out" | "inout"`.
  * Values are DELTAS FROM REST (`x`,`y`,`rot`) except `scale`, which is a
    MULTIPLIER on rest scale. Identity is 0/0/0/1.0.

TWO THINGS THE CONTRACT DOES NOT SAY, DECIDED HERE AND REPORTED AS OPEN
-----------------------------------------------------------------------
1. WHICH SEGMENT AN EASE GOVERNS. A key's `ease` governs the segment LEAVING
   that key, i.e. the interpolation from key i to key i+1 uses key i's ease.
   An ease on the final key of a field is therefore inert, and `Clip.warnings`
   says so rather than staying quiet about it.
2. `scale` AS MULTIPLIER, not additive delta. `"scale": 1.2` is 1.2x rest. The
   alternative reading, rest + 1.2, makes 0.0 the identity and every hand-
   written clip an invitation to forget it.

LOOP SAFETY
    "A looping clip MUST have its last key equal its first on every track, or
    it pops." Enforced at load, per field, naming the offending track AND
    field. A silent pop is the defect people describe as "it feels wrong" and
    nobody can locate.

CHANNELS
    BASE  whole-body       HEAD  head/neck      FACE  eyes/beak/visor
    PROP  what is in prop_anchor

    Each channel owns a DISJOINT bone set, asserted at `ChannelMap`
    construction, and a clip may only key bones inside its own channel's set,
    asserted at `Clip` load. Those two assertions together mean the contract's
    "precedence by specificity, FACE beats HEAD beats BASE" has nothing left to
    adjudicate — blending is a disjoint union. `PRECEDENCE` below is kept as
    the documented tiebreak and is, by construction, unreachable: a
    simplification the disjointness rule already earned.

    `SKIN` is NOT a channel and never produces bone deltas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from .rig import Delta, Pose, RigError, Skeleton

__all__ = [
    "AnimError",
    "ChannelMap",
    "Clip",
    "ClipLibrary",
    "DEFAULT_CHANNEL_BONES",
    "EASES",
    "FIELDS",
    "Key",
    "Layered",
    "PRECEDENCE",
    "Track",
]


class AnimError(Exception):
    """A clip that will not load. Always names the track and field at fault."""


#: The four keyable fields, and the pose component each drives.
FIELDS: tuple[str, ...] = ("x", "y", "rot", "scale")

#: Per-field identity. Note `scale` is 1.0 — see the module docstring.
FIELD_IDENTITY: Mapping[str, float] = {"x": 0.0, "y": 0.0, "rot": 0.0, "scale": 1.0}

EASES: tuple[str, ...] = ("linear", "in", "out", "inout")

#: Documented tiebreak from the contract. Unreachable given disjoint channels
#: plus in-channel clip validation; kept so the order is written down once.
PRECEDENCE: tuple[str, ...] = ("BASE", "HEAD", "FACE", "PROP")


def _ease(kind: str, u: float) -> float:
    """Remap normalised segment position `u` in [0,1]. Stdlib only."""
    if kind == "in":
        return u * u
    if kind == "out":
        return 1.0 - (1.0 - u) * (1.0 - u)
    if kind == "inout":
        return u * u * (3.0 - 2.0 * u)
    return u


class Key(Sequence):
    """One keyed value on one field of one track: `(t, value, ease)`."""

    __slots__ = ("t", "value", "ease")

    def __init__(self, t: float, value: float, ease: str = "linear") -> None:
        self.t = float(t)
        self.value = float(value)
        self.ease = ease

    def __getitem__(self, i):  # type: ignore[override]
        return (self.t, self.value, self.ease)[i]

    def __len__(self) -> int:
        return 3

    def __repr__(self) -> str:
        return "Key(t=%g, %g, %s)" % (self.t, self.value, self.ease)


@dataclass(frozen=True)
class Track:
    """One bone's keys, split into one independent timeline per field."""

    bone: str
    fields: Mapping[str, tuple[Key, ...]]

    def value_at(self, fld: str, t: float) -> float:
        keys = self.fields.get(fld)
        if not keys:
            return FIELD_IDENTITY[fld]
        if t <= keys[0].t:
            return keys[0].value
        if t >= keys[-1].t:
            return keys[-1].value
        lo = 0
        hi = len(keys) - 1
        while hi - lo > 1:  # keys are sorted; binary search
            mid = (lo + hi) // 2
            if keys[mid].t <= t:
                lo = mid
            else:
                hi = mid
        k0, k1 = keys[lo], keys[hi]
        span = k1.t - k0.t
        u = 0.0 if span <= 0.0 else (t - k0.t) / span
        return k0.value + (k1.value - k0.value) * _ease(k0.ease, u)

    def delta_at(self, t: float) -> Delta:
        return Delta(
            dx=self.value_at("x", t),
            dy=self.value_at("y", t),
            drot=self.value_at("rot", t),
            scale=self.value_at("scale", t),
        )


class ChannelMap:
    """Which bones each channel may write. Disjointness is asserted here.

    BASE is the explicit COMPLEMENT of the named channels, so coverage of the
    skeleton is total by construction: every bone belongs to exactly one
    channel and no bone is quietly unanimatable.
    """

    def __init__(
        self,
        skeleton: Skeleton,
        named: Optional[Mapping[str, Iterable[str]]] = None,
        *,
        base: str = "BASE",
    ) -> None:
        spec = {k: tuple(v) for k, v in (named or DEFAULT_CHANNEL_BONES).items()}
        if base in spec:
            raise AnimError(
                "%r is the complement channel and must not be listed "
                "explicitly; it is computed from everything else" % base
            )
        self.skeleton = skeleton
        self.base = base

        for chan, bones in spec.items():
            unknown = sorted(b for b in bones if not skeleton.has_bone(b))
            if unknown:
                raise AnimError(
                    "channel %s names bone(s) not in the skeleton: %s"
                    % (chan, ", ".join(unknown))
                )

        # The contract's assertion, section 4. Pairwise, and it names the
        # overlap rather than saying "sets overlap" — precedence becoming
        # order-dependent is invisible until two clips coincide, so the message
        # has to be enough to act on.
        names = sorted(spec)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                clash = set(spec[a]) & set(spec[b])
                if clash:
                    raise AnimError(
                        "channels %s and %s both claim %s — blending would "
                        "become order-dependent" % (a, b, sorted(clash))
                    )

        claimed = {b for bones in spec.values() for b in bones}
        spec[base] = tuple(n for n in skeleton.bone_names if n not in claimed)
        self.sets: Mapping[str, frozenset[str]] = {
            k: frozenset(v) for k, v in spec.items()
        }

        covered = set().union(*self.sets.values()) if self.sets else set()
        missing = set(skeleton.bone_names) - covered
        if missing:  # impossible while base is the complement; guards a future edit
            raise AnimError(
                "channel map leaves %d bone(s) unanimatable: %s"
                % (len(missing), sorted(missing))
            )

    @property
    def channels(self) -> tuple[str, ...]:
        """Channels in documented precedence order, unknown ones appended."""
        known = [c for c in PRECEDENCE if c in self.sets]
        return tuple(known + sorted(c for c in self.sets if c not in PRECEDENCE))

    def bones(self, channel: str) -> frozenset[str]:
        try:
            return self.sets[channel]
        except KeyError:
            raise AnimError(
                "unknown channel %r — known: %s" % (channel, ", ".join(self.channels))
            ) from None

    def channel_of(self, bone: str) -> str:
        for chan, bones in self.sets.items():
            if bone in bones:
                return chan
        raise AnimError("bone %r belongs to no channel" % bone)

    def __repr__(self) -> str:
        return "<ChannelMap %s>" % ", ".join(
            "%s:%d" % (c, len(self.sets[c])) for c in self.channels
        )


#: HEAD/FACE/PROP as data; BASE is the complement (16 bones in Pico_Master v1).
#:
#: PROP owns `prop_anchor` ONLY. A `hold_mobiglas` clip that also wanted to pose
#: `hand_L`/`hand_R` cannot have them, because `wave` (BASE) uses the hands and
#: the disjointness assertion would fire. That is the assertion doing its job,
#: and it is an OPEN question rather than something to quietly resolve by
#: widening PROP.
DEFAULT_CHANNEL_BONES: Mapping[str, tuple[str, ...]] = {
    "HEAD": ("neck", "head"),
    "FACE": ("eye_L", "eye_R", "beak", "visor"),
    "PROP": ("prop_anchor",),
}


@dataclass(frozen=True)
class Clip:
    """A parsed, validated `.anim`. Sampling is pure: same t, same Pose.

    `sample` always builds a Pose from the keys. It never reads or writes any
    running state, which is why "a loop played twice returns to its exact
    starting pose" is true by construction rather than by care.
    """

    name: str
    duration: float
    loop: bool
    channel: str
    tracks: Mapping[str, Track]
    warnings: tuple[str, ...] = ()
    source: Optional[Path] = None

    # -- time --------------------------------------------------------------
    def local_time(self, t: float) -> float:
        """Map clip-relative `t` into [0, duration]. Wraps when `loop`."""
        if self.loop:
            if self.duration <= 0.0:
                return 0.0
            return t % self.duration
        return min(self.duration, max(0.0, t))

    def sample(self, t: float) -> Pose:
        """Pose at clip-relative time `t`, recomputed from rest every call."""
        lt = self.local_time(t)
        return Pose({name: tr.delta_at(lt) for name, tr in self.tracks.items()})

    def bones(self) -> tuple[str, ...]:
        return tuple(sorted(self.tracks))

    # -- loading -----------------------------------------------------------
    @classmethod
    def from_dict(
        cls,
        d: Mapping,
        *,
        skeleton: Optional[Skeleton] = None,
        channel_map: Optional[ChannelMap] = None,
        source: Optional[Path] = None,
    ) -> "Clip":
        where = ("%s" % source) if source else str(d.get("name", "<dict>"))
        try:
            name = str(d["name"])
            duration = float(d["duration"])
            loop = bool(d["loop"])
            channel = str(d["channel"])
            raw_tracks = d["tracks"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AnimError("%s: malformed clip header: %s" % (where, exc)) from exc

        if duration <= 0.0:
            raise AnimError("%s: duration must be > 0, got %r" % (where, duration))
        if not isinstance(raw_tracks, Mapping) or not raw_tracks:
            raise AnimError("%s: 'tracks' must be a non-empty object" % where)

        if channel_map is not None:
            allowed = channel_map.bones(channel)  # raises on unknown channel
        else:
            allowed = None

        warnings: list[str] = []
        tracks: dict[str, Track] = {}

        for bone, keys in raw_tracks.items():
            if skeleton is not None and not skeleton.has_bone(bone):
                raise AnimError(
                    "%s: track %r is not a bone in the skeleton. Slots are not "
                    "bones and cannot be keyframed." % (where, bone)
                )
            if allowed is not None and bone not in allowed:
                raise AnimError(
                    "%s: clip is channel %s but keys bone %r, which belongs to "
                    "channel %s. A channel writes only its own bone set."
                    % (where, channel, bone,
                       channel_map.channel_of(bone) if channel_map else "?")
                )
            if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)) or not keys:
                raise AnimError("%s: track %r must be a non-empty list of keys"
                                % (where, bone))

            per_field: dict[str, list[Key]] = {f: [] for f in FIELDS}
            for i, k in enumerate(keys):
                if not isinstance(k, Mapping) or "t" not in k:
                    raise AnimError("%s: track %r key %d has no 't'" % (where, bone, i))
                try:
                    t = float(k["t"])
                except (TypeError, ValueError) as exc:
                    raise AnimError("%s: track %r key %d bad 't': %s"
                                    % (where, bone, i, exc)) from exc
                if t < 0.0:
                    raise AnimError("%s: track %r key %d has t=%g < 0"
                                    % (where, bone, i, t))
                if t > duration + 1e-9:
                    raise AnimError(
                        "%s: track %r key %d has t=%g past duration %g — it can "
                        "never be reached" % (where, bone, i, t, duration)
                    )
                ease = str(k.get("ease", "linear"))
                if ease not in EASES:
                    raise AnimError(
                        "%s: track %r key %d ease %r is not one of %s"
                        % (where, bone, i, ease, ", ".join(EASES))
                    )
                present = [f for f in FIELDS if f in k]
                extra = sorted(set(k) - {"t", "ease"} - set(FIELDS))
                if extra:
                    raise AnimError(
                        "%s: track %r key %d has unknown field(s) %s — known "
                        "fields are %s" % (where, bone, i, extra, ", ".join(FIELDS))
                    )
                if not present:
                    raise AnimError(
                        "%s: track %r key %d changes nothing (no %s)"
                        % (where, bone, i, "/".join(FIELDS))
                    )
                for f in present:
                    try:
                        v = float(k[f])
                    except (TypeError, ValueError) as exc:
                        raise AnimError("%s: track %r key %d bad %s: %s"
                                        % (where, bone, i, f, exc)) from exc
                    per_field[f].append(Key(t, v, ease))

            fields: dict[str, tuple[Key, ...]] = {}
            for f, klist in per_field.items():
                if not klist:
                    continue
                klist.sort(key=lambda kk: kk.t)
                ts = [kk.t for kk in klist]
                for j in range(1, len(ts)):
                    if abs(ts[j] - ts[j - 1]) < 1e-12:
                        raise AnimError(
                            "%s: track %r field %s has two keys at t=%g"
                            % (where, bone, f, ts[j])
                        )
                if klist[-1].ease != "linear":
                    warnings.append(
                        "track %r field %s: ease %r on the LAST key is inert — "
                        "an ease governs the segment leaving its key"
                        % (bone, f, klist[-1].ease)
                    )
                fields[f] = tuple(klist)

            tracks[bone] = Track(bone=bone, fields=fields)

        if loop:
            for bone, tr in tracks.items():
                for f, keys_t in tr.fields.items():
                    if abs(keys_t[0].value - keys_t[-1].value) > 1e-9:
                        raise AnimError(
                            "%s: LOOP POP — track %r field %s starts at %g and "
                            "ends at %g. A looping clip must have its last key "
                            "equal its first on every field."
                            % (where, bone, f, keys_t[0].value, keys_t[-1].value)
                        )
                    if keys_t[-1].t < duration - 1e-9:
                        warnings.append(
                            "track %r field %s last key at t=%g holds until the "
                            "clip's duration %g" % (bone, f, keys_t[-1].t, duration)
                        )

        return cls(
            name=name,
            duration=duration,
            loop=loop,
            channel=channel,
            tracks=tracks,
            warnings=tuple(warnings),
            source=source,
        )

    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        skeleton: Optional[Skeleton] = None,
        channel_map: Optional[ChannelMap] = None,
    ) -> "Clip":
        p = Path(path)
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        clip = cls.from_dict(d, skeleton=skeleton, channel_map=channel_map, source=p)
        if clip.name != p.stem:
            raise AnimError(
                "%s: clip's own name is %r but the file stem is %r. Two names "
                "for one clip is a fork waiting to drift." % (p, clip.name, p.stem)
            )
        return clip

    def __repr__(self) -> str:
        return "<Clip %s %s %.3gs%s %d track(s)>" % (
            self.name, self.channel, self.duration,
            " loop" if self.loop else "", len(self.tracks),
        )


DEFAULT_CLIP_DIR = Path(__file__).resolve().parent / "clips"


class ClipLibrary(Mapping):
    """Every `.anim` in a directory, loaded and validated against a skeleton."""

    def __init__(self, clips: Mapping[str, Clip]) -> None:
        self._clips = dict(clips)

    @classmethod
    def load_dir(
        cls,
        path: Optional[Path | str] = None,
        *,
        skeleton: Skeleton,
        channel_map: ChannelMap,
    ) -> "ClipLibrary":
        d = Path(path) if path is not None else DEFAULT_CLIP_DIR
        if not d.is_dir():
            raise AnimError("no clip directory at %s" % d)
        files = sorted(d.glob("*.anim"))
        if not files:
            raise AnimError("no *.anim files in %s" % d)
        return cls({
            f.stem: Clip.load(f, skeleton=skeleton, channel_map=channel_map)
            for f in files
        })

    def __getitem__(self, k: str) -> Clip:
        try:
            return self._clips[k]
        except KeyError:
            raise AnimError(
                "no clip %r — have: %s" % (k, ", ".join(sorted(self._clips)))
            ) from None

    def __iter__(self):
        return iter(sorted(self._clips))

    def __len__(self) -> int:
        return len(self._clips)

    def all_warnings(self) -> tuple[str, ...]:
        return tuple(
            "%s: %s" % (name, w)
            for name in sorted(self._clips)
            for w in self._clips[name].warnings
        )


@dataclass
class _Playing:
    clip: Clip
    started_at: float


class Layered:
    """One clip per channel, blended into a single Pose.

    Blending is a DISJOINT UNION, not a priority resolve. `ChannelMap` proves
    the sets do not overlap and `Clip` refuses a track outside its channel, so
    two channels physically cannot both write a bone. `PRECEDENCE` fixes the
    visit order anyway; because the union is disjoint the order cannot matter,
    which is what "HEAD-then-FACE == FACE-then-HEAD" tests.

    `Pose.merge_disjoint` still raises if it ever sees a double write. That
    branch should be unreachable; it is kept because the alternative to raising
    is silently becoming order-dependent.
    """

    def __init__(self, channel_map: ChannelMap) -> None:
        self.channel_map = channel_map
        self._playing: dict[str, _Playing] = {}

    def play(self, clip: Clip, *, at: float = 0.0) -> None:
        """Start `clip` on its own declared channel, treating `at` as t=0."""
        self.channel_map.bones(clip.channel)  # validates the channel exists
        self._playing[clip.channel] = _Playing(clip=clip, started_at=float(at))

    def stop(self, channel: str) -> None:
        self._playing.pop(channel, None)

    def clear(self) -> None:
        self._playing.clear()

    def active(self) -> Mapping[str, Clip]:
        return {c: p.clip for c, p in self._playing.items()}

    def sample(self, now: float) -> Pose:
        """Blend every active channel at wall time `now`."""
        out = Pose.empty()
        for chan in self.channel_map.channels:
            p = self._playing.get(chan)
            if p is None:
                continue
            try:
                out = out.merge_disjoint(p.clip.sample(now - p.started_at),
                                         who="channel %s" % chan)
            except RigError as exc:  # pragma: no cover - unreachable by design
                raise AnimError(
                    "channel %s collided with an earlier channel: %s" % (chan, exc)
                ) from exc
        return out

    def __repr__(self) -> str:
        return "<Layered %s>" % (
            ", ".join("%s=%s" % (c, p.clip.name) for c, p in sorted(self._playing.items()))
            or "idle"
        )
