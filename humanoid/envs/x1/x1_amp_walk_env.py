"""Command-driven AMP walking: no per-step reference feedforward.

Diagnostics across exp0.1t / exp0.6t / exp0.8t proved the retargeted walk
reference is dynamically infeasible for X1_29: pure-PD replay falls at 1.67 s
(full speed), 1.70 s (half speed), and 1.59 s (half speed with 2x stiffer PD
and 3x smaller tracking error). Better tracking makes the fall EARLIER, so
no reward shaping or PD tuning around this reference can converge.

This task pivots to the robolab RPO-Amp recipe instead: actions are position
offsets from the default pose (uniform scale), the D term uses measured
velocities, and the gait is shaped by the AMP discriminator plus a constant
forward-velocity command. The five-stage state machine is bypassed - every
env walks continuously until it falls or times out.

exp0.9r3: spawn/default pose come from the NPZ standing frame (frame 0),
whose FK was validated in exp0.1t; the r2 variant spawned at init_state
(z=0.7 with a mismatched default pose) and never survived the first step.
"""

import torch
from isaacgym import gymtorch

from humanoid.envs.x1.x1_amp_env import X1AmpEnv


class X1AmpWalkEnv(X1AmpEnv):
    """AMP walking with a constant velocity command and no reference tracking."""

    def __init__(self, cfg, sim_params, physics_engine, sim_device, headless):
        cfg.env.random_phase_reset_prob = 0.0  # always reset to standing
        super().__init__(cfg, sim_params, physics_engine, sim_device, headless)
        # exp0.9r3: adopt the NPZ standing pose as the default pose. Zero-action
        # PD targets then hold the FK-consistent stand (exp0.1t-validated),
        # and the (dof_pos - default) observation normalization matches spawn.
        self.default_dof_pos = self.reference_dof_pos[0].clone()

    @property
    def stand_root_z(self):
        """Root height of the NPZ standing frame (0.6179 m, FK-consistent)."""
        return float(self.reference_root_pos[0, 2])

    def compute_ref_state(self):
        """Replace the staged reference with a constant walk command.

        The observation slots keep their layout (79 single-frame dims), but
        reference-dependent blocks become constants: standing pose, zero joint
        velocities, upright orientation, and a root velocity equal to the walk
        command. Rewards that compare against these slots degenerate into
        pure velocity tracking.

        ref_root_pos_w must hold the nominal standing spawn (NPZ frame-0
        height, current xy): reset_idx copies it into the root state, so
        cloning the fallen robot's position would spawn every episode at
        ground level (exp0.9 bug).
        """
        walk_speed = self.cfg.walk.speed
        self.ref_dof_pos = self.default_dof_pos.repeat(self.num_envs, 1)
        self.ref_dof_vel = torch.zeros_like(self.ref_dof_pos)
        self.ref_root_quat = torch.zeros(self.num_envs, 4, device=self.device)
        self.ref_root_quat[:, 3] = 1.0  # identity (upright)
        self.ref_root_lin_vel = torch.zeros(self.num_envs, 3, device=self.device)
        self.ref_root_lin_vel[:, 0] = walk_speed
        self.ref_root_ang_vel = torch.zeros(self.num_envs, 3, device=self.device)
        self.ref_root_pos_w = torch.zeros(self.num_envs, 3, device=self.device)
        self.ref_root_pos_w[:, 0] = self.root_states[:, 0]
        self.ref_root_pos_w[:, 1] = self.root_states[:, 1]
        self.ref_root_pos_w[:, 2] = self.stand_root_z

    def reset_idx(self, env_ids):
        """Spawn at the NPZ standing state (no staged/random phases).

        exp0.9r3 accounting fix: the r2 override forgot to zero
        episode_length_buf, so after the first 920 steps every step tripped
        time_out and episodes collapsed to length 1 forever.

        r5 fix: do NOT clear reset_buf here. step() returns reset_buf as the
        done flags, so clearing it before the return swallowed every
        termination - rewbuffer/lenbuffer never filled, Train/mean_reward
        and Train/mean_episode_length never logged, and replay CSVs showed
        a false "0 terminations / 100% survival". check_termination
        reassigns reset_buf from scratch each step, so no stale flags leak.
        """
        if len(env_ids) == 0:
            return
        self.compute_ref_state()
        self.dof_pos[env_ids] = self.ref_dof_pos[env_ids]
        self.dof_vel[env_ids] = self.ref_dof_vel[env_ids]
        self.root_states[env_ids, :3] = self.ref_root_pos_w[env_ids]
        self.root_states[env_ids, 3:7] = self.ref_root_quat[env_ids]
        # Spawn stationary: the forward speed must be earned by the policy,
        # not injected at reset.
        self.root_states[env_ids, 7:10] = 0.0
        self.root_states[env_ids, 10:13] = 0.0
        env_ids_int32 = env_ids.to(dtype=torch.int32)
        self.gym.set_dof_state_tensor_indexed(
            self.sim, gymtorch.unwrap_tensor(self.dof_state), gymtorch.unwrap_tensor(env_ids_int32), len(env_ids)
        )
        self.gym.set_actor_root_state_tensor_indexed(
            self.sim, gymtorch.unwrap_tensor(self.root_states), gymtorch.unwrap_tensor(env_ids_int32), len(env_ids)
        )
        self.episode_length_buf[env_ids] = 0
        self.time_out_buf[env_ids] = False
        self.episode_root_start_xy[env_ids] = self.root_states[env_ids, :2]
        self.episode_success_buf[env_ids] = False
        self.walk_steps_elapsed[env_ids] = 0.0
        self.single_support_steps[env_ids] = 0.0
        self.swing_streak_l[env_ids] = 0.0
        self.swing_streak_r[env_ids] = 0.0
        self.swing_count_l[env_ids] = 0.0
        self.swing_count_r[env_ids] = 0.0
        self.cum_abs_yaw[env_ids] = 0.0

    def _post_physics_step_callback(self):
        """Stay in WALK forever; only the diagnostics accumulator runs."""
        self.motion_stage[:] = self.WALK
        self.motion_stage_step += 1
        self.motion_ended[:] = False
        self._update_episode_diagnostics()

    def _compute_torques(self, actions):
        """Default-relative position targets, D term on measured velocity."""
        target_pos = self.default_dof_pos + actions * self.cfg.control.action_scale
        margin = self.cfg.trajectory.joint_limit_margin_rad
        target_lower = self.dof_pos_limits[:, 0] + margin
        target_upper = self.dof_pos_limits[:, 1] - margin
        target_pos = torch.maximum(torch.minimum(target_pos, target_upper), target_lower)
        self.pd_target_dof_pos = target_pos
        torques = self.p_gains * (target_pos - self.dof_pos) - self.d_gains * self.dof_vel
        return torch.clip(torques, -self.torque_limits, self.torque_limits)

    def _reward_alive(self):
        """exp0.9r4: alive credit only while standing tall enough.

        The r3 replay showed a 1.5 s limit cycle: sprint to 3.3 m/s, collapse
        to bh 0.13, and spring back up - all while collecting unconditional
        alive credit. Requiring root height above 0.45 m (and a roughly
        upright torso) closes the face-down farming channel; the recovery
        behavior itself is not punished because the reward just goes to zero.
        """
        upright = (self.root_states[:, 2] > 0.45) & (self.projected_gravity[:, 2] < -0.7)
        return upright.float()

    def _reward_forward_progress(self):
        """exp0.9r4: two-sided speed kernel instead of the parent's Gaussian.

        The parent kernel exp(-(v-cmd)^2/0.3^2) saturates to zero for fast
        sprints - overshooting is worth as little as standing still, so the
        burst cycle costs nothing. This version is full credit inside
        [0.8, 1.2]x command, decays to zero by 1.8x, and turns LINEARLY
        NEGATIVE beyond 1.8x (capped at -1) so sprinting is strictly worse
        than standing.
        """
        cmd = self.cfg.walk.speed
        v = self.base_lin_vel[:, 0]
        lo, hi = 0.8 * cmd, 1.2 * cmd
        over = 1.8 * cmd
        reward = torch.zeros_like(v)
        # undershoot: linear ramp 0 -> 1 up to 0.8x command
        undershoot = v < lo
        reward[undershoot] = torch.clamp(v[undershoot] / lo, min=0.0, max=1.0)
        in_band = (v >= lo) & (v <= hi)
        reward[in_band] = 1.0
        # overshoot: 1 -> 0 across [1.2, 1.8]x command
        overshoot_band = (v > hi) & (v <= over)
        reward[overshoot_band] = 1.0 - (v[overshoot_band] - hi) / (over - hi)
        # hard penalty beyond 1.8x, linear down to -1 at 2.6x command
        sprint = v > over
        reward[sprint] = torch.clamp(-(v[sprint] - over) / (0.8 * cmd), min=-1.0, max=0.0)
        return reward

    def _reward_base_height(self):
        """Reward staying inside the nominal walking height band."""
        target = self.cfg.walk.base_height
        return torch.exp(-torch.square(self.root_states[:, 2] - target) / 0.08**2)

    def _reward_walk_orientation(self):
        """Keep the torso upright (projected gravity z close to -1)."""
        return torch.exp(-torch.square(self.projected_gravity[:, 2] + 1.0) / 0.2**2)
