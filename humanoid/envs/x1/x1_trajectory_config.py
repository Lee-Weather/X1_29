"""Configuration for staged X1 standing, walking, and stopping tracking."""

from humanoid import LEGGED_GYM_ROOT_DIR
from humanoid.envs.x1.x1_dh_stand_config import X1DHStandCfg, X1DHStandCfgPPO


class X1TrajectoryCfg(X1DHStandCfg):
    """Independent configuration for X1 trajectory tracking."""

    class env(X1DHStandCfg.env):
        # The complete sequence is stand -> start -> walk -> stop -> stand.
        episode_length_s = 10.0
        use_ref_actions = False
        num_single_obs = 79
        num_observations = int(X1DHStandCfg.env.frame_stack * num_single_obs)
        single_num_privileged_obs = 105
        num_privileged_obs = int(X1DHStandCfg.env.c_frame_stack * single_num_privileged_obs)

    class terrain(X1DHStandCfg.terrain):
        mesh_type = "plane"
        curriculum = False
        static_friction = 0.8
        dynamic_friction = 0.8

    class domain_rand(X1DHStandCfg.domain_rand):
        push_robots = False
        randomize_friction = False
        randomize_base_mass = False
        randomize_com = False
        randomize_gains = False
        randomize_torque = False
        randomize_link_mass = False
        randomize_motor_offset = False
        randomize_joint_friction = False
        randomize_joint_damping = False
        randomize_joint_armature = False
        add_lag = False
        add_dof_lag = False
        add_dof_pos_vel_lag = False
        add_imu_lag = False

    class normalization(X1DHStandCfg.normalization):
        clip_actions = 1.0

    class noise(X1DHStandCfg.noise):
        # Reference tracking starts from clean observations; noise is added later by curriculum.
        add_noise = False

    class trajectory:
        file = (
            f"{LEGGED_GYM_ROOT_DIR}/resources/motions/x1/"
            "motion_walk_0.6ms_v1_x1_12d_100hz.npz"
        )
        expected_rate_hz = 100.0
        gait_period_s = 1.08
        # A near-periodic 1.09 s unit selected from the retargeted steady walk.
        # The state at frame 141 is matched to frame 32 before repeating.
        steady_cycle_start_frame = 32
        steady_cycle_frames = 109
        steady_walk_cycles = 6
        initial_stand_s = 0.40
        start_transition_s = 0.80
        stop_transition_s = 0.80
        final_stand_s = 0.40
        joint_names = (
            "left_hip_pitch_joint",
            "left_hip_roll_joint",
            "left_hip_yaw_joint",
            "left_knee_pitch_joint",
            "left_ankle_pitch_joint",
            "left_ankle_roll_joint",
            "right_hip_pitch_joint",
            "right_hip_roll_joint",
            "right_hip_yaw_joint",
            "right_knee_pitch_joint",
            "right_ankle_pitch_joint",
            "right_ankle_roll_joint",
        )

    class rewards(X1DHStandCfg.rewards):
        class scales(X1DHStandCfg.rewards.scales):
            # Disable the analytic-gait objectives inherited from x1_dh_stand.
            ref_joint_pos = 0.0
            feet_clearance = 0.0
            feet_contact_number = 0.0
            feet_air_time = 0.0
            foot_slip = 0.0
            feet_distance = 0.0
            knee_distance = 0.0
            feet_contact_forces = 0.0
            tracking_lin_vel = 0.0
            tracking_ang_vel = 0.0
            vel_mismatch_exp = 0.0
            low_speed = 0.0
            track_vel_hard = 0.0
            default_joint_pos = 0.0
            orientation = 0.0
            feet_rotation = 0.0
            base_height = 0.0
            base_acc = 0.0
            stand_still = 0.0

            trajectory_joint_pos = 3.0
            trajectory_joint_vel = 0.5
            trajectory_root_pos = 1.0
            trajectory_root_ori = 1.0
            trajectory_root_lin_vel = 0.5
            trajectory_root_ang_vel = 0.25

            action_smoothness = -0.002
            torques = -8e-9
            dof_vel = -2e-8
            dof_acc = -1e-7
            collision = -1.0
            dof_vel_limits = -1.0
            dof_pos_limits = -10.0
            dof_torque_limits = -0.1


class X1TrajectoryCfgPPO(X1DHStandCfgPPO):
    """PPO configuration kept separate from the baseline task."""

    class runner(X1DHStandCfgPPO.runner):
        experiment_name = "x1_trajectory"
