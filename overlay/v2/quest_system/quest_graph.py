"""
quest_system/quest_graph.py

Generic potential-based shaping reward driven entirely by the QUESTS list
in quest_definitions.py. This file does not need to change when you add,
remove, or reorder quests -- only quest_definitions.py does.

Distance is computed in the GLOBAL coordinate frame that
global_map.local_to_global() already provides -- the same one
RedGymEnv.get_global_coords() and self.explore_map use. Because every map
(including interior maps like Oak's Lab) has real global coordinates in
map_data.json, a single Manhattan-distance calculation handles both
"walk across a town" and "walk from the overworld into a building" --
no separate hand-built map-connectivity graph is needed.

Potential-based shaping: reward at each step is Phi(s') - Phi(s), where
Phi(s) = -distance_to_active_quest_target. This form (Ng, Harada & Russell
1999) is guaranteed not to change the optimal policy relative to the
unshaped reward -- it only changes how quickly the agent finds it. Two
edge cases are handled explicitly since they would otherwise violate that
guarantee in practice:

  1. No active quest (every is_active check is False): Phi = 0, no shaping.
  2. The active quest just changed (completed, or a new one became active):
     the raw Phi jumps discontinuously because the target moved. That one
     transition step is skipped (reward 0) rather than handing out a
     reward spike unrelated to anything the agent did.
"""


class QuestGraph:
    def __init__(self, waypoints):
        self.waypoints = waypoints
        self._prev_potential = None
        self._active_quest_id = None

    def get_active_quest(self, env):
        """Returns the first waypoint whose is_active(env) is True. Order in
        quest_definitions.QUESTS matters -- see the note there."""
        for wp in self.waypoints:
            if wp.is_active(env):
                return wp
        return None

    def compute_potential(self, env) -> float:
        wp = self.get_active_quest(env)
        if wp is None:
            return 0.0
        py, px = env.get_global_coords()
        ty, tx = wp.target_global_coords()
        dist = abs(py - ty) + abs(px - tx)
        return -dist * wp.reward_scale

    def step_reward(self, env) -> float:
        """Call once per env step, AFTER the emulator has ticked for that
        step, so state reflects the action just taken. Returns the shaping
        reward for this step and updates internal potential/quest bookkeeping."""
        wp = self.get_active_quest(env)
        current_quest_id = wp.quest_id if wp else None

        phi_s_prime = self.compute_potential(env)

        if self._prev_potential is None or current_quest_id != self._active_quest_id:
            reward = 0.0  # first call this episode, or quest just transitioned
        else:
            reward = phi_s_prime - self._prev_potential

        self._prev_potential = phi_s_prime
        self._active_quest_id = current_quest_id
        return reward

    def reset(self):
        """Call from the env's reset() so shaping doesn't carry a stale
        potential/quest-id across episode boundaries."""
        self._prev_potential = None
        self._active_quest_id = None

    def active_quest_id(self) -> str:
        return self._active_quest_id or "none"
