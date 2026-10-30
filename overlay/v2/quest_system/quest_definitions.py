"""
quest_system/quest_definitions.py

Data-only file: one QuestWaypoint entry per story objective. Adding a new
quest means adding a new entry to QUESTS below -- quest_graph.py and the
env integration never need to change.

Waypoints are ordered by expected progression. QuestGraph.get_active_quest()
returns the FIRST waypoint whose is_active(env) is True, so keep entries in
roughly the order the player will hit them, and make is_active checks
mutually exclusive where possible (usually: "flag A is set AND flag B,
which marks completion of this step, is not set yet").

Exception: an OPTIONAL quest whose is_active condition can stay true for a long
stretch (it only depends on "picked X up, haven't handed X in yet") must go at
the END of the list, not in its story-order slot -- otherwise it shadows every
main-story quest that comes after it. That is why revive_fossil sits in the
post-game block even though the fossil is picked up in Mt Moon.

CORRECTED (previously wrong) -- event flag bit numbering, verified against
the actual pokered disassembly (constants/event_constants.asm) and
red_gym_env_v2.py's event_flags_start = 0xD747:

  wEventFlags is one continuous bit stream starting at 0xD747. Each event
  has a sequential const_value (0, 1, 2, ...); byte address = 0xD747 +
  const_value // 8, and bit-within-byte = const_value % 8 -- standard
  bit-test convention, matching read_bit(addr, bit)'s own bit=0..7 argument
  DIRECTLY. events.json's "0xADDR-N" naming already uses this same N =
  const_value % 8 convention (confirmed via two independent anchors:
  EVENT_FOLLOWED_OAK_INTO_LAB is const_value 0 -> "0xD747-0", and
  EVENT_OAK_GOT_PARCEL/EVENT_GOT_OAKS_PARCEL are const_value 56/57, landing
  on bits 0/1 of byte D74E -> "0xD74E-0"/"0xD74E-1", exactly matching).

  So an events.json idx is ALREADY the correct read_bit() bit argument.
  NO conversion is needed -- a previous version of this file incorrectly
  applied bit = 7 - idx, which silently checked the wrong bit for every
  single quest in this file. If quest progress ever looks "stuck" despite
  real in-game progress, this is the first thing to suspect again.

--- Source of this quest list ---
Every flag address/idx below was pulled directly from YOUR uploaded
events.json (grepped for the actual key strings, not recalled from memory
or a wiki), so the address+bit pairs are correct for this build. Every
target_map_id was pulled directly from YOUR uploaded map_data.json the same
way. What's NOT verified: target_local_coords -- those are still (4, 4)
placeholders everywhere except Oak's Lab/Viridian Mart, same caveat as
before. They still work (potential shaping only needs "closer is better"),
just less tightly tuned right at each quest's finish line until you fill in
real coords.

--- Scope note ---
This covers the MAIN STORY CRITICAL PATH (parcel -> 8 gyms -> Elite Four
rival) plus the major optional-but-expected beats along the way (Bill,
S.S. Anne, Rocket Hideout, Silph Co, Pokemon Tower/Poke Flute, fossil
revival). It deliberately does NOT include every single one of the ~50+
individual trainer-battle flags (e.g. "Beat Silph Co 2F Trainer 0..3", the
per-trainer S.S. Anne/Rocket Hideout flags) -- those are sub-steps within a
single already-covered location (e.g. all Silph Co floor-trainer flags sit
inside the beat_silph_co_giovanni waypoint) and adding a waypoint per
trainer would mean dozens of near-duplicate entries pointing at the same
building for negligible shaping benefit. If you want finer granularity
inside a specific dungeon (e.g. per-floor Silph Co targets instead of one
final-floor target), that's a good candidate for a follow-up pass -- easy
to add as more QuestWaypoint entries here without touching anything else.

Map IDs used below (from map_data.json):
    Oaks Lab=40  Pokemart Viridian=42  Pallet Town=0  Pewter Gym=54
    Cerulean City=3  Cerulean Gym=65  Bills Lab=88  Vermilion Harbor=94
    Vermilion Gym=92  Celadon City=6  Rocket Game Corner=135
    Celadon Gym=134  Pokemon Tower F7=148  Lavender Town=4  Route 12=23
    Fuchsia Gym=157  Saffron Gym=178  Silph Co 11F=235  Mt Moon B1F=61
    Pokemon Lab (Cinnabar fossil lab)=167  Cinnabar Gym=166
    Viridian Gym=45  Indigo Plateau Lobby=174
"""

from dataclasses import dataclass
from typing import Callable

from global_map import local_to_global

OAKS_LAB_MAP_ID = 40
VIRIDIAN_MART_MAP_ID = 42

# UNVERIFIED placeholders -- see module docstring.
OAKS_LAB_DELIVERY_COORDS = (4, 4)
VIRIDIAN_MART_COUNTER_COORDS = (4, 4)
DEFAULT_COORDS = (4, 4)


@dataclass
class QuestWaypoint:
    quest_id: str
    description: str
    is_active: Callable[[object], bool]  # takes the env instance, returns bool
    target_map_id: int
    target_local_coords: tuple  # (x, y) local coords, same frame as get_game_coords()
    reward_scale: float = 1.0

    def target_global_coords(self):
        x, y = self.target_local_coords
        # local_to_global's signature is (row, col, map_n) == (y, x, map_n),
        # matching how RedGymEnv.get_global_coords() calls it.
        return local_to_global(y, x, self.target_map_id)


def _flag(env, addr, idx) -> bool:
    """idx is the events.json index, which is ALREADY the correct bit
    argument for read_bit() -- see the module docstring for the verified
    proof. No conversion needed; this is now a thin pass-through kept only
    so every is_active check below can read the same idx visible in
    events.json without duplicating read_bit() calls everywhere."""
    return env.read_bit(addr, idx)


# ---- individual flag checks, named to match events.json's own labels ----
def _parcel_not_received(env):        return not _flag(env, 0xD74E, 1)   # "Got Oaks Parcel"
def _parcel_received(env):            return _flag(env, 0xD74E, 1)
def _parcel_delivered(env):           return _flag(env, 0xD74E, 0)       # "Oak Got Parcel"
def _battled_rival_oaks_lab(env):     return _flag(env, 0xD74B, 3)
def _got_pokedex(env):                return _flag(env, 0xD74B, 5)
def _got_town_map(env):               return _flag(env, 0xD74A, 0)
def _beat_brock(env):                 return _flag(env, 0xD755, 7)
def _beat_cerulean_rival(env):        return _flag(env, 0xD75A, 0)
def _beat_misty(env):                 return _flag(env, 0xD75E, 7)
def _left_bills_house(env):           return _flag(env, 0xD7F2, 7)
def _ss_anne_left(env):               return _flag(env, 0xD803, 2)
def _beat_lt_surge(env):              return _flag(env, 0xD773, 7)
def _got_bike_voucher(env):           return _flag(env, 0xD771, 1)
def _found_rocket_hideout(env):       return _flag(env, 0xD77E, 1)
def _beat_rocket_hideout_giovanni(env): return _flag(env, 0xD81B, 7)
def _beat_erika(env):                 return _flag(env, 0xD77C, 1)
def _beat_pokemon_tower_rival(env):   return _flag(env, 0xD764, 7)
def _got_poke_flute(env):             return _flag(env, 0xD76C, 0)
def _beat_route12_snorlax(env):       return _flag(env, 0xD7D8, 7)
def _beat_koga(env):                  return _flag(env, 0xD792, 1)
def _beat_sabrina(env):               return _flag(env, 0xD7B3, 1)
def _beat_silph_co_giovanni(env):     return _flag(env, 0xD838, 7)
def _got_dome_fossil(env):            return _flag(env, 0xD7F6, 6)
def _got_helix_fossil(env):           return _flag(env, 0xD7F6, 7)
def _got_any_fossil(env):             return _got_dome_fossil(env) or _got_helix_fossil(env)
def _gave_fossil_to_lab(env):         return _flag(env, 0xD7A3, 0)
def _beat_blaine(env):                return _flag(env, 0xD79A, 1)
def _beat_viridian_gym_giovanni(env): return _flag(env, 0xD751, 1)
def _beat_champion_rival(env):        return _flag(env, 0xD867, 1)
def _beat_route22_rival_1(env):       return _flag(env, 0xD7EB, 5)
def _beat_route22_rival_2(env):       return _flag(env, 0xD7EB, 6)

# --- additions found on a second pass against the full events.json ---
def _got_nugget(env):                 return _flag(env, 0xD7EF, 0)
def _got_bicycle(env):                return _flag(env, 0xD75F, 0)
def _gave_gold_teeth(env):            return _flag(env, 0xD78E, 1)
def _got_hm04(env):                   return _flag(env, 0xD78E, 0)   # Strength, from the Warden after Gold Teeth
def _got_hm02(env):                   return _flag(env, 0xD7E0, 6)   # Fly
def _got_hm03(env):                   return _flag(env, 0xD857, 0)   # Surf
def _beat_lance(env):                 return _flag(env, 0xD866, 6)
def _beat_mewtwo(env):                return _flag(env, 0xD85F, 1)   # post-game, Cerulean Cave


QUESTS = [
    # --- Leg 1: Oak's Parcel ---
    QuestWaypoint(
        quest_id="pickup_oaks_parcel",
        description="Reach the Viridian Mart counter to receive Oak's Parcel",
        is_active=_parcel_not_received,
        target_map_id=VIRIDIAN_MART_MAP_ID,
        target_local_coords=VIRIDIAN_MART_COUNTER_COORDS,
    ),
    QuestWaypoint(
        quest_id="deliver_oaks_parcel",
        description="Return Oak's Parcel to Professor Oak in Pallet Town",
        is_active=lambda env: _parcel_received(env) and not _parcel_delivered(env),
        target_map_id=OAKS_LAB_MAP_ID,
        target_local_coords=OAKS_LAB_DELIVERY_COORDS,
    ),
    QuestWaypoint(
        quest_id="get_pokedex",
        description="Receive the Pokedex from Professor Oak in his lab",
        is_active=lambda env: _parcel_delivered(env) and not _got_pokedex(env),
        target_map_id=OAKS_LAB_MAP_ID,
        target_local_coords=OAKS_LAB_DELIVERY_COORDS,
    ),
    QuestWaypoint(
        quest_id="battle_rival_oaks_lab",
        description="Battle your rival in Oak's Lab after delivering the parcel",
        is_active=lambda env: _got_pokedex(env) and not _battled_rival_oaks_lab(env),
        target_map_id=OAKS_LAB_MAP_ID,
        target_local_coords=OAKS_LAB_DELIVERY_COORDS,
    ),
    QuestWaypoint(
        quest_id="get_town_map",
        description="Get the Town Map in Pallet Town",
        is_active=lambda env: _battled_rival_oaks_lab(env) and not _got_town_map(env),
        target_map_id=0,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_route22_rival_1",
        description="Battle your rival on Route 22, before Viridian Forest",
        is_active=lambda env: _got_town_map(env) and not _beat_route22_rival_1(env),
        target_map_id=33,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 2: Pewter / Brock ---
    QuestWaypoint(
        quest_id="beat_brock",
        description="Defeat Brock at the Pewter Gym",
        is_active=lambda env: _beat_route22_rival_1(env) and not _beat_brock(env),
        target_map_id=54,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 3: fossil (optional, branches off around Mt Moon) ---
    QuestWaypoint(
        quest_id="get_fossil_mt_moon",
        description="Pick up a fossil (Dome or Helix) in Mt Moon",
        is_active=lambda env: _beat_brock(env) and not _got_any_fossil(env) and not _gave_fossil_to_lab(env),
        target_map_id=61,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 4: Cerulean / Misty ---
    QuestWaypoint(
        quest_id="beat_cerulean_rival",
        description="Battle your rival in Cerulean City",
        is_active=lambda env: _beat_brock(env) and not _beat_cerulean_rival(env),
        target_map_id=3,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_misty",
        description="Defeat Misty at the Cerulean Gym",
        is_active=lambda env: _beat_cerulean_rival(env) and not _beat_misty(env),
        target_map_id=65,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 5: Bill / S.S. Anne / Lt. Surge ---
    QuestWaypoint(
        quest_id="help_bill",
        description="Help Bill at his lab on Route 25",
        is_active=lambda env: _beat_misty(env) and not _left_bills_house(env),
        target_map_id=88,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="board_ss_anne",
        description="Board the S.S. Anne in Vermilion City",
        is_active=lambda env: _left_bills_house(env) and not _ss_anne_left(env),
        target_map_id=94,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_lt_surge",
        description="Defeat Lt. Surge at the Vermilion Gym",
        is_active=lambda env: _ss_anne_left(env) and not _beat_lt_surge(env),
        target_map_id=92,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="cross_nugget_bridge",
        description="Defeat the Team Rocket trainers on the Route 24 Nugget Bridge",
        is_active=lambda env: _beat_lt_surge(env) and not _got_nugget(env),
        target_map_id=35,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="get_bike_voucher",
        description="Turn in the Nugget at the Cerulean Pokemart for the Bike Voucher",
        is_active=lambda env: _got_nugget(env) and not _got_bike_voucher(env),
        target_map_id=3,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="get_bicycle",
        description="Redeem the Bike Voucher at the Cerulean Bike Shop",
        is_active=lambda env: _got_bike_voucher(env) and not _got_bicycle(env),
        target_map_id=66,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 6: Rocket Hideout / Erika ---
    QuestWaypoint(
        quest_id="find_rocket_hideout",
        description="Find the entrance to the Rocket Hideout under the Celadon Game Corner",
        is_active=lambda env: _got_bicycle(env) and not _found_rocket_hideout(env),
        target_map_id=6,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_rocket_hideout_giovanni",
        description="Defeat Giovanni in the Rocket Hideout",
        is_active=lambda env: _found_rocket_hideout(env) and not _beat_rocket_hideout_giovanni(env),
        target_map_id=135,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_erika",
        description="Defeat Erika at the Celadon Gym",
        is_active=lambda env: _beat_rocket_hideout_giovanni(env) and not _beat_erika(env),
        target_map_id=134,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 7: Pokemon Tower / Poke Flute / Snorlax ---
    QuestWaypoint(
        quest_id="beat_pokemon_tower_rival",
        description="Battle your rival at the top of Pokemon Tower and rescue Mr. Fuji",
        is_active=lambda env: _beat_erika(env) and not _beat_pokemon_tower_rival(env),
        target_map_id=148,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="get_poke_flute",
        description="Receive the Poke Flute from Mr. Fuji in Lavender Town",
        is_active=lambda env: _beat_pokemon_tower_rival(env) and not _got_poke_flute(env),
        target_map_id=4,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="wake_route12_snorlax",
        description="Use the Poke Flute to clear the sleeping Snorlax on Route 12",
        is_active=lambda env: _got_poke_flute(env) and not _beat_route12_snorlax(env),
        target_map_id=23,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 8: Fuchsia / Saffron ---
    QuestWaypoint(
        quest_id="beat_koga",
        description="Defeat Koga at the Fuchsia Gym",
        is_active=lambda env: _beat_route12_snorlax(env) and not _beat_koga(env),
        target_map_id=157,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Optional side quest: Safari Zone Gold Teeth -> HM04 Strength ---
    # NOTE: events.json has no separate "picked up Gold Teeth" flag, only
    # "Gave Gold Teeth" (handing them to the Warden) and "Got Hm04" (his
    # reward) -- both of which likely flip in the same dialogue. The two
    # waypoints below approximate the two real locations involved (Safari
    # Zone Secret House, then the Warden's House) but can't be gated on a
    # clean intermediate flag the way the rest of the chain is.
    # ALSO NOTE: map_data.json has TWO locations both literally named
    # "Secret House" (ids 188 and 222) with nothing else to disambiguate
    # them. 222 is used here as the closer guess (numerically adjacent to
    # the Safari Zone map ids 217-220) but this is NOT verified -- confirm
    # in-game before trusting this one specifically.
    QuestWaypoint(
        quest_id="find_gold_teeth",
        description="Find the Gold Teeth in the Safari Zone's Secret House",
        is_active=lambda env: _beat_koga(env) and not _gave_gold_teeth(env) and not _got_hm04(env),
        target_map_id=222,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="give_gold_teeth",
        description="Give the Gold Teeth to the Safari Zone Warden for HM04 (Strength)",
        is_active=lambda env: _beat_koga(env) and not _got_hm04(env),
        target_map_id=155,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="get_hm02_fly",
        description="Get HM02 (Fly) from the Bird Keeper on Route 16",
        is_active=lambda env: _got_hm04(env) and not _got_hm02(env),
        target_map_id=27,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_sabrina",
        description="Defeat Sabrina at the Saffron Gym",
        is_active=lambda env: _got_hm02(env) and not _beat_sabrina(env),
        target_map_id=178,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_silph_co_giovanni",
        description="Clear Silph Co and defeat Giovanni at the top",
        is_active=lambda env: _beat_sabrina(env) and not _beat_silph_co_giovanni(env),
        target_map_id=235,
        target_local_coords=DEFAULT_COORDS,
    ),
    # NOTE: location unverified -- HM03 (Surf) is given by one of the Silph
    # Co employees you rescue, at a guard gate south of Fuchsia. "Gate House
    # F1" (184) is a guess among several similarly-named gate maps in
    # map_data.json (184/185/186/187/190/191/195) -- confirm in-game.
    QuestWaypoint(
        quest_id="get_hm03_surf",
        description="Get HM03 (Surf) from the rescued Silph Co. employee",
        is_active=lambda env: _beat_silph_co_giovanni(env) and not _got_hm03(env),
        target_map_id=184,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 9: Cinnabar / Blaine ---
    QuestWaypoint(
        quest_id="beat_blaine",
        description="Defeat Blaine at the Cinnabar Gym",
        is_active=lambda env: _got_hm03(env) and not _beat_blaine(env),
        target_map_id=166,
        target_local_coords=DEFAULT_COORDS,
    ),

    # --- Leg 10: Viridian Gym (final badge) / Elite Four ---
    QuestWaypoint(
        quest_id="beat_viridian_gym_giovanni",
        description="Defeat Giovanni at the Viridian Gym for the 8th badge",
        is_active=lambda env: _beat_blaine(env) and not _beat_viridian_gym_giovanni(env),
        target_map_id=45,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_route22_rival_2",
        description="Battle your rival on Route 22 again, on the way to the Elite Four",
        is_active=lambda env: _beat_viridian_gym_giovanni(env) and not _beat_route22_rival_2(env),
        target_map_id=33,
        target_local_coords=DEFAULT_COORDS,
    ),
    # NOTE: events.json has explicit "Beat X" flags for Lance and the
    # champion-rival battle, but NOT for Lorelei/Bruno/Agatha individually
    # (only "Beat <Name>s Room Trainer 0" sub-trainer flags and "Autowalked
    # Into <Name>s Room" exist for those three). Rather than build waypoints
    # on flags that don't reliably mean "defeated," those three are left
    # untracked as individual quests -- the game enforces their order
    # sequentially anyway, so reaching Lance implies they're already beaten.
    QuestWaypoint(
        quest_id="beat_lance",
        description="Defeat Lance, the last member of the Elite Four",
        is_active=lambda env: _beat_route22_rival_2(env) and not _beat_lance(env),
        target_map_id=113,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_champion_rival",
        description="Challenge the Elite Four and your rival at Indigo Plateau",
        is_active=lambda env: _beat_lance(env) and not _beat_champion_rival(env),
        target_map_id=174,
        target_local_coords=DEFAULT_COORDS,
    ),
    # --- Post-game ---
    # Optional side quest: revive_fossil becomes active the moment a fossil is
    # picked up in Mt Moon, but Cinnabar Island can't be reached until Surf.
    # get_active_quest() returns the FIRST active waypoint, so leaving this in
    # its story-order slot (just after Mt Moon) would shadow every Cerulean ->
    # Silph Co. quest in between and send the agent at an unreachable target.
    # Keeping it last means main-story quests always win, and once they're done
    # the agent can still go hand the fossil in.
    QuestWaypoint(
        quest_id="revive_fossil",
        description="Give the fossil to the lab on Cinnabar Island to revive it (optional)",
        is_active=lambda env: _got_any_fossil(env) and not _gave_fossil_to_lab(env),
        target_map_id=167,
        target_local_coords=DEFAULT_COORDS,
    ),
    QuestWaypoint(
        quest_id="beat_mewtwo",
        description="Defeat Mewtwo in Cerulean Cave (post-game, requires the National Pokedex / all badges)",
        is_active=lambda env: _beat_champion_rival(env) and not _beat_mewtwo(env),
        target_map_id=227,
        target_local_coords=DEFAULT_COORDS,
    ),
]
