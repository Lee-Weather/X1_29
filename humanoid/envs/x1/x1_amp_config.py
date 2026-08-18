"""AMP-style reward configuration for the staged X1 trajectory task."""

from humanoid.envs.x1.x1_trajectory_config import X1TrajectoryCfg, X1TrajectoryCfgPPO


class X1AmpCfg(X1TrajectoryCfg):
    """Trajectory task whose gait style is scored by an AMP discriminator.

    Inherits the five-stage state machine, observations, resets, and reference
    trajectory from X1TrajectoryCfg; only the reward mix changes.
    """

    class env(X1TrajectoryCfg.env):
        # exp0.4: fraction of resets that drop the env directly into a random
        # mid-WALK phase instead of the staged stand->walk->stand sequence.
        random_phase_reset_prob = 0.8

    class rewards(X1TrajectoryCfg.rewards):
        class scales(X1TrajectoryCfg.rewards.scales):
            # Hand-written style terms are replaced by the discriminator
            # reward; keep weak root anchors, forward progress, single-support
            # shaping, and the full penalty group.
            trajectory_joint_pos = 0.0
            trajectory_joint_vel = 0.0
            trajectory_root_pos = 0.5
            trajectory_root_ori = 0.0
            trajectory_root_lin_vel = 0.5
            trajectory_root_ang_vel = 0.0
            forward_progress = 1.5
            single_support = 0.8
            # Style rewards are batch-centered inside AmpPPO, but -300 would
            # still dominate the value targets; use a moderate failure penalty.
            termination = -50.0


class X1AmpCfgPPO(X1TrajectoryCfgPPO):
    """PPO + AMP discriminator training configuration."""

    seed = 9

    class algorithm(X1TrajectoryCfgPPO.algorithm):
        # AMP discriminator hyperparameters (robolab RPO-Amp starting point).
        amp_style_weight = 1.5
        amp_disc_hidden_dims = [1024, 512]
        amp_disc_activation = "elu"
        # exp0.4: 1e-4 let the discriminator win instantly in exp0.3 (loss
        # ~1e-5, style reward saturated, zero effective gradient). Slow it
        # down so logits stay discriminative across policy samples.
        amp_disc_lr = 1.0e-5
        amp_grad_penalty = 10.0
        amp_disc_max_grad_norm = 1.0
        amp_loss_type = "lsgan"

    class runner(X1TrajectoryCfgPPO.runner):
        algorithm_class_name = "AmpPPO"
        experiment_name = "x1_amp"
        run_name = "exp0_4_amp"
