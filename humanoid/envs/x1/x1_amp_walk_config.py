"""Configuration for command-driven AMP walking (no reference feedforward)."""

from humanoid.envs.x1.x1_amp_config import X1AmpCfg, X1AmpCfgPPO


class X1AmpWalkCfg(X1AmpCfg):
    """AMP walking shaped by a velocity command and the style discriminator.

    Inherits observations, AMP machinery, penalties, and the raised walking
    PD gains from X1AmpCfg; the staged reference tracking is bypassed by
    X1AmpWalkEnv (constant walk command, default-relative actions).
    """

    class walk:
        speed = 0.3  # m/s forward command (matches the half-speed demo)
        base_height = 0.62  # nominal torso height while walking [m]

    class control(X1AmpCfg.control):
        # Uniform default-relative action scale (robolab RPO-Amp value).
        action_scale = 0.25

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


class X1AmpWalkCfgPPO(X1AmpCfgPPO):
    """PPO + AMP training configuration for the command-driven walk task."""

    seed = 15

    class runner(X1AmpCfgPPO.runner):
        experiment_name = "x1_amp_walk"
        run_name = "exp0_9r_amp_walk"
