"""
quest_reward_env.py

Adds quest-waypoint potential shaping on top of ConfigurableRedGymEnv,
without modifying red_gym_env_v2.py or configurable_reward_env.py.

QuestRewardEnv subclasses ConfigurableRedGymEnv the same way that class
subclasses RedGymEnv: it only overrides get_game_state_reward() (to add one
new "quest" entry to the reward dict), reset() (to reset the quest graph's
internal state each episode), and step() (only when disable_episode_reset
is set -- see below). Everything else -- event/heal/badge/explore/stuck/
battle/etc. -- is inherited unchanged from ConfigurableRedGymEnv.

"Discovered area" bonus note: your existing ConfigurableRedGymEnv already
rewards len(self.seen_coords) growing via the "explore" component (weighted
by explore_multiplier in reward_config.json). That already IS a discovered-
tiles bonus. This file does not duplicate it -- it only adds the
quest-distance signal, which is a different thing (pulls toward a specific
known objective rather than rewarding any new tile).

--- disable_episode_reset ---
red_gym_env_v2.py's step() always returns terminated=False and
truncated=(step_count >= max_steps - 1). SB3's VecEnv auto-resets an env the
step AFTER truncated=True comes back, reloading init_state -- that's the
"resets after some time" behavior. Since red_gym_env_v2.py isn't touched,
AND baseline_fast_v2.py isn't touched either, this is controlled purely via
an environment variable read at env construction time -- nothing has to be
threaded through env_config or argparse in the training script at all.

    POKE_NO_RESET=1   -- never auto-reset at ep_length, current run continues

--- stuck watchdog (only matters when POKE_NO_RESET=1) ---
Removing the periodic reset also removes a safety net it was quietly
providing: if the policy ever wanders into a soft-lock (stuck in a dialogue
box, repeatedly walking into the same wall, cycling a menu), the scheduled
reset used to recover automatically by reloading init_state. With resets
disabled, nothing did that anymore -- exactly the "gets stuck after 1-1.5
hours" symptom. This is fixed by detecting *lack of progress* instead of a
fixed schedule: if len(self.seen_coords) hasn't grown in
POKE_STUCK_RESET_STEPS consecutive steps (default 20000), truncated is
allowed through as True for that one step (same as a normal ep_length
reset would), then POKE_NO_RESET resumes suppressing it afterward. So
"no reset" now means "no reset on a fixed schedule," not "no reset ever" --
recovery from genuine soft-locks still happens, just event-driven instead
of time-driven.

    POKE_STUCK_RESET_STEPS=20000   (default; lower it if 1-1.5h is too long
                                     a soft-lock to tolerate, raise it if
                                     legitimate slow exploration phases are
                                     triggering false-positive resets)

--- curriculum snapshots ---
Two more env vars, same "no code changes elsewhere" approach:

    POKE_CURRICULUM_SNAPSHOTS=1
        The FIRST time any quest_id in QUESTS completes (across all workers,
        all episodes, all runs -- snapshots are never overwritten once
        saved), the PyBoy emulator state at that moment is saved to
        <POKE_CURRICULUM_DIR>/<quest_id>.state, plus an entry in
        <POKE_CURRICULUM_DIR>/manifest.json describing it. Over time this
        builds a library of "mid-story" starting points (post-Brock,
        post-Silph-Co, etc.) so future runs don't have to re-walk the whole
        early game from init_state every single episode.

    POKE_CURRICULUM_DIR=curriculum_snapshots   (default, relative to cwd)
        Where snapshots/manifest.json live. Shared across runs on purpose --
        it's a library, not a per-run artifact -- so it does NOT default to
        session_path the way DiscoveryMemory does.

    POKE_INIT_STATE_OVERRIDE=/absolute/path/to/some.state
        If set, red_gym_env_v2.py loads THIS state file instead of
        env_config["init_state"] ('../init.state', hardcoded in
        baseline_fast_v2.py). This is what lets a curriculum snapshot
        actually become the episode's starting point without touching
        baseline_fast_v2.py or red_gym_env_v2.py: it's applied here, by
        mutating the config dict BEFORE it's ever handed to RedGymEnv's own
        __init__ (which is what reads config["init_state"]).

Usage: point your launcher / training script at QuestRewardEnv instead of
ConfigurableRedGymEnv. Everything else (config dict, reward_config.json,
etc.) works exactly as before.
"""

import json
import os
import time
from pathlib import Path

from configurable_reward_env import ConfigurableRedGymEnv
from quest_system.quest_graph import QuestGraph
from quest_system.quest_definitions import QUESTS

# Scales the whole quest-shaping signal relative to your other reward
# components. Independent of reward_config.json/DEFAULT_WEIGHTS so this file
# stays fully additive -- change this constant (or wire it to your own JSON
# later) rather than touching configurable_reward_env.py.
QUEST_REWARD_MULTIPLIER = 1.0

QUEST_DESCRIPTIONS = {wp.quest_id: wp.description for wp in QUESTS}


class QuestRewardEnv(ConfigurableRedGymEnv):
    def __init__(self, config=None):
        config = dict(config or {})

        override_path = os.environ.get("POKE_INIT_STATE_OVERRIDE", "").strip()
        if override_path:
            config["init_state"] = override_path

        super().__init__(config)
        self.quest_graph = QuestGraph(QUESTS)
        self.disable_episode_reset = os.environ.get("POKE_NO_RESET", "0") == "1"
        self.stuck_reset_steps = int(os.environ.get("POKE_STUCK_RESET_STEPS", "20000"))
        self._last_progress_step = 0
        self._last_seen_coords_len = 0

        self.curriculum_snapshots_enabled = os.environ.get("POKE_CURRICULUM_SNAPSHOTS", "0") == "1"
        self.curriculum_dir = Path(os.environ.get("POKE_CURRICULUM_DIR", "curriculum_snapshots"))
        if self.curriculum_snapshots_enabled:
            self.curriculum_dir.mkdir(parents=True, exist_ok=True)
            self._log_curriculum(
                f"curriculum snapshots ENABLED for this worker, writing to "
                f"{self.curriculum_dir.resolve()} (cwd={Path.cwd()})"
            )

    def reset(self, seed=None, options=None):
        # Order matters: super().reset() calls get_game_state_reward() once.
        # If the quest graph still holds the PREVIOUS episode's active quest at
        # that point, the "quest just completed" check below fires spuriously
        # and (with curriculum snapshots on) would save the freshly-loaded
        # start state under the old quest's name. So clear it first.
        self.quest_graph.reset()
        self.quest_total = 0.0       # running total of quest shaping (see get_game_state_reward)
        self._banked_damage = 0.0    # battle_damage already "locked in" by earlier quest completions
        result = super().reset(seed=seed, options=options)
        self.quest_graph.reset()
        self._last_progress_step = 0
        self._last_seen_coords_len = 0
        return result

    def step(self, action):
        obs, reward, terminated, truncated, info = super().step(action)
        if self.disable_episode_reset:
            current_len = len(self.seen_coords)
            if current_len > self._last_seen_coords_len:
                self._last_seen_coords_len = current_len
                self._last_progress_step = self.step_count

            no_progress_for = self.step_count - self._last_progress_step
            if no_progress_for >= self.stuck_reset_steps:
                # Let this one truncation through -- SB3's VecEnv will
                # auto-reset exactly like a normal ep_length reset would.
                # Don't suppress it: that's precisely what would leave the
                # run permanently soft-locked.
                truncated = True
                self._log_curriculum(
                    f"stuck watchdog: no new tiles for {no_progress_for} steps "
                    f"(limit {self.stuck_reset_steps}) -- forcing a reset at step {self.step_count}"
                )
            else:
                truncated = False
        return obs, reward, terminated, truncated, info

    def _log_curriculum(self, message):
        """Console print is invisible when the GUI launches training without
        an attached console window -- write to a persistent log file too, so
        'nothing got saved' is diagnosable without a terminal."""
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pid={os.getpid()} {message}"
        print(f"\n{line}", flush=True)
        try:
            with open(self.curriculum_dir / "debug.log", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _maybe_save_curriculum_snapshot(self, completed_quest_id):
        state_path = self.curriculum_dir / f"{completed_quest_id}.state"
        if state_path.exists():
            self._log_curriculum(f"skip {completed_quest_id}: snapshot already exists at {state_path}")
            return  # already captured by an earlier episode/worker -- never overwrite

        # Unique tmp name per-process so two SubprocVecEnv workers completing
        # the same quest around the same instant don't stomp each other's
        # partial write; whichever renames first wins, the other's redundant
        # write is harmless (both are valid states at the same quest boundary).
        tmp_path = state_path.with_suffix(f".state.tmp{os.getpid()}")
        try:
            with open(tmp_path, "wb") as f:
                self.pyboy.save_state(f)
            tmp_path.replace(state_path)
        except Exception as e:
            # Broad except on purpose: an OSError-only catch here would let
            # any non-OSError failure from pyboy.save_state() itself (or
            # anything else in this block) escape uncaught, silently killing
            # the SubprocVecEnv worker with no visible sign beyond "nothing
            # ever got saved" -- exactly the symptom being debugged.
            import traceback
            self._log_curriculum(f"SAVE FAILED for {completed_quest_id}: {e}\n{traceback.format_exc()}")
            return

        self._log_curriculum(f"saved {completed_quest_id} -> {state_path}")

        manifest_path = self.curriculum_dir / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, OSError):
                manifest = {}
        manifest[completed_quest_id] = {
            "path": str(state_path.resolve()),
            "description": QUEST_DESCRIPTIONS.get(completed_quest_id, completed_quest_id),
            "saved_at": time.time(),
            "step": self.step_count,
        }
        try:
            manifest_tmp = manifest_path.with_suffix(f".json.tmp{os.getpid()}")
            with open(manifest_tmp, "w") as f:
                json.dump(manifest, f, indent=2)
            manifest_tmp.replace(manifest_path)
        except OSError as e:
            print(f"\ncurriculum manifest update failed: {e}", flush=True)

    def get_game_state_reward(self, print_stats=False):
        # Snapshot the active quest BEFORE this step's quest_graph update, so
        # we can tell a real completion (quest_id changes to a *different*
        # quest, or to none after having been on one) apart from the very
        # first step of the episode, where the active quest goes from "none"
        # to the first waypoint -- that's not a completion, just the quest
        # becoming active, and must not trigger a refill.
        prev_quest_id = self.quest_graph.active_quest_id()

        state_scores = super().get_game_state_reward(print_stats=print_stats)

        quest_reward = self.quest_graph.step_reward(self)
        # RedGymEnv turns the dict's values into a per-step reward by taking
        # the DIFFERENCE of their sum between consecutive steps -- so every
        # entry must be a running TOTAL, not a per-step amount. Putting the
        # raw per-step shaping delta in here would be differenced a second
        # time and cancel itself out.
        self.quest_total += quest_reward * QUEST_REWARD_MULTIPLIER
        state_scores["quest"] = self.reward_scale * self.quest_total

        new_quest_id = self.quest_graph.active_quest_id()

        if self.curriculum_snapshots_enabled and self.step_count % 2000 == 0:
            self._log_curriculum(f"heartbeat: step={self.step_count} active_quest={new_quest_id}")

        quest_just_completed = prev_quest_id != "none" and new_quest_id != prev_quest_id
        w = self._reward_weights
        if quest_just_completed:
            # Refill the battle_damage budget so the per-episode cap doesn't
            # flatten the incentive to fight after real progress. The amount
            # earned so far is BANKED (kept in the running total) rather than
            # discarded -- zeroing it outright would make the total drop and
            # hand the agent a negative reward for finishing a quest.
            self._banked_damage += min(self.battle_damage_rew, w["battle_damage_cap"])
            self.battle_damage_rew = 0.0

            if self.curriculum_snapshots_enabled:
                self._maybe_save_curriculum_snapshot(prev_quest_id)

        state_scores["battle_damage"] = (
            self.reward_scale
            * (self._banked_damage + min(self.battle_damage_rew, w["battle_damage_cap"]))
            * w["battle_damage_multiplier"]
        )

        return state_scores
