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
        # exp0.6: the slowed walk (3 x 2.18 s cycles + 2.4 s transitions)
        # needs a longer horizon: 654 + 240 = 894 steps plus margin.
        episode_length_s = 9.2

    class control(X1TrajectoryCfg.control):
        # exp0.8: the stand-task PD gains cannot track the walk reference.
        # exp0.6t zero-action replay: pure PD falls every 1.70 s at BOTH
        # full and half speed with hip_pitch tracking error 0.295 rad
        # (max 0.89) and hip_yaw max 1.12 rad - the failure is structural
        # gain deficiency, not speed. Roughly double stiffness for the
        # walking legs (knee already tracks at 0.08 rad, keep it).
        stiffness = {
            "hip_pitch_joint": 80,
            "hip_roll_joint": 80,
            "hip_yaw_joint": 70,
            "knee_pitch_joint": 120,
            "ankle_pitch_joint": 70,
            "ankle_roll_joint": 70,
        }
        damping = {
            "hip_pitch_joint": 4.0,
            "hip_roll_joint": 4.0,
            "hip_yaw_joint": 4.0,
            "knee_pitch_joint": 10.0,
            "ankle_pitch_joint": 2.0,
            "ankle_roll_joint": 2.0,
        }

    class trajectory(X1TrajectoryCfg.trajectory):
        # exp0.6 (plan A): train at half speed first. The 0.6 m/s gait left
        # every policy so far in a fall-at-the-WALK-entry deadlock; 0.3 m/s
        # has far larger static stability margin and is the reachable rung.
        # Frame indices below are in the 2x upsampled timeline (861 frames).
        reference_time_scale = 0.5
        steady_cycle_start_frame = 64
        steady_cycle_frames = 218
        gait_period_s = 2.18
        # exp0.7: halve residual authority. exp0.6 replay showed max|action|
        # pinned at 1.0 for the whole WALK stage while vx ran away backward
        # to -2.6 m/s: the policy fights the PD feedforward with saturated
        # residuals and topples itself. Half scales keep even a saturated
        # policy inside the near-statically-stable corridor of the slow gait.
        residual_action_scales = {
            "left_hip_pitch_joint": 0.10,
            "left_hip_roll_joint": 0.06,
            "left_hip_yaw_joint": 0.06,
            "left_knee_pitch_joint": 0.10,
            "left_ankle_pitch_joint": 0.04,
            "left_ankle_roll_joint": 0.04,
            "right_hip_pitch_joint": 0.10,
            "right_hip_roll_joint": 0.06,
            "right_hip_yaw_joint": 0.06,
            "right_knee_pitch_joint": 0.10,
            "right_ankle_pitch_joint": 0.04,
            "right_ankle_roll_joint": 0.04,
        }

    class rewards(X1TrajectoryCfg.rewards):
        class scales(X1TrajectoryCfg.rewards.scales):
            # The discriminator only scores window-level style; exp0.4 showed
            # that without per-joint accuracy the residuals run away (WALK
            # joint error 0.47 rad, vx lunge to 2.83 m/s before the fall).
            # Re-anchor joint accuracy at a mid weight: high enough to bound
            # the residuals, low enough to keep the stepping drive alive.
            trajectory_joint_pos = 1.5
            trajectory_joint_vel = 0.0
            trajectory_root_pos = 0.5
            trajectory_root_ori = 0.0
            trajectory_root_lin_vel = 0.5
            trajectory_root_ang_vel = 0.0
            forward_progress = 1.5
            # exp0.5: penalize forward speed beyond 1.5x reference; the
            # symmetric exp kernel of forward_progress cannot stop lunges.
            forward_overspeed = -1.0
            single_support = 0.8
            # exp0.7: raise the action-magnitude component of the inherited
            # smoothness term (diff + diff2 + 0.05*sum|a|) to directly
            # discourage the saturated bang-bang residuals seen in exp0.6.
            action_smoothness = -0.01
            # Style rewards are batch-centered inside AmpPPO, but -300 would
            # still dominate the value targets; use a moderate failure penalty.
            termination = -50.0


class X1AmpCfgPPO(X1TrajectoryCfgPPO):
    """PPO + AMP discriminator training configuration."""

    seed = 14

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
        run_name = "exp0_8_amp"
