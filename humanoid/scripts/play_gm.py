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

# Fallback URL is supplied only by the GM playback task, never committed to Git.


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


def decode_checkpoint_url_b64(encoded_url):
    """Decode and validate the URL-safe checkpoint URL passed by GM."""
    if not encoded_url:
        return None
    try:
        padding = "=" * (-len(encoded_url) % 4)
        decoded = base64.urlsafe_b64decode(encoded_url + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid URL-safe Base64 checkpoint URL") from exc
    if not decoded.startswith(("https://", "http://")):
        raise ValueError("Decoded checkpoint URL must use HTTP(S)")
    return decoded


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
    header += ["pre_base_height", "pre_base_vel_x", "pre_base_vel_y", "pre_base_vel_yaw",
               "pre_foot_z_l", "pre_foot_z_r", "pre_foot_force_l", "pre_foot_force_r"]
    header += ["command_y", "command_yaw", "tensor_command_x", "tensor_command_y", "tensor_command_yaw",
               "next_command_x", "next_command_y", "next_command_yaw"]
    header += ["done", "time_out", "motion_stage_before", "motion_stage_after",
               "motion_stage_step_before", "motion_stage_step_after",
               "reference_frame_before", "reference_frame_after",
               "episode_length_before", "episode_length_after"]
    header += [f"policy_action_{i}" for i in range(num_actions)]
    header += [f"action_{i}" for i in range(num_actions)]
    header += [f"dof_pos_{i}" for i in range(num_actions)]
    header += [f"dof_vel_{i}" for i in range(num_actions)]
    header += [f"dof_torque_{i}" for i in range(num_actions)]
    header += [f"ref_dof_pos_{i}" for i in range(num_actions)]
    header += [f"ref_dof_vel_{i}" for i in range(num_actions)]
    header += [f"target_dof_pos_{i}" for i in range(num_actions)]
    header += ["ref_root_pos_x", "ref_root_pos_y", "ref_root_pos_z",
               "ref_root_quat_x", "ref_root_quat_y", "ref_root_quat_z", "ref_root_quat_w",
               "ref_root_lin_vel_x", "ref_root_lin_vel_y", "ref_root_lin_vel_z",
               "ref_root_ang_vel_x", "ref_root_ang_vel_y", "ref_root_ang_vel_z"]
    header += ["forward_displacement_ratio", "single_support_ratio",
               "swing_count_l", "swing_count_r", "cum_abs_yaw", "episode_success"]

    def value(data, key, index, default=np.nan):
        values = data.get(key)
        if values is None:
            return default
        return values[index]

    def row_values(data, key, index, width):
        values = data.get(key)
        if values is None:
            return [np.nan] * width
        return list(values[index])

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for i in range(len(diag_data["base_height"])):
            row = [i, round(i * dt, 6), diag_data["base_height"][i],
                   diag_data["base_vel_x"][i], diag_data["base_vel_y"][i],
                   diag_data["base_vel_yaw"][i], diag_data["command_x"][i],
                   diag_data["foot_z_l"][i], diag_data["foot_z_r"][i],
                   diag_data["foot_force_l"][i], diag_data["foot_force_r"][i]]
            row += [value(diag_data, "pre_base_height", i),
                    value(diag_data, "pre_base_vel_x", i),
                    value(diag_data, "pre_base_vel_y", i),
                    value(diag_data, "pre_base_vel_yaw", i),
                    value(diag_data, "pre_foot_z_l", i),
                    value(diag_data, "pre_foot_z_r", i),
                    value(diag_data, "pre_foot_force_l", i),
                    value(diag_data, "pre_foot_force_r", i)]
            row += [value(diag_data, "command_y", i),
                    value(diag_data, "command_yaw", i),
                    value(diag_data, "tensor_command_x", i),
                    value(diag_data, "tensor_command_y", i),
                    value(diag_data, "tensor_command_yaw", i),
                    value(diag_data, "next_command_x", i),
                    value(diag_data, "next_command_y", i),
                    value(diag_data, "next_command_yaw", i)]
            row += [value(diag_data, "done", i, False),
                    value(diag_data, "time_out", i, False),
                    value(diag_data, "motion_stage_before", i, -1),
                    value(diag_data, "motion_stage_after", i, -1),
                    value(diag_data, "motion_stage_step_before", i, -1),
                    value(diag_data, "motion_stage_step_after", i, -1),
                    value(diag_data, "reference_frame_before", i, -1),
                    value(diag_data, "reference_frame_after", i, -1),
                    value(diag_data, "episode_length_before", i, -1),
                    value(diag_data, "episode_length_after", i, -1)]
            row += row_values(diag_data, "policy_action", i, num_actions)
            row += row_values(diag_data, "action", i, num_actions)
            row += diag_data["dof_pos"][i]
            row += diag_data["dof_vel"][i]
            row += diag_data["dof_torque"][i]
            row += row_values(diag_data, "ref_dof_pos", i, num_actions)
            row += row_values(diag_data, "ref_dof_vel", i, num_actions)
            row += row_values(diag_data, "target_dof_pos", i, num_actions)
            row += row_values(diag_data, "ref_root_pos", i, 3)
            row += row_values(diag_data, "ref_root_quat", i, 4)
            row += row_values(diag_data, "ref_root_lin_vel", i, 3)
            row += row_values(diag_data, "ref_root_ang_vel", i, 3)
            row += [value(diag_data, "forward_displacement_ratio", i),
                    value(diag_data, "single_support_ratio", i),
                    value(diag_data, "swing_count_l", i),
                    value(diag_data, "swing_count_r", i),
                    value(diag_data, "cum_abs_yaw", i),
                    value(diag_data, "episode_success", i)]
            writer.writerow(row)

    print(f"[play_gm] Saved diagnostic CSV -> {csv_path}")
    return csv_path


def package_csv_as_pt(csv_path, experiment_name="x1_dh_stand"):
    """Package diagnostic CSV as model_isaac_csv.pt for GM SDK auto-upload"""
    log_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "logs", experiment_name, "gm_play")
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

    checkpoint_url = getattr(args, "checkpoint_url", None)
    encoded_checkpoint_url = getattr(args, "checkpoint_url_b64", None)
    if encoded_checkpoint_url:
        checkpoint_url = decode_checkpoint_url_b64(encoded_checkpoint_url)

    zero_action = bool(getattr(args, "zero_action", False))
    if not zero_action:
        # Find and load checkpoint for normal policy playback.
        checkpoint_path = find_checkpoint(checkpoint_url)
        if checkpoint_path is None:
            print("[play_gm] ERROR: No checkpoint found in /personal/ or logs/")
            sys.exit(1)

        print(f"[play_gm] Found checkpoint: {checkpoint_path}")
        copy_checkpoint_to_logs(checkpoint_path, train_cfg.runner.experiment_name)
        model_name = os.path.basename(checkpoint_path)  # e.g. model_10000.pt
        checkpoint_num = int(model_name.replace("model_", "").replace(".pt", ""))
        train_cfg.runner.resume = True
        train_cfg.runner.load_run = "gm_play"
        train_cfg.runner.checkpoint = checkpoint_num
    else:
        print("[play_gm] Zero-action reference validation enabled; checkpoint loading is skipped")

    # Create environment (headless with rendering enabled)
    env, _ = task_registry.make_env(name=args.task, args=args, env_cfg=env_cfg)
    # exp0.4: evaluation replays always use the full staged sequence; random
    # mid-WALK resets are a training-only distribution.
    if hasattr(env, "cfg") and hasattr(env.cfg.env, "random_phase_reset_prob"):
        env.cfg.env.random_phase_reset_prob = 0.0
        print("[play_gm] random_phase_reset_prob forced to 0.0 for evaluation")
    env.set_camera(env_cfg.viewer.pos, env_cfg.viewer.lookat)

    policy = None
    if zero_action:
        # Normally the runner performs the initial environment reset. The
        # checkpoint-free path must do it explicitly before collecting data.
        env.reset()
    else:
        # Create runner and load policy.
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
        "command_y": [],
        "command_yaw": [],
        "tensor_command_x": [],
        "tensor_command_y": [],
        "tensor_command_yaw": [],
        "next_command_x": [],
        "next_command_y": [],
        "next_command_yaw": [],
        "foot_z_l": [],
        "foot_z_r": [],
        "foot_force_l": [],
        "foot_force_r": [],
        "pre_base_height": [],
        "pre_base_vel_x": [],
        "pre_base_vel_y": [],
        "pre_base_vel_yaw": [],
        "pre_foot_z_l": [],
        "pre_foot_z_r": [],
        "pre_foot_force_l": [],
        "pre_foot_force_r": [],
        "done": [],
        "time_out": [],
        "motion_stage_before": [],
        "motion_stage_after": [],
        "motion_stage_step_before": [],
        "motion_stage_step_after": [],
        "reference_frame_before": [],
        "reference_frame_after": [],
        "episode_length_before": [],
        "episode_length_after": [],
        "policy_action": [],
        "action": [],
        "dof_pos": [],
        "dof_vel": [],
        "dof_torque": [],
        "ref_dof_pos": [],
        "ref_dof_vel": [],
        "target_dof_pos": [],
        "ref_root_pos": [],
        "ref_root_quat": [],
        "ref_root_lin_vel": [],
        "ref_root_ang_vel": [],
        # exp0.2 episode-level diagnostics (per-step running values; the last
        # row of an episode carries its final value).
        "forward_displacement_ratio": [],
        "single_support_ratio": [],
        "swing_count_l": [],
        "swing_count_r": [],
        "cum_abs_yaw": [],
        "episode_success": [],
    }

    # The trajectory task derives commands from the current reference. Replacing
    # them with a fixed scalar would make actor observations and rewards disagree.
    FIX_COMMAND = args.task != "x1_trajectory"
    fix_vel = 0.5  # Forward walking speed

    def tensor_row(name, width, default=np.nan):
        """Return one environment's row for an optional diagnostic tensor."""
        tensor = getattr(env, name, None)
        if tensor is None:
            return [default] * width
        if not torch.is_tensor(tensor):
            return [default] * width
        row = tensor[0].detach().cpu().reshape(-1).tolist()
        if len(row) != width:
            return [default] * width
        return row

    def tensor_scalar(name, default=-1):
        """Return one environment's scalar for an optional diagnostic tensor."""
        tensor = getattr(env, name, None)
        if tensor is None or not torch.is_tensor(tensor):
            return default
        return tensor[0].detach().cpu().item()

    def episode_diag_row(done):
        """exp0.2 running episode diagnostics; nan when the task lacks them."""
        def val(name, default=np.nan):
            tensor = getattr(env, name, None)
            if tensor is None or not torch.is_tensor(tensor):
                return default
            return tensor[0].detach().cpu().item()

        start_xy = getattr(env, "episode_root_start_xy", None)
        ref_disp = getattr(env, "reference_episode_xy_displacement", None)
        if start_xy is not None and torch.is_tensor(start_xy) and ref_disp:
            displacement = torch.linalg.vector_norm(env.root_states[0, :2] - start_xy[0]).item()
            forward_displacement_ratio = displacement / ref_disp
        else:
            forward_displacement_ratio = np.nan
        walk_steps = val("walk_steps_elapsed", 0.0)
        single_steps = val("single_support_steps", 0.0)
        single_support_ratio = single_steps / walk_steps if walk_steps > 0 else np.nan
        # Success is only defined once the episode ends; reset_idx snapshots it
        # into last_episode_success before clearing the counters.
        episode_success = val("last_episode_success") if done else np.nan
        return {
            "forward_displacement_ratio": forward_displacement_ratio,
            "single_support_ratio": single_support_ratio,
            "swing_count_l": val("swing_count_l"),
            "swing_count_r": val("swing_count_r"),
            "cum_abs_yaw": val("cum_abs_yaw"),
            "episode_success": episode_success,
        }

    for i in range(total_steps):
        # Capture the state and reference used for the action.  The environment
        # resets terminated robots inside env.step(), so post-step-only logging
        # loses the actual terminal state.
        pre_base_height = env.root_states[0, 2].item()
        pre_base_vel = env.base_lin_vel[0].detach().cpu().tolist()
        pre_base_ang_vel = env.base_ang_vel[0].detach().cpu().tolist()
        pre_foot_z_l = env.rigid_state[0, left_foot_idx, 2].item()
        pre_foot_z_r = env.rigid_state[0, right_foot_idx, 2].item()
        pre_foot_force_l = env.contact_forces[0, left_foot_idx, 2].item()
        pre_foot_force_r = env.contact_forces[0, right_foot_idx, 2].item()
        command_before = env.commands[0].detach().cpu().tolist()
        stage_before = tensor_scalar("motion_stage")
        stage_step_before = tensor_scalar("motion_stage_step")
        reference_frame_before = tensor_scalar("reference_frame")
        episode_length_before = tensor_scalar("episode_length_buf")
        ref_dof_pos = tensor_row("ref_dof_pos", env_cfg.env.num_actions)
        ref_dof_vel = tensor_row("ref_dof_vel", env_cfg.env.num_actions)
        ref_root_pos = tensor_row("ref_root_pos_w", 3)
        ref_root_quat = tensor_row("ref_root_quat", 4)
        ref_root_lin_vel = tensor_row("ref_root_lin_vel", 3)
        ref_root_ang_vel = tensor_row("ref_root_ang_vel", 3)

        # Keep fixed-command playback only for command-driven baseline tasks.
        if FIX_COMMAND:
            env.commands[:, 0] = fix_vel
            env.commands[:, 1] = 0.0
            env.commands[:, 2] = 0.0
            env.commands[:, 3] = 0.0
            command_before = env.commands[0].detach().cpu().tolist()

        # command_x/y/yaw are the command values actually present in the
        # latest actor observation.  The tensor_command_* fields retain the
        # command tensor immediately before env.step(), while next_command_*
        # records the value produced by the environment after the step.
        single_obs = obs[0, -env_cfg.env.num_single_obs:]
        observed_command = [
            single_obs[2].item() / float(env.obs_scales.lin_vel),
            single_obs[3].item() / float(env.obs_scales.lin_vel),
            single_obs[4].item() / float(env.obs_scales.ang_vel),
        ]

        if zero_action:
            actions = torch.zeros(
                (env.num_envs, env_cfg.env.num_actions), dtype=torch.float, device=env.device
            )
        else:
            actions = policy(obs.detach())
        policy_action = actions[0].detach().cpu().tolist()
        clip_actions = float(env_cfg.normalization.clip_actions)
        applied_action = torch.clamp(actions, -clip_actions, clip_actions)[0].detach().cpu().tolist()

        obs, critic_obs, rews, dones, infos = env.step(actions.detach())

        # Record diagnostic data
        diag["base_height"].append(env.root_states[0, 2].item())
        diag["base_vel_x"].append(env.base_lin_vel[0, 0].item())
        diag["base_vel_y"].append(env.base_lin_vel[0, 1].item())
        diag["base_vel_yaw"].append(env.base_ang_vel[0, 2].item())
        diag["command_x"].append(observed_command[0])
        diag["command_y"].append(observed_command[1])
        diag["command_yaw"].append(observed_command[2])
        diag["tensor_command_x"].append(command_before[0])
        diag["tensor_command_y"].append(command_before[1])
        diag["tensor_command_yaw"].append(command_before[2])
        diag["next_command_x"].append(env.commands[0, 0].item())
        diag["next_command_y"].append(env.commands[0, 1].item())
        diag["next_command_yaw"].append(env.commands[0, 2].item())
        diag["foot_z_l"].append(env.rigid_state[0, left_foot_idx, 2].item())
        diag["foot_z_r"].append(env.rigid_state[0, right_foot_idx, 2].item())
        diag["foot_force_l"].append(env.contact_forces[0, left_foot_idx, 2].item())
        diag["foot_force_r"].append(env.contact_forces[0, right_foot_idx, 2].item())
        diag["pre_base_height"].append(pre_base_height)
        diag["pre_base_vel_x"].append(pre_base_vel[0])
        diag["pre_base_vel_y"].append(pre_base_vel[1])
        diag["pre_base_vel_yaw"].append(pre_base_ang_vel[2])
        diag["pre_foot_z_l"].append(pre_foot_z_l)
        diag["pre_foot_z_r"].append(pre_foot_z_r)
        diag["pre_foot_force_l"].append(pre_foot_force_l)
        diag["pre_foot_force_r"].append(pre_foot_force_r)
        diag["done"].append(bool(dones[0].item()))
        diag["time_out"].append(bool(env.time_out_buf[0].item()))
        diag["motion_stage_before"].append(stage_before)
        diag["motion_stage_after"].append(tensor_scalar("motion_stage"))
        diag["motion_stage_step_before"].append(stage_step_before)
        diag["motion_stage_step_after"].append(tensor_scalar("motion_stage_step"))
        diag["reference_frame_before"].append(reference_frame_before)
        diag["reference_frame_after"].append(tensor_scalar("reference_frame"))
        diag["episode_length_before"].append(episode_length_before)
        diag["episode_length_after"].append(tensor_scalar("episode_length_buf"))
        diag["policy_action"].append(policy_action)
        diag["action"].append(applied_action)
        diag["dof_pos"].append(env.dof_pos[0].cpu().numpy().tolist())
        diag["dof_vel"].append(env.dof_vel[0].cpu().numpy().tolist())
        diag["dof_torque"].append(env.torques[0].cpu().numpy().tolist())
        diag["ref_dof_pos"].append(ref_dof_pos)
        diag["ref_dof_vel"].append(ref_dof_vel)
        diag["target_dof_pos"].append(tensor_row("pd_target_dof_pos", env_cfg.env.num_actions))
        diag["ref_root_pos"].append(ref_root_pos)
        diag["ref_root_quat"].append(ref_root_quat)
        diag["ref_root_lin_vel"].append(ref_root_lin_vel)
        diag["ref_root_ang_vel"].append(ref_root_ang_vel)
        for key, value in episode_diag_row(bool(dones[0].item())).items():
            diag[key].append(value)

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
    # GM SDK scans this fixed directory for the diagnostic upload package.
    csv_pt_path = package_csv_as_pt(csv_path)

    # Print summary
    print("\n[play_gm] === Playback Summary ===")
    print(f"  Total steps: {total_steps}")
    print(f"  Frames recorded: {frame_count}")
    avg_vel = np.mean(diag["base_vel_x"])
    avg_height = np.mean(diag["base_height"])
    avg_command = np.mean(diag["command_x"])
    print(f"  Avg forward velocity: {avg_vel:.3f} m/s (mean command: {avg_command:.3f} m/s)")
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
