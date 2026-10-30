"""
configurable_reward_env.py

Subclasses RedGymEnv (from red_gym_env_v2.py, unmodified) and overrides only
get_game_state_reward() so its multipliers are read from an external JSON
file instead of being hardcoded. This lets the launcher GUI's reward-weight
editor change reward shaping without ever touching red_gym_env_v2.py.

Config file (default: reward_config.json, resolved relative to the process's
working directory) looks like:

{
    "event_multiplier": 4,
    "heal_multiplier": 10,
    "badge_multiplier": 10,
    "explore_multiplier": 0.1,
    "stuck_multiplier": -0.05,
    "stuck_ramp_start_count": 100,
    "stuck_ramp_end_count": 600,
    "battle_multiplier": -1,
    "battle_grace_steps": 30,
    "battle_penalty_per_step": 0.02,
    "battle_max_penalty": 5.0
}

These defaults exactly match the original hardcoded values in
red_gym_env_v2.py, so behavior is identical until you actually change them.
Two exceptions, both additive (no prior behavior to preserve):
  - "stuck" is no longer a hard 0/1 jump at 600 visits to the same tile; it
    now ramps linearly from 0 (at stuck_ramp_start_count visits) up to 1
    (at stuck_ramp_end_count visits), then holds at 1. stuck_multiplier still
    scales the ramped value, so with the defaults above the ramp behaves
    identically to the old step function at both ends -- only the transition
    in between is now smooth instead of abrupt.
  - "battle" is new: it penalizes battles that run long, using the 0xD057
    in-battle flag to count consecutive in-battle steps. No penalty for the
    first battle_grace_steps steps of a battle; after that it grows by
    battle_penalty_per_step per step, capped at battle_max_penalty, then
    scaled by battle_multiplier (negative by default, i.e. a penalty).

The file is re-read automatically whenever its modified-time changes, so
edits made via the GUI take effect at the next reward computation (i.e. by
the next env reset at the latest) without restarting training.
"""

import json
import os
from pathlib import Path

from red_gym_env_v2 import RedGymEnv

DEFAULT_WEIGHTS = {
    "event_multiplier": 4,
    "heal_multiplier": 10,
    "badge_multiplier": 10,
    "explore_multiplier": 0.1,
    "stuck_multiplier": -0.05,
    "level_multiplier": 1,       # re-enables get_levels_reward(); was hardcoded off (commented out) in original
    "op_lvl_multiplier": 0.2,    # re-enables update_max_op_level(); matches original commented value
    "death_multiplier": -0.1,    # re-enables died_count penalty; matches original commented value
    "catch_multiplier": 5,       # new: rewards party-size increases (i.e. catching Pokemon). No canonical
                                  # prior value existed for this -- 5 is a reasonable starting point, set to
                                  # 0 in the reward editor to disable it entirely.

    # -- stuck penalty ramp (replaces the old hard 0/1 cutoff at 600 visits) --
    "stuck_ramp_start_count": 100,   # visits to a tile below this count incur no stuck penalty
    "stuck_ramp_end_count": 600,     # visits at/above this count incur the full stuck penalty
                                      # (same threshold as the original hardcoded cutoff)

    # -- battle duration penalty (new) --
    "battle_multiplier": -1,         # sign lives here, like death_multiplier; keep negative for a penalty
    "battle_grace_steps": 30,        # no penalty for the first N steps of a battle
    "battle_penalty_per_step": 0.02, # penalty added per step beyond the grace period
    "battle_max_penalty": 5.0,       # cap on the per-step battle penalty magnitude

    # -- battle damage reward (new) -- rewards *dealing* damage, not just
    # avoiding the duration penalty above. Uses 0xCFE6/0xCFF4 (enemy cur/max
    # HP) -- commonly cited addresses for this struct, but UNVERIFIED against
    # this specific build. Verify empirically before trusting: watch these
    # two addresses live during a battle and confirm they track the enemy's
    # on-screen HP bar. If wrong, this reward will silently do nothing
    # useful (dealt will compute as ~0) rather than crash.
    "battle_damage_multiplier": 2.0,
    "battle_damage_cap": 10.0,       # caps total battle_damage reward per episode, so grinding
                                      # infinite wild battles can't outweigh actually progressing

    # -- evolution reward (new) -- detects a party mon's species changing at
    # the same slot (base class's read_party() gives species IDs per slot).
    "evolution_multiplier": 8.0,

    # -- map progress reward (new) -- rewards reaching each new essential
    # map location (Viridian City, etc.) using the base class's existing
    # max_map_progress tracker, which was tracked but never rewarded before.
    # Weighted high relative to battle_damage/explore so pushing toward the
    # next city actually outweighs farming battles in the same early area.
    "map_progress_multiplier": 5.0,
}


class ConfigurableRedGymEnv(RedGymEnv):
    def __init__(self, config=None):
        self._reward_config_path = Path(
            (config or {}).get("reward_config_path", "reward_config.json")
        )
        self._reward_weights = dict(DEFAULT_WEIGHTS)
        self._reward_config_mtime = None
        self._maybe_reload_weights()
        super().__init__(config)

    def reset(self, seed=None, options=None):
        self.max_party_size = 0
        self.battle_turn_count = 0
        self._last_battle_penalty = 0.0
        self.battle_damage_rew = 0.0
        self.last_enemy_hp = 1.0
        self.evolution_count = 0
        self.last_party_species = [0, 0, 0, 0, 0, 0]
        return super().reset(seed=seed, options=options if options is not None else {})

    def _maybe_reload_weights(self):
        try:
            mtime = os.path.getmtime(self._reward_config_path)
        except OSError:
            return  # file doesn't exist yet -- keep current/default weights
        if mtime == self._reward_config_mtime:
            return
        try:
            with open(self._reward_config_path, "r") as f:
                loaded = json.load(f)
            weights = dict(DEFAULT_WEIGHTS)
            weights.update({k: v for k, v in loaded.items() if k in DEFAULT_WEIGHTS})
            self._reward_weights = weights
            self._reward_config_mtime = mtime
        except (json.JSONDecodeError, OSError):
            # Leave previous weights in place if the file is mid-write / malformed
            pass

    def update_max_catch_reward(self):
        current_party_size = self.read_m(0xD163)
        self.max_party_size = max(self.max_party_size, current_party_size)
        return self.max_party_size

    def update_evolution_reward(self):
        """Detects evolution as a same-slot species change: slot was already
        occupied (non-zero) last step and now holds a different non-zero
        species. Skips slots that were empty (that's catching, not
        evolving) or that just appeared (party growing, handled by
        update_max_catch_reward instead). A brand new mon appearing in an
        already-occupied slot due to trading/boxing is a rare false
        positive this simple check won't distinguish from evolving -- an
        acceptable tradeoff given the codebase's existing pragmatic use of
        hardcoded memory reads."""
        current_species = self.read_party()
        for i in range(6):
            prev = self.last_party_species[i]
            curr = current_species[i]
            if prev != 0 and curr != 0 and curr != prev:
                self.evolution_count += 1
        self.last_party_species = current_species
        return self.evolution_count

    def get_current_coord_count_reward(self):
        """Overrides the base class's hard 0/1 cutoff at 600 visits with a
        continuous ramp: 0 below stuck_ramp_start_count, linearly increasing
        to 1 at stuck_ramp_end_count, then holding at 1 beyond that. The
        result is still multiplied by stuck_multiplier by the caller, same
        as the base class's binary version was."""
        w = self._reward_weights
        ramp_start = w["stuck_ramp_start_count"]
        ramp_end = w["stuck_ramp_end_count"]

        x_pos, y_pos, map_n = self.get_game_coords()
        coord_string = f"x:{x_pos} y:{y_pos} m:{map_n}"
        count = self.seen_coords.get(coord_string, 0)

        if count <= ramp_start:
            return 0.0
        if count >= ramp_end:
            return 1.0
        return (count - ramp_start) / (ramp_end - ramp_start)

    def read_enemy_hp_fraction(self):
        # UNVERIFIED addresses -- see battle_damage_multiplier comment above.
        cur = self.read_hp(0xCFE6)
        max_hp = max(self.read_hp(0xCFF4), 1)
        return cur / max_hp

    def _update_battle_tracking(self):
        """Tracks consecutive in-battle steps using the 0xD057 battle-state
        flag (0 == not in battle). Resets the counter the moment the flag
        clears, so it always reflects only the current battle's length.
        Also tracks damage dealt to the opponent's active mon this step.
        Called from get_game_state_reward(), which runs once per step after
        the emulator has ticked, so the flag reflects post-action state."""
        currently_in_battle = self.read_m(0xD057) != 0
        if currently_in_battle:
            self.battle_turn_count += 1
            enemy_hp = self.read_enemy_hp_fraction()
            dealt = max(self.last_enemy_hp - enemy_hp, 0)
            self.battle_damage_rew += dealt
            self.last_enemy_hp = enemy_hp
        else:
            self.battle_turn_count = 0
            self.last_enemy_hp = 1.0  # reset baseline for the next battle
        self._last_battle_penalty = self._get_battle_duration_penalty()

    def _get_battle_duration_penalty(self):
        w = self._reward_weights
        grace = w["battle_grace_steps"]
        per_step = w["battle_penalty_per_step"]
        max_penalty = w["battle_max_penalty"]
        overage = max(self.battle_turn_count - grace, 0)
        return min(overage * per_step, max_penalty)

    def get_game_state_reward(self, print_stats=False):
        self._maybe_reload_weights()
        self._update_battle_tracking()
        self.update_map_progress()  # base class updates this *after* reward calc in step(); refresh here so it isn't one step stale
        w = self._reward_weights
        state_scores = {
            "event": self.reward_scale * self.update_max_event_rew() * w["event_multiplier"],
            "heal": self.reward_scale * self.total_healing_rew * w["heal_multiplier"],
            "badge": self.reward_scale * self.get_badges() * w["badge_multiplier"],
            "explore": self.reward_scale * self.explore_weight * len(self.seen_coords) * w["explore_multiplier"],
            "stuck": self.reward_scale * self.get_current_coord_count_reward() * w["stuck_multiplier"],
            "level": self.reward_scale * self.get_levels_reward() * w["level_multiplier"],
            "op_lvl": self.reward_scale * self.update_max_op_level() * w["op_lvl_multiplier"],
            "dead": self.reward_scale * self.died_count * w["death_multiplier"],
            "catch": self.reward_scale * self.update_max_catch_reward() * w["catch_multiplier"],
            "battle": self.reward_scale * self._last_battle_penalty * w["battle_multiplier"],
            "battle_damage": self.reward_scale * min(self.battle_damage_rew, w["battle_damage_cap"]) * w["battle_damage_multiplier"],
            "evolution": self.reward_scale * self.update_evolution_reward() * w["evolution_multiplier"],
            "map_progress": self.reward_scale * self.max_map_progress * w["map_progress_multiplier"],
        }
        return state_scores

    def step(self, action):
        obs, reward, terminated, truncated, info = super().step(action)
        info = dict(info)
        info["reward_components"] = dict(self.progress_reward)
        return obs, reward, terminated, truncated, info
