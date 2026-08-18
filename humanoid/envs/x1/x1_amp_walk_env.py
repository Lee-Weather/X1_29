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
"""

import torch

from humanoid.envs.x1.x1_amp_env import X1AmpEnv


class X1AmpWalkEnv(X1AmpEnv):
    """AMP walking with a constant velocity command and no reference tracking."""

    def __init__(self, cfg, sim_params, physics_engine, sim_device, headless):
        cfg.env.random_phase_reset_prob = 0.0  # always reset to standing
        super().__init__(cfg, sim_params, physics_engine, sim_device, headless)

    def compute_ref_state(self):
        """Replace the staged reference with a constant walk command.

        The observation slots keep their layout (79 single-frame dims), but
        reference-dependent blocks become constants: default pose, zero joint
        velocities, upright orientation, and a root velocity equal to the
        walk command. Rewards that compare against these slots degenerate
        into pure velocity tracking.
        """
        walk_speed = self.cfg.walk.speed
        self.ref_dof_pos = self.default_dof_pos.repeat(self.num_envs, 1)
        self.ref_dof_vel = torch.zeros_like(self.ref_dof_pos)
        self.ref_root_quat = self.base_init_state[3:7].repeat(self.num_envs, 1).to(self.device)
        self.ref_root_lin_vel = torch.zeros(self.num_envs, 3, device=self.device)
        self.ref_root_lin_vel[:, 0] = walk_speed
        self.ref_root_ang_vel = torch.zeros(self.num_envs, 3, device=self.device)
        self.ref_root_pos_w = self.root_states[:, :3].clone()

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
        return torch.ones(self.num_envs, device=self.device)

    def _reward_base_height(self):
        """Reward staying inside the nominal walking height band."""
        target = self.cfg.walk.base_height
        return torch.exp(-torch.square(self.root_states[:, 2] - target) / 0.05**2)

    def _reward_walk_orientation(self):
        """Keep the torso upright (projected gravity z close to -1)."""
        return torch.exp(-torch.square(self.projected_gravity[:, 2] + 1.0) / 0.2**2)
