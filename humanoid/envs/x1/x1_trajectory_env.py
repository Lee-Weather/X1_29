"""Independent X1 task that validates and loads a packaged reference motion."""

from pathlib import Path

import numpy as np
import torch
from isaacgym import gymtorch
from isaacgym.torch_utils import quat_rotate_inverse

from humanoid.envs.x1.x1_dh_stand_env import X1DHStandEnv, get_euler_xyz_tensor
from humanoid.envs.x1.trajectory_utils import canonicalize_root_trajectory


class X1TrajectoryEnv(X1DHStandEnv):
    """Track a staged stand-start-walk-stop-stand reference sequence."""

    STAND_INITIAL, START, WALK, STOP, STAND_FINAL = range(5)
    STAGE_COUNT = 5
    # exp0.2: a foot must stay airborne this many consecutive control steps
    # to count as one valid swing (success criterion 4/5).
    SWING_MIN_STEPS = 5

    def __init__(self, cfg, sim_params, physics_engine, sim_device, headless):
        super().__init__(cfg, sim_params, physics_engine, sim_device, headless)
        self._load_reference_trajectory()
        self.reference_frame = torch.zeros(self.num_envs, dtype=torch.long, device=self.device)
        self.reference_origin_xy = torch.zeros(self.num_envs, 2, device=self.device)
        self.motion_stage = torch.full(
            (self.num_envs,), self.STAND_INITIAL, dtype=torch.long, device=self.device
        )
        self.motion_stage_step = torch.zeros(self.num_envs, dtype=torch.long, device=self.device)
        self.motion_ended = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)
        # exp0.2: per-episode diagnostics for the success criteria and play_gm CSV.
        self.episode_success_buf = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)
        self.episode_root_start_xy = torch.zeros(self.num_envs, 2, device=self.device)
        self.walk_steps_elapsed = torch.zeros(self.num_envs, device=self.device)
        self.single_support_steps = torch.zeros(self.num_envs, device=self.device)
        self.swing_streak_l = torch.zeros(self.num_envs, device=self.device)
        self.swing_streak_r = torch.zeros(self.num_envs, device=self.device)
        self.swing_count_l = torch.zeros(self.num_envs, device=self.device)
        self.swing_count_r = torch.zeros(self.num_envs, device=self.device)
        self.cum_abs_yaw = torch.zeros(self.num_envs, device=self.device)
        self.last_yaw = torch.zeros(self.num_envs, device=self.device)
        # Final values of the previous episode; written in reset_idx before the
        # per-step buffers are cleared so play_gm can log them after `dones`.
        self.last_episode_success = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)
        self.last_forward_displacement_ratio = torch.zeros(self.num_envs, device=self.device)
        self.last_single_support_ratio = torch.zeros(self.num_envs, device=self.device)
        self.last_swing_count_l = torch.zeros(self.num_envs, device=self.device)
        self.last_swing_count_r = torch.zeros(self.num_envs, device=self.device)
        self.last_cum_abs_yaw = torch.zeros(self.num_envs, device=self.device)
        self._validate_stage_config()
        self.compute_ref_state()
        self._set_reference_commands()

    def _validate_stage_config(self):
        """Validate the retargeted subsection and precompute stage durations."""
        trajectory = self.cfg.trajectory
        self.walk_start_frame = trajectory.steady_cycle_start_frame
        self.walk_cycle_frames = trajectory.steady_cycle_frames
        self.walk_cycle_boundary_frame = self.walk_start_frame + self.walk_cycle_frames
        self.walk_cycle_count = trajectory.steady_walk_cycles
        if (
            self.walk_start_frame < 0
            or self.walk_cycle_frames < 2
            or self.walk_cycle_count < 1
            or self.walk_cycle_boundary_frame >= self.reference_num_frames
        ):
            raise ValueError(
                "Trajectory steady cycle must be inside the packaged reference "
                f"[0, {self.reference_num_frames - 1}]"
            )
        self.walk_total_steps = self.walk_cycle_frames * self.walk_cycle_count
        self.walk_cycle_duration_s = self.walk_cycle_frames / self.reference_rate_hz
        if not np.isclose(trajectory.gait_period_s, self.walk_cycle_duration_s, atol=1e-6):
            raise ValueError(
                f"Configured gait period {trajectory.gait_period_s:g}s does not match "
                f"the {self.walk_cycle_duration_s:g}s reference cycle"
            )
        self.walk_cycle_pos_correction = (
            self.reference_dof_pos[self.walk_start_frame]
            - self.reference_dof_pos[self.walk_cycle_boundary_frame]
        )
        self.walk_cycle_vel_correction = (
            self.reference_dof_vel[self.walk_start_frame]
            - self.reference_dof_vel[self.walk_cycle_boundary_frame]
        )
        self.walk_cycle_xy_displacement = (
            self.reference_root_pos[self.walk_cycle_boundary_frame, :2]
            - self.reference_root_pos[self.walk_start_frame, :2]
        )
        self.stage_duration_steps = {
            self.STAND_INITIAL: round(trajectory.initial_stand_s / self.dt),
            self.START: round(trajectory.start_transition_s / self.dt),
            self.STOP: round(trajectory.stop_transition_s / self.dt),
            self.STAND_FINAL: round(trajectory.final_stand_s / self.dt),
        }
        if any(steps < 1 for steps in self.stage_duration_steps.values()):
            raise ValueError("Each analytic trajectory stage must last at least one control step")

        # exp0.2: reference xy displacement over the whole staged sequence
        # (stand -> start -> walk -> stop -> final stand). Environment origins
        # cancel out, so this is a single scalar shared by all envs. After a
        # reset, reference_origin_xy equals the walk-start xy, making the walk
        # start coincide with the env origin while the initial stand starts at
        # base_init_state[:2] relative to it.
        initial_xy = torch.as_tensor(
            self.base_init_state[:2].to(self.device), dtype=torch.float, device=self.device
        )
        walk_end_lin_vel_xy = self.reference_root_lin_vel[self.walk_start_frame, :2]
        final_xy = (
            self.walk_cycle_count * self.walk_cycle_xy_displacement
            + 0.5 * trajectory.stop_transition_s * walk_end_lin_vel_xy
        )
        self.reference_episode_xy_displacement = float(
            torch.linalg.vector_norm(final_xy - initial_xy)
        )

    @staticmethod
    def _hermite(position_0, velocity_0, position_1, velocity_1, time, duration):
        """Evaluate a cubic Hermite segment and its time derivative."""
        u = (time / duration).clamp(0.0, 1.0).unsqueeze(1)
        h00 = 2 * u**3 - 3 * u**2 + 1
        h10 = u**3 - 2 * u**2 + u
        h01 = -2 * u**3 + 3 * u**2
        h11 = u**3 - u**2
        position = h00 * position_0 + h10 * duration * velocity_0 + h01 * position_1 + h11 * duration * velocity_1
        derivative = (
            ((6 * u**2 - 6 * u) / duration) * position_0
            + (3 * u**2 - 4 * u + 1) * velocity_0
            + ((-6 * u**2 + 6 * u) / duration) * position_1
            + (3 * u**2 - 2 * u) * velocity_1
        )
        return position, derivative

    @staticmethod
    def _quat_slerp(quat_0, quat_1, blend):
        """Spherical interpolation for Isaac Gym xyzw quaternions."""
        dot = torch.sum(quat_0 * quat_1, dim=1, keepdim=True)
        quat_1 = torch.where(dot < 0.0, -quat_1, quat_1)
        dot = dot.abs().clamp(max=1.0)
        angle = torch.acos(dot)
        sin_angle = torch.sin(angle)
        blend = blend.unsqueeze(1)
        linear = (1.0 - blend) * quat_0 + blend * quat_1
        spherical = (
            torch.sin((1.0 - blend) * angle) / sin_angle * quat_0
            + torch.sin(blend * angle) / sin_angle * quat_1
        )
        quat = torch.where(sin_angle > 1e-6, spherical, linear)
        return quat / torch.linalg.vector_norm(quat, dim=1, keepdim=True)

    def _load_reference_trajectory(self):
        """Load the 12-DOF reference and reorder it to Isaac Gym DOF order."""
        path = Path(self.cfg.trajectory.file)
        if not path.is_file():
            raise FileNotFoundError(f"Trajectory file not found: {path}")

        with np.load(path, allow_pickle=False) as motion:
            required = {
                "time", "qpos", "qvel", "joint_names", "rate_hz", "root_pos",
                "root_quat_xyzw", "root_lin_vel", "root_ang_vel",
            }
            missing = required.difference(motion.files)
            if missing:
                raise ValueError(f"Trajectory {path} is missing fields: {sorted(missing)}")

            time = np.asarray(motion["time"], dtype=np.float32)
            qpos = np.asarray(motion["qpos"], dtype=np.float32)
            qvel = np.asarray(motion["qvel"], dtype=np.float32)
            root_pos = np.asarray(motion["root_pos"], dtype=np.float32)
            root_quat = np.asarray(motion["root_quat_xyzw"], dtype=np.float32)
            root_lin_vel = np.asarray(motion["root_lin_vel"], dtype=np.float32)
            root_ang_vel = np.asarray(motion["root_ang_vel"], dtype=np.float32)
            joint_names = tuple(str(name) for name in motion["joint_names"])
            rate_hz = float(motion["rate_hz"])

        if time.ndim != 1 or len(time) < 2 or not np.all(np.diff(time) > 0.0):
            raise ValueError(f"Trajectory {path} must contain increasing time samples")
        if qpos.shape != qvel.shape or qpos.shape != (len(time), len(joint_names)):
            raise ValueError(f"Trajectory {path} has incompatible time, qpos, or qvel shapes")
        root_values = (root_pos, root_quat, root_lin_vel, root_ang_vel)
        if any(values.shape != (len(time), 3 if values is not root_quat else 4) for values in root_values):
            raise ValueError(f"Trajectory {path} has invalid root-state shapes")
        if not all(np.isfinite(values).all() for values in (qpos, qvel, *root_values)):
            raise ValueError(f"Trajectory {path} contains non-finite values")
        if not np.allclose(np.linalg.norm(root_quat, axis=1), 1.0, atol=1e-4):
            raise ValueError(f"Trajectory {path} contains non-unit root quaternions")
        if not np.isclose(rate_hz, self.cfg.trajectory.expected_rate_hz):
            raise ValueError(
                f"Trajectory rate is {rate_hz:g} Hz; expected {self.cfg.trajectory.expected_rate_hz:g} Hz"
            )
        if not np.isclose(1.0 / rate_hz, self.dt):
            raise ValueError(
                f"Trajectory interval {1.0 / rate_hz:g}s does not match environment control step {self.dt:g}s"
            )
        if set(joint_names) != set(self.cfg.trajectory.joint_names):
            raise ValueError("Trajectory joint names do not match the X1 trajectory-task configuration")
        if set(self.dof_names) != set(joint_names):
            raise ValueError(f"Trajectory joints do not match simulator DOFs: {self.dof_names}")

        if self.cfg.trajectory.canonicalize_root:
            (
                root_pos,
                root_quat,
                root_lin_vel,
                root_ang_vel,
                canonical_yaw,
            ) = canonicalize_root_trajectory(
                root_pos,
                root_quat,
                root_lin_vel,
                root_ang_vel,
                self.cfg.trajectory.steady_cycle_start_frame,
            )
            self.reference_canonical_yaw = canonical_yaw
            print(
                "[X1Trajectory] Canonicalized reference root at frame "
                f"{self.cfg.trajectory.steady_cycle_start_frame}; removed yaw "
                f"{np.degrees(canonical_yaw):.2f} deg"
            )
        else:
            self.reference_canonical_yaw = 0.0

        # exp0.6: optional reference playback speed. time_scale < 1 slows the
        # motion down: frames are upsampled by 1/time_scale (positions and
        # quaternions linearly interpolated, quaternions renormalized) and all
        # velocities are multiplied by time_scale so the slowed trajectory is
        # physically consistent. Config frame indices (steady cycle window)
        # must then be expressed in the upsampled timeline.
        time_scale = float(getattr(self.cfg.trajectory, "reference_time_scale", 1.0))
        if not np.isclose(time_scale, 1.0):
            if time_scale <= 0.0:
                raise ValueError("reference_time_scale must be positive")
            old_time = time.astype(np.float64)
            new_num = int(round((len(old_time) - 1) / time_scale)) + 1
            new_time = (np.arange(new_num, dtype=np.float64) / rate_hz).astype(np.float32)
            sampled_t = np.clip(new_time * time_scale, old_time[0], old_time[-1])

            def interp_rows(arr):
                return np.stack(
                    [np.interp(sampled_t, old_time, arr[:, d]) for d in range(arr.shape[1])],
                    axis=1,
                ).astype(np.float32)

            qpos = interp_rows(qpos)
            qvel = interp_rows(qvel) * time_scale
            root_pos = interp_rows(root_pos)
            root_quat = interp_rows(root_quat)
            root_quat /= np.linalg.norm(root_quat, axis=1, keepdims=True)
            root_lin_vel = interp_rows(root_lin_vel) * time_scale
            root_ang_vel = interp_rows(root_ang_vel) * time_scale
            time = new_time
            print(
                f"[X1Trajectory] reference_time_scale={time_scale:g}: resampled "
                f"{len(old_time)} -> {new_num} frames"
            )

        source_index = {name: index for index, name in enumerate(joint_names)}
        sim_order = [source_index[name] for name in self.dof_names]
        residual_scales = self.cfg.trajectory.residual_action_scales
        if set(residual_scales) != set(self.dof_names):
            raise ValueError("Residual action scales must define every simulator DOF exactly once")
        self.residual_action_scales = torch.tensor(
            [residual_scales[name] for name in self.dof_names],
            dtype=torch.float,
            device=self.device,
        )
        self.reference_time = torch.from_numpy(time.copy()).to(self.device)
        self.reference_dof_pos = torch.from_numpy(qpos[:, sim_order].copy()).to(self.device)
        self.reference_dof_vel = torch.from_numpy(qvel[:, sim_order].copy()).to(self.device)
        self.reference_root_pos = torch.from_numpy(root_pos.copy()).to(self.device)
        self.reference_root_quat = torch.from_numpy(root_quat.copy()).to(self.device)
        self.reference_root_lin_vel = torch.from_numpy(root_lin_vel.copy()).to(self.device)
        self.reference_root_ang_vel = torch.from_numpy(root_ang_vel.copy()).to(self.device)
        self.reference_rate_hz = rate_hz
        self.reference_num_frames = len(time)

    def _get_phase(self):
        """Return a phase only while the retargeted steady-walk segment is active."""
        local_time = (self.reference_frame - self.walk_start_frame) / self.reference_rate_hz
        phase = torch.remainder(local_time / self.cfg.trajectory.gait_period_s, 1.0)
        return torch.where(self.motion_stage == self.WALK, phase, torch.zeros_like(phase))

    def compute_ref_state(self):
        """Build a reference from analytic transitions and the steady walk segment."""
        stage = self.motion_stage
        default_dof_pos = self.default_dof_pos.expand(self.num_envs, -1)
        zero_dof_vel = torch.zeros_like(default_dof_pos)
        initial_root_pos = self.base_init_state[:3].expand(self.num_envs, -1).clone()
        initial_root_pos[:, :2] += self.env_origins[:, :2]
        initial_root_quat = self.base_init_state[3:7].expand(self.num_envs, -1)
        zero_root_vel = torch.zeros(self.num_envs, 3, device=self.device)

        walk_start_pos = self.reference_root_pos[self.walk_start_frame].expand(self.num_envs, -1).clone()
        walk_start_pos[:, :2] += self.env_origins[:, :2] - self.reference_origin_xy
        walk_start_dof_pos = self.reference_dof_pos[self.walk_start_frame].expand(self.num_envs, -1)
        walk_start_dof_vel = self.reference_dof_vel[self.walk_start_frame].expand(self.num_envs, -1)
        walk_start_quat = self.reference_root_quat[self.walk_start_frame].expand(self.num_envs, -1)
        walk_start_lin_vel = self.reference_root_lin_vel[self.walk_start_frame].expand(self.num_envs, -1)
        walk_start_ang_vel = self.reference_root_ang_vel[self.walk_start_frame].expand(self.num_envs, -1)
        walk_end_pos = walk_start_pos.clone()
        walk_end_pos[:, :2] += self.walk_cycle_count * self.walk_cycle_xy_displacement
        walk_end_dof_pos = walk_start_dof_pos
        walk_end_dof_vel = walk_start_dof_vel
        walk_end_quat = walk_start_quat
        walk_end_lin_vel = walk_start_lin_vel
        walk_end_ang_vel = walk_start_ang_vel

        self.ref_dof_pos = default_dof_pos.clone()
        self.ref_dof_vel = zero_dof_vel.clone()
        self.ref_root_pos_w = initial_root_pos.clone()
        self.ref_root_quat = initial_root_quat.clone()
        self.ref_root_lin_vel = zero_root_vel.clone()
        self.ref_root_ang_vel = zero_root_vel.clone()

        start_mask = stage == self.START
        if torch.any(start_mask):
            time = self.motion_stage_step[start_mask].float() * self.dt
            duration = self.cfg.trajectory.start_transition_s
            self.ref_dof_pos[start_mask], self.ref_dof_vel[start_mask] = self._hermite(
                default_dof_pos[start_mask], zero_dof_vel[start_mask], walk_start_dof_pos[start_mask],
                walk_start_dof_vel[start_mask], time, duration,
            )
            self.ref_root_pos_w[start_mask], self.ref_root_lin_vel[start_mask] = self._hermite(
                initial_root_pos[start_mask], zero_root_vel[start_mask], walk_start_pos[start_mask],
                walk_start_lin_vel[start_mask], time, duration,
            )
            blend = (time / duration).clamp(0.0, 1.0)
            self.ref_root_quat[start_mask] = self._quat_slerp(
                initial_root_quat[start_mask], walk_start_quat[start_mask], blend
            )
            self.ref_root_ang_vel[start_mask] = blend.unsqueeze(1) * walk_start_ang_vel[start_mask]

        walk_mask = stage == self.WALK
        if torch.any(walk_mask):
            walk_steps = self.motion_stage_step[walk_mask]
            cycle_index = torch.div(walk_steps, self.walk_cycle_frames, rounding_mode="floor")
            cycle_step = torch.remainder(walk_steps, self.walk_cycle_frames)
            walk_frame = self.walk_start_frame + cycle_step
            cycle_time = cycle_step.float() / self.reference_rate_hz
            correction_pos, correction_vel = self._hermite(
                torch.zeros_like(self.reference_dof_pos[walk_frame]),
                torch.zeros_like(self.reference_dof_vel[walk_frame]),
                self.walk_cycle_pos_correction.expand(len(walk_frame), -1),
                self.walk_cycle_vel_correction.expand(len(walk_frame), -1),
                cycle_time,
                self.walk_cycle_duration_s,
            )
            blend = cycle_time / self.walk_cycle_duration_s
            smooth_blend = blend.square() * (3.0 - 2.0 * blend)
            self.reference_frame[walk_mask] = walk_frame
            self.ref_dof_pos[walk_mask] = self.reference_dof_pos[walk_frame] + correction_pos
            self.ref_dof_vel[walk_mask] = self.reference_dof_vel[walk_frame] + correction_vel
            self.ref_root_quat[walk_mask] = self._quat_slerp(
                self.reference_root_quat[walk_frame], walk_start_quat[walk_mask], smooth_blend
            )
            self.ref_root_lin_vel[walk_mask] = self.reference_root_lin_vel[walk_frame]
            self.ref_root_ang_vel[walk_mask] = (
                (1.0 - smooth_blend).unsqueeze(1) * self.reference_root_ang_vel[walk_frame]
                + smooth_blend.unsqueeze(1) * walk_start_ang_vel[walk_mask]
            )
            self.ref_root_pos_w[walk_mask] = self.reference_root_pos[walk_frame]
            self.ref_root_pos_w[walk_mask, :2] += (
                self.env_origins[walk_mask, :2] - self.reference_origin_xy[walk_mask]
                + cycle_index.unsqueeze(1) * self.walk_cycle_xy_displacement
            )

        stop_mask = stage == self.STOP
        if torch.any(stop_mask):
            time = self.motion_stage_step[stop_mask].float() * self.dt
            duration = self.cfg.trajectory.stop_transition_s
            stop_root_pos = walk_end_pos[stop_mask] + 0.5 * duration * walk_end_lin_vel[stop_mask]
            self.ref_dof_pos[stop_mask], self.ref_dof_vel[stop_mask] = self._hermite(
                walk_end_dof_pos[stop_mask], walk_end_dof_vel[stop_mask], default_dof_pos[stop_mask],
                zero_dof_vel[stop_mask], time, duration,
            )
            self.ref_root_pos_w[stop_mask], self.ref_root_lin_vel[stop_mask] = self._hermite(
                walk_end_pos[stop_mask], walk_end_lin_vel[stop_mask], stop_root_pos,
                zero_root_vel[stop_mask], time, duration,
            )
            self.ref_root_quat[stop_mask] = walk_end_quat[stop_mask]
            self.ref_root_ang_vel[stop_mask] = (
                1.0 - (time / duration).clamp(0.0, 1.0).unsqueeze(1)
            ) * walk_end_ang_vel[stop_mask]

        final_mask = stage == self.STAND_FINAL
        if torch.any(final_mask):
            stop_root_pos = walk_end_pos[final_mask] + 0.5 * self.cfg.trajectory.stop_transition_s * walk_end_lin_vel[final_mask]
            self.ref_dof_pos[final_mask] = default_dof_pos[final_mask]
            self.ref_dof_vel[final_mask] = zero_dof_vel[final_mask]
            self.ref_root_pos_w[final_mask] = stop_root_pos
            self.ref_root_quat[final_mask] = walk_end_quat[final_mask]
            self.ref_root_lin_vel[final_mask] = zero_root_vel[final_mask]
            self.ref_root_ang_vel[final_mask] = zero_root_vel[final_mask]

    def _set_reference_commands(self):
        """Supply the reference body velocity through the inherited command channels."""
        ref_lin_vel_b = quat_rotate_inverse(self.ref_root_quat, self.ref_root_lin_vel)
        ref_ang_vel_b = quat_rotate_inverse(self.ref_root_quat, self.ref_root_ang_vel)
        self.commands[:, 0] = ref_lin_vel_b[:, 0]
        self.commands[:, 1] = ref_lin_vel_b[:, 1]
        self.commands[:, 2] = ref_ang_vel_b[:, 2]

    def compute_observations(self):
        """Build observations with explicit current-frame trajectory targets."""
        self.compute_ref_state()
        self._set_reference_commands()
        phase = self._get_phase()
        sin_pos = torch.sin(2 * torch.pi * phase).unsqueeze(1)
        cos_pos = torch.cos(2 * torch.pi * phase).unsqueeze(1)
        self.command_input = torch.cat(
            (sin_pos, cos_pos, self.commands[:, :3] * self.commands_scale), dim=1
        )

        stage_one_hot = torch.nn.functional.one_hot(self.motion_stage, num_classes=self.STAGE_COUNT).float()
        ref_joint_pos = (self.ref_dof_pos - self.default_dof_pos) * self.obs_scales.dof_pos
        ref_joint_vel = self.ref_dof_vel * self.obs_scales.dof_vel
        ref_projected_gravity = quat_rotate_inverse(self.ref_root_quat, self.gravity_vec)
        joint_error = self.dof_pos - self.ref_dof_pos
        stance_mask = self._get_stance_mask()
        contact_mask = self.contact_forces[:, self.feet_indices, 2] > 5.0

        privileged_obs = torch.cat((
            self.command_input,
            (self.dof_pos - self.default_joint_pd_target) * self.obs_scales.dof_pos,
            self.dof_vel * self.obs_scales.dof_vel,
            self.actions,
            joint_error,
            self.base_lin_vel * self.obs_scales.lin_vel,
            self.base_ang_vel * self.obs_scales.ang_vel,
            self.base_euler_xyz * self.obs_scales.quat,
            self.rand_push_force[:, :2],
            self.rand_push_torque,
            self.env_frictions,
            self.body_mass / 10.0,
            stance_mask,
            contact_mask,
            ref_joint_pos,
            ref_joint_vel,
            ref_projected_gravity,
            stage_one_hot,
        ), dim=-1)
        if privileged_obs.shape[1] != self.cfg.env.single_num_privileged_obs:
            raise RuntimeError(f"Expected {self.cfg.env.single_num_privileged_obs} critic features, got {privileged_obs.shape[1]}")

        obs = torch.cat((
            self.command_input,
            (self.dof_pos - self.default_dof_pos) * self.obs_scales.dof_pos,
            self.dof_vel * self.obs_scales.dof_vel,
            self.actions,
            self.base_ang_vel * self.obs_scales.ang_vel,
            self.base_euler_xyz * self.obs_scales.quat,
            ref_joint_pos,
            ref_joint_vel,
            ref_projected_gravity,
            stage_one_hot,
        ), dim=-1)
        if obs.shape[1] != self.cfg.env.num_single_obs:
            raise RuntimeError(f"Expected {self.cfg.env.num_single_obs} actor features, got {obs.shape[1]}")

        self.obs_history.append(obs)
        self.critic_history.append(privileged_obs)
        self.obs_buf = torch.stack(tuple(self.obs_history), dim=1).reshape(self.num_envs, -1)
        self.privileged_obs_buf = torch.cat(tuple(self.critic_history), dim=1)

    def _post_physics_step_callback(self):
        """Advance the five-stage state machine by one policy step."""
        self.motion_stage_step += 1
        stage = self.motion_stage
        transitions = (
            (self.STAND_INITIAL, self.START, self.stage_duration_steps[self.STAND_INITIAL]),
            (self.START, self.WALK, self.stage_duration_steps[self.START]),
            (self.WALK, self.STOP, self.walk_total_steps),
            (self.STOP, self.STAND_FINAL, self.stage_duration_steps[self.STOP]),
        )
        for current_stage, next_stage, duration in transitions:
            change = (stage == current_stage) & (self.motion_stage_step >= duration)
            self.motion_stage[change] = next_stage
            self.motion_stage_step[change] = 0
        self.motion_ended = (
            (self.motion_stage == self.STAND_FINAL)
            & (self.motion_stage_step >= self.stage_duration_steps[self.STAND_FINAL])
        )
        self.compute_ref_state()
        self._set_reference_commands()
        self._update_episode_diagnostics()

    def _update_episode_diagnostics(self):
        """Accumulate exp0.2 per-step diagnostics (success criteria + CSV)."""
        walk_mask = (self.motion_stage == self.WALK).float()
        contact = self.contact_forces[:, self.feet_indices, 2] > 5.0
        single_support = contact[:, 0] ^ contact[:, 1]
        self.walk_steps_elapsed += walk_mask
        self.single_support_steps += single_support.float() * walk_mask
        for foot, streak, count in (
            (0, self.swing_streak_l, self.swing_count_l),
            (1, self.swing_streak_r, self.swing_count_r),
        ):
            airborne = ~contact[:, foot]
            streak += airborne.float()
            streak[~airborne] = 0.0
            count += (airborne & (streak == self.SWING_MIN_STEPS)).float()
        yaw = self.base_euler_xyz[:, 2]
        delta_yaw = torch.remainder(yaw - self.last_yaw + torch.pi, 2 * torch.pi) - torch.pi
        self.cum_abs_yaw += delta_yaw.abs()
        self.last_yaw = yaw

    def check_termination(self):
        super().check_termination()
        # Completing the staged motion is a successful finite-horizon
        # truncation, not a failure that should receive the termination penalty.
        successful_end = self.motion_ended & ~self.reset_buf.bool()
        self.time_out_buf |= successful_end
        self.reset_buf |= self.motion_ended
        # exp0.2: record (never reward) full-trajectory success. Condition 1 is
        # successful_end itself; an unplanned reset clears the counters below
        # and can never reach the final step.
        displacement = torch.linalg.vector_norm(
            self.root_states[:, :2] - self.episode_root_start_xy, dim=1
        )
        self.episode_success_buf |= successful_end & (
            (displacement / self.reference_episode_xy_displacement >= 0.5)
            & (self.single_support_steps / self.walk_steps_elapsed.clamp(min=1.0) >= 0.3)
            & (self.swing_count_l >= 3)
            & (self.swing_count_r >= 3)
            & (self.cum_abs_yaw <= 3.0)
        )

    def reset_idx(self, env_ids):
        """Reset selected environments to the analytic initial standing state."""
        if len(env_ids) == 0:
            return

        # exp0.2: snapshot the finished episode's diagnostics before the state
        # reset overwrites the root position and clears the per-step counters.
        final_displacement = torch.linalg.vector_norm(
            self.root_states[env_ids, :2] - self.episode_root_start_xy[env_ids], dim=1
        )
        self.last_episode_success[env_ids] = self.episode_success_buf[env_ids]
        self.last_forward_displacement_ratio[env_ids] = (
            final_displacement / self.reference_episode_xy_displacement
        )
        self.last_single_support_ratio[env_ids] = self.single_support_steps[env_ids] / self.walk_steps_elapsed[env_ids].clamp(min=1.0)
        self.last_swing_count_l[env_ids] = self.swing_count_l[env_ids]
        self.last_swing_count_r[env_ids] = self.swing_count_r[env_ids]
        self.last_cum_abs_yaw[env_ids] = self.cum_abs_yaw[env_ids]

        # exp0.4: split resets between the full staged sequence and random
        # mid-WALK phases so the policy practices balance across the whole
        # gait cycle instead of only from a static standstill start.
        random_prob = getattr(self.cfg.env, "random_phase_reset_prob", 0.0)
        use_random_phase = (
            torch.rand(len(env_ids), device=self.device) < random_prob
        )
        random_ids = env_ids[use_random_phase]
        staged_ids = env_ids[~use_random_phase]

        if len(random_ids) > 0:
            # Drop these envs directly into a random WALK step. The reference
            # state machine is set first so compute_ref_state() produces the
            # exact (cycle-corrected) targets for that phase, and the robot
            # is initialized to match its upcoming PD target.
            walk_steps = torch.randint(
                0, self.walk_total_steps, (len(random_ids),), device=self.device
            )
            self.motion_stage[random_ids] = self.WALK
            self.motion_stage_step[random_ids] = walk_steps
            self.motion_ended[random_ids] = False
            self.reference_origin_xy[random_ids] = self.reference_root_pos[
                self.walk_start_frame, :2
            ]

        self.reference_frame[staged_ids] = self.walk_start_frame
        self.reference_origin_xy[staged_ids] = self.reference_root_pos[self.walk_start_frame, :2]
        self.motion_stage[staged_ids] = self.STAND_INITIAL
        self.motion_stage_step[staged_ids] = 0
        self.motion_ended[staged_ids] = False
        self.compute_ref_state()

        self.dof_pos[env_ids] = self.ref_dof_pos[env_ids]
        self.dof_vel[env_ids] = self.ref_dof_vel[env_ids]
        self.root_states[env_ids, :3] = self.ref_root_pos_w[env_ids]
        self.root_states[env_ids, 3:7] = self.ref_root_quat[env_ids]
        self.root_states[env_ids, 7:10] = self.ref_root_lin_vel[env_ids]
        self.root_states[env_ids, 10:13] = self.ref_root_ang_vel[env_ids]
        if len(random_ids) > 0:
            # Small state jitter so mid-gait balance is not initialized to the
            # exact reference (BeyondMimic-style pose/velocity disturbance).
            self.dof_pos[random_ids] += 0.03 * (
                2.0 * torch.rand_like(self.dof_pos[random_ids]) - 1.0
            )
            self.root_states[random_ids, 7:10] += 0.05 * (
                2.0 * torch.rand_like(self.root_states[random_ids, 7:10]) - 1.0
            )
        env_ids_int32 = env_ids.to(dtype=torch.int32)
        self.gym.set_dof_state_tensor_indexed(
            self.sim, gymtorch.unwrap_tensor(self.dof_state), gymtorch.unwrap_tensor(env_ids_int32), len(env_ids)
        )
        self.gym.set_actor_root_state_tensor_indexed(
            self.sim, gymtorch.unwrap_tensor(self.root_states), gymtorch.unwrap_tensor(env_ids_int32), len(env_ids)
        )
        # exp0.2: the fresh standing pose is the episode displacement origin.
        self.episode_root_start_xy[env_ids] = self.root_states[env_ids, :2]
        self.episode_success_buf[env_ids] = False
        self.walk_steps_elapsed[env_ids] = 0.0
        self.single_support_steps[env_ids] = 0.0
        self.swing_streak_l[env_ids] = 0.0
        self.swing_streak_r[env_ids] = 0.0
        self.swing_count_l[env_ids] = 0.0
        self.swing_count_r[env_ids] = 0.0
        self.cum_abs_yaw[env_ids] = 0.0

        self.commands[env_ids] = 0.0
        self._set_reference_commands()
        self.last_last_actions[env_ids] = 0.0
        self.actions[env_ids] = 0.0
        self.last_actions[env_ids] = 0.0
        self.last_torques[env_ids] = 0.0
        self.last_rigid_state[env_ids] = 0.0
        self.last_contact_forces[env_ids] = 0.0
        self.last_dof_vel[env_ids] = self.ref_dof_vel[env_ids]
        self.last_root_vel[env_ids] = self.root_states[env_ids, 7:13]
        self.feet_air_time[env_ids] = 0.0
        self.episode_length_buf[env_ids] = 0
        self.reset_buf[env_ids] = 1
        self.extras["episode"] = {}
        for key in self.episode_sums:
            self.extras["episode"]["rew_" + key] = torch.mean(self.episode_sums[key][env_ids]) / self.max_episode_length_s
            self.episode_sums[key][env_ids] = 0.0
        # exp0.2: fraction of reset envs that completed the full trajectory
        # while meeting all six success conditions (logging only, no reward).
        self.extras["episode"]["episode_success"] = torch.mean(self.last_episode_success[env_ids].float())
        if self.cfg.env.send_timeouts:
            self.extras["time_outs"] = self.time_out_buf
        for history in self.obs_history:
            history[env_ids] = 0.0
        for history in self.critic_history:
            history[env_ids] = 0.0

        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_net_contact_force_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)
        self.base_quat[env_ids] = self.root_states[env_ids, 3:7]
        self.base_euler_xyz = get_euler_xyz_tensor(self.base_quat)
        self.projected_gravity[env_ids] = quat_rotate_inverse(self.base_quat[env_ids], self.gravity_vec[env_ids])
        self.base_lin_vel[env_ids] = quat_rotate_inverse(self.base_quat[env_ids], self.root_states[env_ids, 7:10])
        self.base_ang_vel[env_ids] = quat_rotate_inverse(self.base_quat[env_ids], self.root_states[env_ids, 10:13])
        # exp0.2: seed the yaw integrator with the reset orientation so the
        # first post-reset step does not inject a wrap-around jump.
        self.last_yaw[env_ids] = self.base_euler_xyz[env_ids, 2]

    def _compute_torques(self, actions):
        """Track the reference with an action residual and velocity feed-forward."""
        target_pos = self.ref_dof_pos + actions * self.residual_action_scales
        joint_limit_margin = self.cfg.trajectory.joint_limit_margin_rad
        target_lower = self.dof_pos_limits[:, 0] + joint_limit_margin
        target_upper = self.dof_pos_limits[:, 1] - joint_limit_margin
        if torch.any(target_lower >= target_upper):
            raise RuntimeError("Joint-limit margin leaves an empty target range")
        target_pos = torch.maximum(torch.minimum(target_pos, target_upper), target_lower)
        self.pd_target_dof_pos = target_pos
        torques = self.p_gains * (target_pos - self.dof_pos) + self.d_gains * (self.ref_dof_vel - self.dof_vel)
        return torch.clip(torques, -self.torque_limits, self.torque_limits)

    def _reward_trajectory_joint_pos(self):
        return torch.exp(-torch.mean((self.dof_pos - self.ref_dof_pos).square(), dim=1) / 0.15**2)

    def _reward_trajectory_joint_vel(self):
        return torch.exp(-torch.mean((self.dof_vel - self.ref_dof_vel).square(), dim=1) / 2.0**2)

    def _reward_trajectory_root_pos(self):
        return torch.exp(-torch.sum((self.root_states[:, :3] - self.ref_root_pos_w).square(), dim=1) / 0.25**2)

    def _reward_trajectory_root_ori(self):
        dot = torch.sum(self.base_quat * self.ref_root_quat, dim=1).abs().clamp(max=1.0)
        return torch.exp(-(1.0 - dot.square()) / 0.15**2)

    def _reward_trajectory_root_lin_vel(self):
        return torch.exp(-torch.mean((self.root_states[:, 7:10] - self.ref_root_lin_vel).square(), dim=1) / 0.5**2)

    def _reward_trajectory_root_ang_vel(self):
        return torch.exp(-torch.mean((self.root_states[:, 10:13] - self.ref_root_ang_vel).square(), dim=1) / 1.5**2)

    def _reward_forward_progress(self):
        """Match the world-frame speed along the reference walking heading.

        A local velocity signal without the position dead zone of
        `trajectory_root_pos`: standing still while the reference walks keeps
        this reward near zero instead of near one.
        """
        heading = self.walk_cycle_xy_displacement / self.walk_cycle_xy_displacement.norm().clamp(min=1e-8)
        world_lin_vel = self.root_states[:, 7:10]
        v_along = world_lin_vel[:, 0] * heading[0] + world_lin_vel[:, 1] * heading[1]
        v_ref = self.ref_root_lin_vel[:, 0] * heading[0] + self.ref_root_lin_vel[:, 1] * heading[1]
        return torch.exp(-torch.square(v_along - v_ref) / 0.3**2)

    def _reward_forward_overspeed(self):
        """Penalize forward speed overshoot beyond 1.5x the reference speed.

        exp0.4 replay showed vx peaking at 2.83 m/s against a 0.6 m/s
        reference right before the fall; `forward_progress` alone does not
        stop this lunge because its exp kernel is symmetric and already
        saturated on the high side. Below the threshold this term is zero,
        so standing (v_ref ~= 0, threshold ~= 0) is unaffected.
        """
        heading = self.walk_cycle_xy_displacement / self.walk_cycle_xy_displacement.norm().clamp(min=1e-8)
        world_lin_vel = self.root_states[:, 7:10]
        v_along = world_lin_vel[:, 0] * heading[0] + world_lin_vel[:, 1] * heading[1]
        v_ref = self.ref_root_lin_vel[:, 0] * heading[0] + self.ref_root_lin_vel[:, 1] * heading[1]
        threshold = 1.5 * v_ref.abs()
        overshoot = (v_along - threshold).clamp(min=0.0)
        # exp0.5r fix: return a positive penalty magnitude; legged_gym
        # combines reward terms as scale * term, and the config scale is
        # negative (-1.0). The previous minus sign here inverted the whole
        # term into a lunge reward (exp0.5 overspeed "reward" rose 0.28 ->
        # 0.65 and episodes never grew past ~100 steps).
        return torch.square(overshoot) / 0.3**2

    def _reward_single_support(self):
        """Reward having exactly one foot on the ground during the WALK stage.

        Phase-agnostic on purpose: the analytic stance mask has not been
        verified against the actual NPZ foot phases, and hard-binding the
        reward to a wrong foot would reward the inverted gait. Timing stays
        with the joint-tracking rewards; this term only breaks the permanent
        double-support local optimum.
        """
        contact = self.contact_forces[:, self.feet_indices, 2] > 5.0
        single_support = contact[:, 0] ^ contact[:, 1]
        return (single_support & (self.motion_stage == self.WALK)).float()
