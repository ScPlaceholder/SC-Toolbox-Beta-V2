"""test_death_detection.py - local-player death / respawn detection in EventParser.

Every log line below is copied VERBATIM from a real Game.log / logbackups (source file noted above
each constant). Near-miss lines (an NPC the pilot killed, another player's incap, a kill volume, another
player's corpse, a login for a different character) must NOT produce `incapacitated` / `player_respawned`.

Run: python test_death_detection.py      (also collects under pytest)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from event_parser import EventParser  # noqa: E402

# Game.log
LOGIN_CURRENT = '<2026-09-21T23:36:06.166Z> [Notice] <AccountLoginCharacterStatus_Character> Character: createdAt 1787760004563 - updatedAt 1787760004968 - geid 204715025323 - accountId 5154538 - name ProjectGegnome - state STATE_CURRENT [Team_GameServices][Login]'

# Game.log
EMERGENCY_SHUD = '<2026-09-22T01:01:42.049Z> [Notice] <SHUDEvent_OnNotification> Added notification "Standby, Local Emergency Services Are En Route: " [212] to queue. New queue size: 1, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] [Team_CoreGameplayFeatures][Missions][Comms]'

# Game.log
EMERGENCY_RAW = '<2026-09-22T01:01:42.049Z>    "Standby, Local Emergency Services Are En Route: " [212]'

# Game.log
EMERGENCY_RAW2 = '<2026-09-22T01:01:44.448Z>    "Standby, Local Emergency Services Are En Route: " [212]'

# Game Build(12568521) 07 Sep 26 (19 49 05).log
UNBIND = '<2026-09-08T01:02:28.336Z> [Notice] <Recv Unbind Batch Add Player> Unbind Batch Add sent for player in batch 10448 ViewName="Replicant 35.245.92.221:64294" connection={3, 2} node_id=d44d340a-6e7d-62ca-a8e4-3b806a490386 playerGEID=204715025323 sessionId="deceb666e8e41bdbc76603c2aa13884b"  entityId=204715025323, className="Player", classCrc=2961494058, flags=2225082880, attachmentType=1186995890, parentEntityId=820916417146 [Team_Network][Network][Replication][EntityStreaming][Protocol][Send]'

# Game Build(12568521) 07 Sep 26 (19 49 05).log
BIND = '<2026-09-08T01:02:28.339Z> [Notice] <Recv Bind Batch Add Player> Bind Batch Add received for player in batch 10449 ViewName="Replicant 35.245.92.221:64294" connection={3, 2} node_id=d44d340a-6e7d-62ca-a8e4-3b806a490386 playerGEID=204715025323 sessionId="deceb666e8e41bdbc76603c2aa13884b"  entityId=204715025323, className="Player", classCrc=2961494058, flags=2225082880, attachmentType=649247729, parentEntityId=820916417610 [Team_Network][Network][Replication][EntityStreaming][Protocol][Receive]'

# Game Build(12568521) 07 Sep 26 (19 49 05).log
ATTACH = '<2026-09-08T00:59:58.217Z> [Notice] <AttachmentReceived> Player[ProjectGegnome] Attachment[volt_sniper_energy_01_store01_820459722322, volt_sniper_energy_01_store01, 820459722322] Status[persistent] Port[wep_stocked_3] Elapsed[36.238602] [Team_CoreGameplayFeatures][Inventory]'

# Game Build(10007308) 09 Aug 25 (19 06 37).log
LOGIN_2025 = '<2025-08-09T23:07:10.989Z> [Notice] <AccountLoginCharacterStatus_Character> Character: createdAt 1752784876916 - updatedAt 1752784878906 - geid 201926433820 - accountId 5154538 - name ProjectGegnome - state STATE_CURRENT [Team_GameServices][Login]'

# Game Build(10007308) 09 Aug 25 (19 06 37).log
ACTOR_DEATH_LOCAL = "<2025-08-10T00:08:49.472Z> [Notice] <Actor Death> CActor::Kill: 'ProjectGegnome' [201926433820] in zone 'pyro1' killed by 'Seelenloser' [1507429576042] using 'behr_lmg_ballistic_01_5403148893548' [Class behr_lmg_ballistic_01] with damage type 'Bullet' from direction x: -0.631338, y: 0.317006, z: 0.707757 [Team_ActorTech][Actor]"

# Game Build(10007308) 09 Aug 25 (19 06 37).log
ACTOR_DEATH_NPC = "<2025-08-10T00:21:42.485Z> [Notice] <Actor Death> CActor::Kill: 'PU_Human_Enemy_GroundCombat_NPC_ASD_techie_5403148870160' [5403148870160] in zone 'asd_labresearch_int_01a' killed by 'ProjectGegnome' [201926433820] using 'behr_shotgun_ballistic_01_5403148899229' [Class behr_shotgun_ballistic_01] with damage type 'Bullet' from direction x: 0.981064, y: 0.193126, z: -0.014685 [Team_ActorTech][Actor]"

# Game Build(10098575) 16 Aug 25 (21 33 32).log
INCAP_LOCAL = '<2025-08-17T01:44:09.105Z> Logged an incap.! nickname: ProjectGegnome, causes: [RadiationDamageHigh (55.673298 damage)]'

# Game Build(10188864) 06 Sep 25 (13 51 04).log
INCAP_OTHER = '<2025-09-07T01:34:16.364Z> Logged an incap.! nickname: Lumenesque, causes: [RadiationDamageHigh (140.000000 damage)]'

# Game Build(12061511) 28 Jun 26 (19 21 14).log
ASDEAD_LOCAL = "<2026-06-28T23:27:31.562Z> [Notice] <[ActorState] Dead> [ACTOR STATE][CSCActorControlStateDead::PrePhysicsUpdate] Actor 'ProjectGegnome' [204715025323] ejected from zone 'DRAK_Vulture_624220273265' [624220273265] to zone 'OOC_Stanton_1a_Ariel' [524998364020] due to previous zone being in a destroyed vehicle with detached interior. [Team_ActorFeatures][Actor]"

# Game Build(12568521) 07 Sep 26 (19 49 05).log
KILL_VOLUME = '<2026-09-07T23:57:08.130Z> [Notice] <Kill Volume> Kill volume destroyed Entity: ID: 19507 Class: GeomEntity_NoPhysics Name: HoloVehicleShield VolumeID: 820916417225 VolumeName: KillVolume_Invert Inverted: True [Team_CoreGameplayFeatures][Entity]'

# Game Build(10007308) 09 Aug 25 (19 06 37).log
CORPSE_OTHER = "<2025-08-10T00:06:39.897Z> [Notice] <[ActorState] Corpse> [ACTOR STATE][SSCActorStateCVars::LogCorpse] Player 'Jon_Forker' <remote client>: Running corpsify for corpse. [Team_ActorFeatures][Actor]"


def _run(*lines):
    p = EventParser()
    got = []
    p.subscribe(got.append)
    for ln in lines:
        p.on_raw_line(ln)
    return got


def _of(events, et):
    return [e for e in events if e.event_type == et]


# -- current builds (Sep 2026) ---------------------------------------------------------------------------------
def test_emergency_notice_is_one_incapacitation():
    ev = _run(LOGIN_CURRENT, EMERGENCY_SHUD, EMERGENCY_RAW, EMERGENCY_RAW2)
    inc = _of(ev, "incapacitated")
    assert len(inc) == 1, [e.to_dict() for e in inc]
    assert inc[0].data == {"state": "downed", "source": "emergency_services"}
    assert inc[0].category == "ACTOR_STATE"
    assert len(_of(ev, "emergency_services")) == 3      # existing classification untouched


def test_unbind_is_respawn_for_local_geid():
    ev = _run(LOGIN_CURRENT, UNBIND)
    rs = _of(ev, "player_respawned")
    assert len(rs) == 1
    assert rs[0].data == {"player_geid": "204715025323", "death_observed": False}
    assert not _of(ev, "incapacitated")


def test_respawn_after_seen_death_is_marked_and_rearms():
    ev = _run(LOGIN_CURRENT, EMERGENCY_SHUD, UNBIND, EMERGENCY_RAW2)
    assert [e.event_type for e in ev if e.event_type in ("incapacitated", "player_respawned")] == [
        "incapacitated", "player_respawned", "incapacitated"]
    assert _of(ev, "player_respawned")[0].data["death_observed"] is True


def test_bind_line_alone_is_not_respawn():
    assert not _of(_run(LOGIN_CURRENT, BIND), "player_respawned")


def test_unbind_for_another_characters_geid_is_ignored():
    # LOGIN_2025 is a real login for geid 201926433820; the unbind is for 204715025323.
    assert not _of(_run(LOGIN_2025, UNBIND), "player_respawned")


def test_kill_volume_is_not_a_death():
    ev = _run(LOGIN_CURRENT, KILL_VOLUME)
    assert not _of(ev, "incapacitated")


# -- legacy builds (still real lines; they fire if SC writes them again) ---------------------------------------
def test_actor_death_of_local_player():
    inc = _of(_run(LOGIN_2025, ACTOR_DEATH_LOCAL), "incapacitated")
    assert len(inc) == 1
    assert inc[0].data == {"state": "dead", "source": "actor_death", "zone": "pyro1", "cause": "Bullet",
                           "killer": "Seelenloser", "weapon_class": "behr_lmg_ballistic_01"}


def test_actor_death_of_npc_killed_by_player_is_not_incapacitated():
    assert not _of(_run(LOGIN_2025, ACTOR_DEATH_NPC), "incapacitated")


def test_incap_log_local_player():
    inc = _of(_run(LOGIN_2025, INCAP_LOCAL), "incapacitated")
    assert len(inc) == 1
    assert inc[0].data == {"state": "downed", "source": "incap_log",
                           "causes": ["RadiationDamageHigh"], "cause": "RadiationDamageHigh"}


def test_incap_log_other_player_is_ignored():
    assert not _of(_run(LOGIN_2025, INCAP_OTHER), "incapacitated")


def test_actor_state_dead_local_player():
    inc = _of(_run(LOGIN_CURRENT, ASDEAD_LOCAL), "incapacitated")
    assert len(inc) == 1
    d = inc[0].data
    assert d["state"] == "dead" and d["source"] == "actor_state_dead" and d["cause"] == "vehicle_destroyed"
    assert d["zone"].startswith("DRAK_Vulture_") and d["location_raw"] == "OOC_Stanton_1a_Ariel"


def test_other_players_corpse_is_not_a_death():
    assert not _of(_run(LOGIN_2025, CORPSE_OTHER), "incapacitated")


# -- identity ------------------------------------------------------------------------------------------------------
def test_identity_learned_mid_session_from_attachment_line():
    # companion attached after login: no login line, but AttachmentReceived names the local player
    assert len(_of(_run(ATTACH, INCAP_LOCAL), "incapacitated")) == 1


def test_no_identity_means_no_guess():
    assert not _of(_run(ACTOR_DEATH_LOCAL, INCAP_LOCAL), "incapacitated")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
