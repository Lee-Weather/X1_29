"""Configuration for command-driven AMP walking (no reference feedforward)."""

from humanoid import LEGGED_GYM_ROOT_DIR
from humanoid.envs.x1.x1_amp_config import X1AmpCfg, X1AmpCfgPPO


class X1AmpWalkCfg(X1AmpCfg):
    """AMP walking shaped by a velocity command and the style discriminator.

    Inherits observations, AMP machinery, penalties, and the raised walking
    PD gains from X1AmpCfg; the staged reference tracking is bypassed by
    X1AmpWalkEnv (constant walk command, default-relative actions).
    """

    class walk:
        # exp0.9r4: 0.25 m/s matches the yz demo's measured speed (0.244) so
        # the velocity command and the discriminator's reference gait pull
        # in the same direction instead of fighting each other.
        speed = 0.25
        base_height = 0.62  # yz demo root height band 0.612-0.632 m
        # exp0.9r6 (plan B): spawn mid-walk with the command speed injected.
        # The yz frame-0 pose is mid-stride; stationary spawns fall in ~1.2 s
        # open-loop (r5 gate) and waste most of each episode on recovery.
        # Set False for the plan-A stationary control run.
        spawn_with_velocity = True

    class control(X1AmpCfg.control):
        # exp0.9r4: 0.25 -> 0.20. r3 ran 8 of 12 joints pinned at |a|>0.9;
        # a smaller step shrinks the per-action damage radius of the
        # burst-and-recover cycle seen in the r3 replay.
        action_scale = 0.20

    class trajectory(X1AmpCfg.trajectory):
        # exp0.9r4: swap the AMP demo to the yz walk (466 frames @100 Hz,
        # two 2.33 s cycles tiled from the 30 Hz source CSV; validated
        # offline: L/R antiphase knees, vx 0.244, clean Hermite seams).
        # The 0.6 m/s demo inherited from X1AmpCfg was dynamically
        # infeasible (exp0.8t) and 2x the command speed.
        motion_file = (
            f"{LEGGED_GYM_ROOT_DIR}/resources/motions/x1/"
            "motion_walk_yz_0.26ms_x1_12d_100hz.npz"
        )
        reference_time_scale = 1.0  # no resampling: demo is already 100 Hz
        steady_cycle_start_frame = 0  # demo starts mid-gait, no stand prefix
        steady_cycle_frames = 233
        gait_period_s = 2.33

    class rewards(X1AmpCfg.rewards):
        class scales(X1AmpCfg.rewards.scales):
            # Reference-tethered terms are meaningless without the reference.
            trajectory_joint_pos = 0.0
            trajectory_root_pos = 0.0
            trajectory_root_lin_vel = 0.0
            forward_overspeed = 0.0
            # Velocity command tracking (forward_progress compares against
            # the constant walk command via ref_root_lin_vel).
            forward_progress = 1.5
            single_support = 0.8
            alive = 0.5
            base_height = 0.5
            walk_orientation = 0.5
            termination = -50.0
            # exp0.9r4: penalize joint velocity bursts (r3 replay showed
            # vx spiking to 3.3 m/s with hip torques near limit).
            dof_vel = -0.005


class X1AmpWalkCfgPPO(X1AmpCfgPPO):
    """PPO + AMP training configuration for the command-driven walk task."""

    seed = 17

    class runner(X1AmpCfgPPO.runner):
        experiment_name = "x1_amp_walk"
        run_name = "exp0_9r4_amp_walk"
