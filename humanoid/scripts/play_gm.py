# GM cloud playback script for X1 Isaac Gym simulation
# Features:
#   - No pygame dependency (headless cloud)
#   - Auto-loads checkpoint from /personal/ or logs/
#   - Headless rendering with GPU camera sensors
#   - Packages video as model_video.pt for GM SDK upload
#   - Outputs diagnostic trajectory data

import os
import sys
import glob
import base64
import shutil
import subprocess
import numpy as np
import cv2
import csv
from isaacgym import gymapi
import torch
from datetime import datetime

from humanoid import LEGGED_GYM_ROOT_DIR
from humanoid.envs import *
from humanoid.utils import get_args, export_policy_as_jit, task_registry, Logger
from isaacgym.torch_utils import *

# Checkpoint URL is supplied only by the GM playback task at runtime.


def find_checkpoint(checkpoint_url=None):
    """Search broadly for model_*.pt checkpoint"""
    search_dirs = [
        "/personal",
        "/workspace",
        os.path.join(LEGGED_GYM_ROOT_DIR, "logs", "x1_dh_stand"),
        os.getcwd(),
    ]
    for d in search_dirs:
        if not os.path.exists(d):
            print(f"[play_gm] Search dir does not exist: {d}")
            continue
        # List all files in the directory for debugging
        try:
            all_files = os.listdir(d)
            pt_files = [f for f in all_files if f.endswith('.pt')]
            if pt_files:
                print(f"[play_gm] .pt files in {d}: {pt_files}")
            else:
                print(f"[play_gm] No .pt files directly in {d} (total files: {len(all_files)})")
        except Exception as e:
            print(f"[play_gm] Cannot list {d}: {e}")
            continue
        # Search for model_*.pt recursively
        models = sorted(glob.glob(os.path.join(d, "**", "model_*.pt"), recursive=True))
        # Also try non-recursive
        models += sorted(glob.glob(os.path.join(d, "model_*.pt")))
        # Also try any .pt file if no model_*.pt found
        if not models:
            models = sorted(glob.glob(os.path.join(d, "**", "*.pt"), recursive=True))
            models += sorted(glob.glob(os.path.join(d, "*.pt")))
        # Exclude deploy/video/diag files
        models = [m for m in models if "deploy" not in m and "video" not in m and "diag" not in m]
        if models:
            print(f"[play_gm] Found checkpoint: {models[-1]}")
            return models[-1]  # Return latest
    # Fallback: download from OSS if not found locally
    print("[play_gm] No local checkpoint found, downloading from OSS...")
    if not checkpoint_url:
        print("[play_gm] No temporary checkpoint URL was supplied")
        return None
    download_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", "x1_dh_stand", "gm_play")
    os.makedirs(download_dir, exist_ok=True)
    download_path = os.path.join(download_dir, "model_5000.pt")
    try:
        result = subprocess.run(
            ["curl", "-L", "-o", download_path, checkpoint_url],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and os.path.exists(download_path):
            print(f"[play_gm] Downloaded checkpoint to {download_path} ({os.path.getsize(download_path)} bytes)")
            return download_path
        else:
            print(f"[play_gm] Download failed: {result.stderr}")
    except Exception as e:
        print(f"[play_gm] Download error: {e}")
    return None


def copy_checkpoint_to_logs(checkpoint_path, experiment_name="x1_dh_stand"):
    """Copy checkpoint to logs directory structure expected by task_registry"""
    # task_registry uses: logs/{experiment_name}/exported_data/{load_run}/model_{checkpoint}.pt
    log_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", experiment_name, "exported_data", "gm_play")
    os.makedirs(log_dir, exist_ok=True)
    dest = os.path.join(log_dir, os.path.basename(checkpoint_path))
    if not os.path.exists(dest):
        shutil.copy2(checkpoint_path, dest)
        print(f"[play_gm] Copied checkpoint: {checkpoint_path} -> {dest}")
    return log_dir


def package_video_as_pt(video_path, experiment_name="x1_dh_stand"):
    """Package mp4 video as model_isaac_video.pt for GM SDK auto-upload"""
    # Save in a subdirectory so SDK's PT directory scan discovers it
    log_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", experiment_name, "play_output")
    os.makedirs(log_dir, exist_ok=True)
    pt_path = os.path.join(log_dir, "model_isaac_video.pt")

    with open(video_path, "rb") as f:
        video_bytes = f.read()

    torch.save({"bytes": video_bytes, "filename": os.path.basename(video_path)}, pt_path)
    print(f"[play_gm] Packaged video ({len(video_bytes)} bytes) -> {pt_path}")
    return pt_path


def save_diag_data(diag_data, experiment_name="x1_dh_stand"):
    """Save diagnostic trajectory data as model_diag.pt for GM SDK upload"""
    log_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", experiment_name, "play_output")
    os.makedirs(log_dir, exist_ok=True)
    pt_path = os.path.join(log_dir, "model_diag.pt")
    torch.save(diag_data, pt_path)
    print(f"[play_gm] Saved diagnostic data -> {pt_path}")


def save_diag_csv(diag_data, experiment_name="x1_dh_stand", num_actions=12, dt=0.01):
    """Save diagnostic trajectory data as isaac_diag.csv."""
    log_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", experiment_name, "play_output")
    os.makedirs(log_dir, exist_ok=True)
    csv_path = os.path.join(log_dir, "isaac_diag.csv")

    header = ["step", "time_s", "base_height", "base_vel_x", "base_vel_y", "base_vel_yaw",
              "command_x", "foot_z_l", "foot_z_r", "foot_force_l", "foot_force_r"]
    header += [f"dof_pos_{i}" for i in range(num_actions)]
    header += [f"dof_vel_{i}" for i in range(num_actions)]
    header += [f"dof_torque_{i}" for i in range(num_actions)]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for i in range(len(diag_data["base_height"])):
            row = [i, round(i * dt, 6), diag_data["base_height"][i],
                   diag_data["base_vel_x"][i], diag_data["base_vel_y"][i],
                   diag_data["base_vel_yaw"][i], diag_data["command_x"][i],
                   diag_data["foot_z_l"][i], diag_data["foot_z_r"][i],
                   diag_data["foot_force_l"][i], diag_data["foot_force_r"][i]]
            row += diag_data["dof_pos"][i]
            row += diag_data["dof_vel"][i]
            row += diag_data["dof_torque"][i]
            writer.writerow(row)

    print(f"[play_gm] Saved diagnostic CSV -> {csv_path}")
    return csv_path


def package_csv_as_pt(csv_path, experiment_name="x1_dh_stand"):
    """Package diagnostic CSV as model_isaac_csv.pt for GM SDK auto-upload"""
    # GM's PT watcher is initialized from the checkpoint directory at task
    # startup.  Keep the CSV package beside that checkpoint so it is queued
    # for upload rather than merely discovered by the global file scanner.
    log_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", "x1_dh_stand", "gm_play")
    os.makedirs(log_dir, exist_ok=True)
    pt_path = os.path.join(log_dir, "model_isaac_csv.pt")
    with open(csv_path, "r", encoding="utf-8") as f:
        csv_bytes = f.read().encode("utf-8")
    torch.save({"bytes": csv_bytes, "filename": os.path.basename(csv_path)}, pt_path)
    print(f"[play_gm] Packaged CSV ({len(csv_bytes)} bytes) -> {pt_path}")
    return pt_path


def play(args):
    env_cfg, train_cfg = task_registry.get_cfgs(name=args.task)

    # Override for playback
    env_cfg.env.num_envs = min(env_cfg.env.num_envs, 1)
    env_cfg.terrain.mesh_type = "plane"
    env_cfg.env.episode_length_s = 1000
    env_cfg.noise.add_noise = False

    # Disable all domain randomization for clean playback
    env_cfg.domain_rand.randomize_friction = False
    env_cfg.domain_rand.push_robots = False
    env_cfg.domain_rand.randomize_base_mass = False
    env_cfg.domain_rand.randomize_com = False
    env_cfg.domain_rand.randomize_gains = False
    env_cfg.domain_rand.randomize_torque = False
    env_cfg.domain_rand.randomize_link_mass = False
    env_cfg.domain_rand.randomize_motor_offset = False
    env_cfg.domain_rand.randomize_joint_friction = False
    env_cfg.domain_rand.randomize_joint_damping = False
    env_cfg.domain_rand.randomize_joint_armature = False
    env_cfg.domain_rand.randomize_lag_timesteps = False
    env_cfg.domain_rand.add_lag = False
    env_cfg.domain_rand.add_dof_lag = False
    env_cfg.commands.heading_command = False
    env_cfg.noise.curriculum = False

    # Enable headless rendering: no viewer but GPU camera sensors work
    env_cfg.env.enable_headless_render = True

    train_cfg.seed = 12345

    # Find and load checkpoint
    checkpoint_url = getattr(args, "checkpoint_url", None)
    checkpoint_url_b64 = getattr(args, "checkpoint_url_b64", None)
    if checkpoint_url_b64:
        padding = "=" * (-len(checkpoint_url_b64) % 4)
        checkpoint_url = base64.urlsafe_b64decode(
            (checkpoint_url_b64 + padding).encode("ascii")
        ).decode("utf-8")
    checkpoint_path = find_checkpoint(checkpoint_url)
    if checkpoint_path is None:
        print("[play_gm] ERROR: No checkpoint found in /personal/ or logs/")
        sys.exit(1)

    print(f"[play_gm] Found checkpoint: {checkpoint_path}")

    # Copy checkpoint to expected logs directory
    log_dir = copy_checkpoint_to_logs(checkpoint_path, train_cfg.runner.experiment_name)
    model_name = os.path.basename(checkpoint_path)  # e.g. model_10000.pt
    checkpoint_num = int(model_name.replace("model_", "").replace(".pt", ""))

    # Configure runner to load from our copied checkpoint
    train_cfg.runner.resume = True
    train_cfg.runner.load_run = "gm_play"
    train_cfg.runner.checkpoint = checkpoint_num

    # Create environment (headless with rendering enabled)
    env, _ = task_registry.make_env(name=args.task, args=args, env_cfg=env_cfg)
    env.set_camera(env_cfg.viewer.pos, env_cfg.viewer.lookat)

    # Create runner and load policy
    ppo_runner, train_cfg, _ = task_registry.make_alg_runner(
        env=env, name=args.task, args=args, train_cfg=train_cfg
    )
    policy = ppo_runner.get_inference_policy(device=env.device)
    print("[play_gm] Policy loaded successfully!")

    # Setup camera for video recording
    camera_properties = gymapi.CameraProperties()
    camera_properties.width = 1920
    camera_properties.height = 1080
    h1 = env.gym.create_camera_sensor(env.envs[0], camera_properties)

    # Attach camera to robot body (follow view)
    camera_offset = gymapi.Vec3(2.0, -2.0, 1.5)
    camera_rotation = gymapi.Quat.from_axis_angle(gymapi.Vec3(-0.3, 0.2, 1), np.deg2rad(135))
    actor_handle = env.gym.get_actor_handle(env.envs[0], 0)
    body_handle = env.gym.get_actor_rigid_body_handle(env.envs[0], actor_handle, 0)
    env.gym.attach_camera_to_body(
        h1, env.envs[0], body_handle,
        gymapi.Transform(camera_offset, camera_rotation),
        gymapi.FOLLOW_POSITION,
    )

    # Setup video writer - save to logs dir (not /personal which may not exist)
    video_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", train_cfg.runner.experiment_name)
    os.makedirs(video_dir, exist_ok=True)
    video_path = os.path.join(video_dir, "play_output.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    video = cv2.VideoWriter(video_path, fourcc, 50.0, (1920, 1080))
    print(f"[play_gm] Recording video to: {video_path}")

    # Get foot indices for diagnostics
    left_foot_idx = env.feet_indices[0].item()
    right_foot_idx = env.feet_indices[1].item()

    obs = env.get_observations()
    total_steps = 2000  # 40 seconds at 50Hz control
    frame_count = 0

    # Diagnostic data storage
    diag = {
        "base_height": [],
        "base_vel_x": [],
        "base_vel_y": [],
        "base_vel_yaw": [],
        "command_x": [],
        "foot_z_l": [],
        "foot_z_r": [],
        "foot_force_l": [],
        "foot_force_r": [],
        "dof_pos": [],
        "dof_vel": [],
        "dof_torque": [],
    }

    FIX_COMMAND = True
    fix_vel = 0.5  # Forward walking speed

    for i in range(total_steps):
        actions = policy(obs.detach())

        if FIX_COMMAND:
            env.commands[:, 0] = fix_vel
            env.commands[:, 1] = 0.0
            env.commands[:, 2] = 0.0
            env.commands[:, 3] = 0.0

        obs, critic_obs, rews, dones, infos = env.step(actions.detach())

        # Record diagnostic data
        diag["base_height"].append(env.root_states[0, 2].item())
        diag["base_vel_x"].append(env.base_lin_vel[0, 0].item())
        diag["base_vel_y"].append(env.base_lin_vel[0, 1].item())
        diag["base_vel_yaw"].append(env.base_ang_vel[0, 2].item())
        diag["command_x"].append(env.commands[0, 0].item())
        diag["foot_z_l"].append(env.rigid_state[0, left_foot_idx, 2].item())
        diag["foot_z_r"].append(env.rigid_state[0, right_foot_idx, 2].item())
        diag["foot_force_l"].append(env.contact_forces[0, left_foot_idx, 2].item())
        diag["foot_force_r"].append(env.contact_forces[0, right_foot_idx, 2].item())
        diag["dof_pos"].append(env.dof_pos[0].cpu().numpy().tolist())
        diag["dof_vel"].append(env.dof_vel[0].cpu().numpy().tolist())
        diag["dof_torque"].append(env.torques[0].cpu().numpy().tolist())

        # Render and record video frame
        frame_count += 1
        env.gym.fetch_results(env.sim, True)
        env.gym.step_graphics(env.sim)
        env.gym.render_all_camera_sensors(env.sim)

        if frame_count % 2 == 0:  # Record at 25fps (sim runs at 50Hz)
            img = env.gym.get_camera_image(env.sim, env.envs[0], h1, gymapi.IMAGE_COLOR)
            if img is not None and len(img) > 0:
                img = np.reshape(img, (1080, 1920, 4))
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                video.write(img[..., :3])

        if i % 200 == 0:
            print(f"[play_gm] Step {i}/{total_steps} | vel_x={env.base_lin_vel[0, 0].item():.3f} | height={env.root_states[0, 2].item():.3f}")

    # Cleanup
    video.release()
    print(f"[play_gm] Video saved to {video_path} ({frame_count} frames)")

    # Package video as .pt for GM SDK auto-upload
    if os.path.exists(video_path):
        package_video_as_pt(video_path, train_cfg.runner.experiment_name)

    # Save diagnostic data
    save_diag_data(diag, train_cfg.runner.experiment_name)
    csv_path = save_diag_csv(diag, train_cfg.runner.experiment_name, env_cfg.env.num_actions, env.dt)
    csv_pt_path = package_csv_as_pt(csv_path, train_cfg.runner.experiment_name)

    # Print summary
    print("\n[play_gm] === Playback Summary ===")
    print(f"  Total steps: {total_steps}")
    print(f"  Frames recorded: {frame_count}")
    avg_vel = np.mean(diag["base_vel_x"])
    avg_height = np.mean(diag["base_height"])
    print(f"  Avg forward velocity: {avg_vel:.3f} m/s (target: {fix_vel})")
    print(f"  Avg base height: {avg_height:.3f} m")
    print(f"  Video: {video_path}")
    print(f"  Packaged for upload: logs/{train_cfg.runner.experiment_name}/model_isaac_video.pt")
    print(f"  Diagnostics: logs/{train_cfg.runner.experiment_name}/model_diag.pt")
    print(f"  CSV: {csv_path}")
    print(f"  CSV packaged for upload: {csv_pt_path}")

    # Wait for SDK to detect and upload model files
    import time
    print("[play_gm] Waiting 60s for SDK file upload...")
    time.sleep(60)
    print("[play_gm] Done.")


if __name__ == "__main__":
    args = get_args()
    play(args)
