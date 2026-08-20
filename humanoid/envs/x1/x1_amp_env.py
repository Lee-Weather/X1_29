"""AMP-style reward extension of the staged X1 trajectory tracking task.

The discriminator compares 3-step proprioceptive windows between the policy
rollout and the packaged reference NPZ. Both sides must use identical
conventions, otherwise the style reward silently degrades to noise:

  * velocities in the base frame: the sim side uses self.base_lin_vel /
    self.base_ang_vel (already base-frame); the NPZ root velocities are
    world-frame and are rotated with the reference quaternion.
  * joint positions are absolute (not offset by the default pose) and joint
    velocities are unscaled, on both sides.
  * DOF order is the simulator order (reference_dof_pos was already reordered
    by _load_reference_trajectory).
"""

import torch
from isaacgym.torch_utils import quat_rotate_inverse

from humanoid.envs.x1.x1_trajectory_env import X1TrajectoryEnv


class X1AmpEnv(X1TrajectoryEnv):
    """Trajectory task whose gait style is additionally scored by a discriminator."""

    AMP_STEPS = 3
    AMP_OBS_DIM = 30  # base_ang_vel(3) + base_lin_vel(3) + dof_pos(12) + dof_vel(12)

    def __init__(self, cfg, sim_params, physics_engine, sim_device, headless):
        super().__init__(cfg, sim_params, physics_engine, sim_device, headless)
        self.num_amp_obs = self.AMP_OBS_DIM * self.AMP_STEPS
        self._build_demo_amp_obs()
        self.amp_history = torch.zeros(
            self.num_envs, self.AMP_STEPS, self.AMP_OBS_DIM, device=self.device)
        self.amp_obs_buf = torch.zeros(self.num_envs, self.num_amp_obs, device=self.device)

    def _build_demo_amp_obs(self):
        """Precompute stacked discriminator windows for the walk demo.

        exp0.9r5: the demo window now covers the WHOLE reference file after the
        first steady-cycle frame (not just one cycle). The v4 single-cycle demo
        gave the discriminator only 233 training frames, which it separated
        from rollouts almost instantly (disc loss 7.2 -> 0.001 in 60 iters).
        The full-length yz reference (1381 frames / 5.9 cycles) multiplies the
        demo support ~6x; history taps are still clamped to the window start
        so every stacked sample stays inside it.
        """
        # World-frame reference velocities -> base frame, matching the sim side.
        ref_ang_vel_b = quat_rotate_inverse(self.reference_root_quat, self.reference_root_ang_vel)
        ref_lin_vel_b = quat_rotate_inverse(self.reference_root_quat, self.reference_root_lin_vel)
        single = torch.cat(
            (ref_ang_vel_b, ref_lin_vel_b, self.reference_dof_pos, self.reference_dof_vel), dim=1)
        if single.shape[1] != self.AMP_OBS_DIM:
            raise RuntimeError(
                f"Expected {self.AMP_OBS_DIM} AMP features per frame, got {single.shape[1]}")

        lo = self.walk_start_frame
        hi = self.reference_num_frames  # full tail: all cycles of the demo
        frames = torch.arange(lo, hi, device=self.device)
        # Oldest frame first, matching amp_history ordering on the sim side.
        stacked = torch.stack(
            [single[torch.clamp(frames - k, min=lo)] for k in range(self.AMP_STEPS - 1, -1, -1)],
            dim=1,
        ).reshape(len(frames), -1)
        self.demo_amp_obs_stacked = stacked

    def _update_amp_obs(self):
        single = torch.cat((self.base_ang_vel, self.base_lin_vel, self.dof_pos, self.dof_vel), dim=1)
        self.amp_history = torch.cat((self.amp_history[:, 1:], single.unsqueeze(1)), dim=1)
        self.amp_obs_buf = self.amp_history.reshape(self.num_envs, -1)

    def post_physics_step(self):
        # super() already ran check_termination and reset_idx, so a freshly
        # reset env contributes one post-reset frame after its zeroed history.
        super().post_physics_step()
        self._update_amp_obs()

    def reset_idx(self, env_ids):
        super().reset_idx(env_ids)
        # super() refreshes base_quat from the reset root states but leaves
        # base_lin_vel/base_ang_vel at their pre-reset (terminal) values; the
        # first post-reset AMP window would otherwise embed the fallen state's
        # velocities. Recompute them from the reset root state.
        self.base_lin_vel[env_ids] = quat_rotate_inverse(
            self.base_quat[env_ids], self.root_states[env_ids, 7:10])
        self.base_ang_vel[env_ids] = quat_rotate_inverse(
            self.base_quat[env_ids], self.root_states[env_ids, 10:13])
        self.amp_history[env_ids] = 0.0

    def get_amp_observations(self):
        return self.amp_obs_buf

    def get_amp_reward_mask(self):
        """Style rewards only apply while the env is in the steady-walk stage;
        standing/transition phases are judged against a walk discriminator and
        would otherwise be penalized for correctly standing still."""
        return (self.motion_stage == self.WALK).float()

    def sample_demo_amp_obs(self, num_samples):
        idx = torch.randint(
            0, self.demo_amp_obs_stacked.shape[0], (num_samples,), device=self.device)
        return self.demo_amp_obs_stacked[idx]
