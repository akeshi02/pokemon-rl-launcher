"""
train_cli.py -- command-line training entry point for PokemonRedExperiments (v2).

A drop-in alternative to the upstream `baseline_fast_v2.py` that:
  * takes its settings from CLI flags instead of requiring edits to the script
  * trains on QuestRewardEnv (configurable rewards + quest shaping) instead of
    the plain RedGymEnv
  * writes the status files the launcher GUI reads
  * saves a checkpoint on Ctrl+C / Ctrl+Break / SIGTERM instead of losing it

It does NOT modify any upstream file. It lives next to `red_gym_env_v2.py`
inside the upstream `v2/` folder (setup_overlay.py puts it there) and imports
from it.

Examples
--------
    python train_cli.py --headless --num-cpu 8
    python train_cli.py --num-cpu 4                      # 4 visible emulator windows
    python train_cli.py --headless --num-cpu 8 --stream  # also broadcast to the public map
    echo runs/poke_1234_steps | python train_cli.py ...  # resume (checkpoint path on stdin)

Environment variables read by quest_reward_env.py (all optional):
    POKE_NO_RESET=1               never reset on a fixed schedule
    POKE_STUCK_RESET_STEPS=20000  soft-lock watchdog used with POKE_NO_RESET
    POKE_CURRICULUM_SNAPSHOTS=1   save a .state the first time each quest completes
    POKE_CURRICULUM_DIR=...       where snapshots go
    POKE_INIT_STATE_OVERRIDE=...  start every episode from this .state file
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from os.path import exists
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROM_SHA1 = "ea9bcae617fdf159b045185467ae58b2e4a48b9a"  # from the upstream README


def parse_args():
    p = argparse.ArgumentParser(description="Train a PPO agent on Pokemon Red (quest-shaped rewards).")
    p.add_argument("--num-cpu", type=int, default=4,
                   help="Number of parallel emulator processes (also episodes per iteration). Default 4.")
    p.add_argument("--ep-length", type=int, default=2048 * 80,
                   help="Max steps per episode per environment. Default 163840.")
    p.add_argument("--explore-weight", type=float, default=0.25, help="Scales the exploration reward.")
    p.add_argument("--reward-scale", type=float, default=0.5, help="Scales every reward component.")
    p.add_argument("--headless", action="store_true",
                   help="Run emulators without windows (much faster). Omit to show one window per env.")
    p.add_argument("--stream", action="store_true",
                   help="Broadcast agents to the shared public map (upstream StreamWrapper). Off by default.")
    p.add_argument("--stream-user", default="pokemon-rl-launcher", help="Name shown on the public map.")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--device", default="auto", help="'auto', 'cpu' or 'cuda'.")
    p.add_argument("--sess-dir", default="runs", help="Folder for checkpoints and logs. Default 'runs'.")
    p.add_argument("--reward-config", default=str(HERE / "reward_config.json"),
                   help="Reward weights JSON (hot-reloaded while training).")
    return p.parse_args()


def preflight(args):
    """Fail early with a readable message instead of a traceback from a worker."""
    problems = []
    for needed in ("events.json", "global_map.py", "map_data.json", "red_gym_env_v2.py"):
        if not (HERE / needed).exists():
            problems.append(f"{needed} not found next to train_cli.py -- is this the upstream v2/ folder?")
    rom = HERE.parent / "PokemonRed.gb"
    if not rom.exists():
        problems.append(f"ROM not found at {rom}. Supply your own legally obtained Pokemon Red ROM "
                        f"and name it PokemonRed.gb.")
    else:
        sha1 = hashlib.sha1(rom.read_bytes()).hexdigest()
        if sha1 != ROM_SHA1:
            print(f"WARNING: PokemonRed.gb SHA-1 is {sha1}\n         expected {ROM_SHA1}. "
                  f"Memory addresses may not line up with a different ROM revision.")
    if not (HERE.parent / "init.state").exists():
        problems.append(f"init.state not found at {HERE.parent / 'init.state'}.")
    cpus = os.cpu_count() or 1
    if args.num_cpu > cpus:
        print(f"WARNING: --num-cpu {args.num_cpu} is more than the {cpus} logical CPUs on this machine.")
    if args.num_cpu < 1:
        problems.append("--num-cpu must be at least 1.")
    if problems:
        print("\nCannot start:")
        for pr in problems:
            print(f"  - {pr}")
        sys.exit(1)


def make_env(rank, env_conf, seed, stream, stream_user):
    # Imports live inside so SubprocVecEnv workers only load what they need.
    def _init():
        # The parent handles Ctrl+C / Ctrl+Break and shuts workers down cleanly.
        # Without this, the same console signal also kills every worker mid-save.
        for name in ("SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, signal.SIG_IGN)
                except (ValueError, OSError):
                    pass
        from quest_reward_env import QuestRewardEnv
        env = QuestRewardEnv(env_conf)
        if stream:
            from stream_agent_wrapper import StreamWrapper
            env = StreamWrapper(env, stream_metadata={
                "user": stream_user, "env_id": rank, "color": "#447799", "extra": "",
            })
        env.reset(seed=seed + rank)
        return env
    return _init


def build_callbacks(sess_path, ep_length):
    import numpy as np
    from stable_baselines3.common.callbacks import BaseCallback, CheckpointCallback, CallbackList
    from tensorboard_callback import TensorboardCallback

    class StatusCallback(BaseCallback):
        """Writes <sess>/training_status.json every few seconds for the launcher GUI."""

        def __init__(self, path, interval_s=5.0):
            super().__init__()
            self.path = Path(path)
            self.interval_s = interval_s
            self._last = 0.0

        def _on_step(self) -> bool:
            now = time.time()
            if now - self._last < self.interval_s:
                return True
            self._last = now
            try:
                totals = self.training_env.get_attr("total_reward")
                comps = self.training_env.get_attr("progress_reward")
                payload = {
                    "steps": int(self.num_timesteps),
                    "mean_reward": float(np.mean(totals)),
                    "components": {
                        k: float(np.mean([c.get(k, 0.0) for c in comps])) for k in comps[0]
                    },
                    "updated_at": now,
                }
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload))
                os.replace(tmp, self.path)
            except Exception:
                pass  # status is best-effort; never let it break training
            return True

    return CallbackList([
        CheckpointCallback(save_freq=max(1, ep_length // 2), save_path=sess_path, name_prefix="poke"),
        TensorboardCallback(sess_path),
        StatusCallback(sess_path / "training_status.json"),
    ])


def main():
    args = parse_args()
    os.chdir(HERE)  # upstream paths ('../init.state', 'events.json') are relative to v2/
    preflight(args)

    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import SubprocVecEnv
    from stable_baselines3.common.utils import set_random_seed

    sess_path = Path(args.sess_dir)
    sess_path.mkdir(exist_ok=True)
    # The GUI looks here to find where status/checkpoints live.
    (sess_path / "current_run.txt").write_text(str(sess_path))

    env_config = {
        "headless": args.headless, "save_final_state": False, "early_stop": False,
        "action_freq": 24, "init_state": "../init.state", "max_steps": args.ep_length,
        "print_rewards": True, "save_video": False, "fast_video": True,
        "session_path": sess_path, "gb_path": "../PokemonRed.gb", "debug": False,
        "reward_scale": args.reward_scale, "explore_weight": args.explore_weight,
        "reward_config_path": args.reward_config,
    }
    print(env_config)

    set_random_seed(args.seed)
    env = SubprocVecEnv([
        make_env(i, env_config, args.seed, args.stream, args.stream_user)
        for i in range(args.num_cpu)
    ])

    # Optional resume: checkpoint path (without .zip) piped on stdin, as upstream does.
    file_name = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read().strip()
    if file_name.endswith(".zip"):
        file_name = file_name[:-4]

    n_steps = args.ep_length // 64
    batch_size = min(512, n_steps * args.num_cpu)

    if file_name and exists(file_name + ".zip"):
        print(f"\nloading checkpoint {file_name}.zip")
        model = PPO.load(file_name, env=env, device=args.device)
        # Same rollout-buffer adjustment upstream uses when resuming with a different setup.
        model.n_steps = n_steps
        model.n_envs = args.num_cpu
        model.rollout_buffer.buffer_size = n_steps
        model.rollout_buffer.n_envs = args.num_cpu
        model.rollout_buffer.reset()
        # A checkpoint saved with a bigger rollout buffer carries a batch_size
        # that no longer fits n_steps * n_envs; SB3 asserts on it in train().
        # Clamp it so resuming with a different --ep-length/--num-cpu works.
        try:
            model.batch_size = max(1, min(int(model.batch_size), n_steps * args.num_cpu))
        except (AttributeError, TypeError, ValueError):
            model.batch_size = min(512, n_steps * args.num_cpu)
    else:
        if file_name:
            print(f"\nCheckpoint '{file_name}.zip' not found -- starting a fresh run.")
        model = PPO("MultiInputPolicy", env, verbose=1, n_steps=n_steps, batch_size=batch_size,
                    n_epochs=1, gamma=0.997, ent_coef=0.01, tensorboard_log=sess_path,
                    device=args.device)
    print(model.policy)

    # GUI stops training with CTRL_BREAK_EVENT (Windows) -- turn it into KeyboardInterrupt.
    def _interrupt(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _interrupt)

    interrupted = False
    try:
        model.learn(total_timesteps=args.ep_length * args.num_cpu * 10000,
                    callback=build_callbacks(sess_path, args.ep_length), tb_log_name="poke_ppo")
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted -- saving checkpoint before exit...")
    finally:
        try:
            out = sess_path / f"poke_{'interrupted_' if interrupted else ''}{model.num_timesteps}_steps"
            model.save(out)
            print(f"Saved {out}.zip")
        except Exception as e:
            print(f"Could not save final checkpoint: {e}")
        try:
            env.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
