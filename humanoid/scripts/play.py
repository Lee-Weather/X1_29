# SPDX-FileCopyrightText: Copyright (c) 2021 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-FileCopyrightText: Copyright (c) 2021 ETH Zurich, Nikita Rudin
# SPDX-FileCopyrightText: Copyright (c) 2024 Beijing RobotEra TECHNOLOGY CO.,LTD. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# Copyright (c) 2024, AgiBot Inc. All rights reserved.


import os
import cv2
import numpy as np
from isaacgym import gymapi
from humanoid import LEGGED_GYM_ROOT_DIR

# import isaacgym
from humanoid.envs import *
from humanoid.utils import  get_args, export_policy_as_jit, task_registry, Logger
from isaacgym.torch_utils import *

import torch
from datetime import datetime

import pygame
from threading import Thread


x_vel_cmd, y_vel_cmd, yaw_vel_cmd = 0.0, 0.0, 0.0
joystick_use = True
joystick_opened = False

if joystick_use:
    pygame.init()
    try:
        # get joystick
        joystick = pygame.joystick.Joystick(0)
        joystick.init()
        joystick_opened = True
    except Exception as e:
        print(f"无法打开手柄：{e}")
    # joystick thread exit flag
    exit_flag = False

    def handle_joystick_input():
        global exit_flag, x_vel_cmd, y_vel_cmd, yaw_vel_cmd, head_vel_cmd
        
        
        while not exit_flag:
            # get joystick input
            pygame.event.get()
            # update robot command
            x_vel_cmd = -joystick.get_axis(1) * 1
            y_vel_cmd = -joystick.get_axis(0) * 1
            yaw_vel_cmd = -joystick.get_axis(3) * 1
            pygame.time.delay(100)

    if joystick_opened and joystick_use:
        joystick_thread = Thread(target=handle_joystick_input)
        joystick_thread.start()

def play(args):
    env_cfg, train_cfg = task_registry.get_cfgs(name=args.task)
    # override some parameters for testing
    env_cfg.env.num_envs = min(env_cfg.env.num_envs, 10)
    # env_cfg.terrain.mesh_type = 'trimesh'
    env_cfg.terrain.mesh_type = 'plane'
    env_cfg.terrain.num_rows = 5
    env_cfg.terrain.num_cols = 5
    env_cfg.terrain.max_init_terrain_level = 5
    env_cfg.env.episode_length_s = 1000
    env_cfg.noise.add_noise = False
    env_cfg.domain_rand.randomize_friction = False 
    env_cfg.domain_rand.push_robots = False 
    env_cfg.domain_rand.continuous_push = False 
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
    env_cfg.noise.curriculum = False
    env_cfg.commands.heading_command = False

    train_cfg.seed = 123145
    print("train_cfg.runner_class_name:", train_cfg.runner_class_name)

    # prepare environment
    env, _ = task_registry.make_env(name=args.task, args=args, env_cfg=env_cfg)


    env.set_camera(env_cfg.viewer.pos, env_cfg.viewer.lookat)


    # load policy
    train_cfg.runner.resume = True
    ppo_runner, train_cfg, _ = task_registry.make_alg_runner(env=env, name=args.task, args=args, train_cfg=train_cfg)
    policy = ppo_runner.get_inference_policy(device=env.device)
    
    # export policy as a jit module (used to run it from C++)
    current_date_str = datetime.now().strftime('%Y-%m-%d')
    current_time_str = datetime.now().strftime('%H-%M-%S')
    if EXPORT_POLICY:
        path = os.path.join(LEGGED_GYM_ROOT_DIR, 'logs', train_cfg.runner.experiment_name, '0_exported', 'policies')
        export_policy_as_jit(ppo_runner.alg.actor_critic, path)
        print('Exported policy as jit script to: ', path)

    logger = Logger(env_cfg.sim.dt * env_cfg.control.decimation)
    robot_index = 0 # which robot is used for logging
    joint_index = 5 # which joint is used for logging
    stop_state_log = 1000 # number of steps before plotting states
    
    # 获取正确的脚部刚体索引
    left_foot_idx = env.feet_indices[0].item()
    right_foot_idx = env.feet_indices[1].item()
    print(f"Left foot rigid body index: {left_foot_idx}")
    print(f"Right foot rigid body index: {right_foot_idx}")
    if RENDER:
        camera_properties = gymapi.CameraProperties()
        camera_properties.width = 1920
        camera_properties.height = 1080
        # camera_properties.width = 1280   # 原值: 1920
        # camera_properties.height = 720   # 原值: 1080
        h1 = env.gym.create_camera_sensor(env.envs[0], camera_properties)
        # camera_offset = gymapi.Vec3(1, -1, 0.5)
        # 修改视角把 Z 从 0.5 提高到 1.5，同时把 X,Y 距离拉大到 2.0
        camera_offset = gymapi.Vec3(2.0, -2.0, 1.5)
        camera_rotation = gymapi.Quat.from_axis_angle(gymapi.Vec3(-0.3, 0.2, 1),
                                                    np.deg2rad(135))
        actor_handle = env.gym.get_actor_handle(env.envs[0], 0)
        body_handle = env.gym.get_actor_rigid_body_handle(env.envs[0], actor_handle, 0)
        env.gym.attach_camera_to_body(
            h1, env.envs[0], body_handle,
            gymapi.Transform(camera_offset, camera_rotation),
            gymapi.FOLLOW_POSITION)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        
        # fourcc = cv2.VideoWriter_fourcc(*"avc1")




        custom_save_path = "/personal/train-more"
        run_name_str = args.run_name if args.run_name is not None else "test"
        file_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_name_str}.mp4"
        
        # 最终的完整路径
        video_filepath = os.path.join(custom_save_path, file_name)
        
        # 3. 如果文件夹不存在，自动创建
        if not os.path.exists(custom_save_path):
            os.makedirs(custom_save_path, exist_ok=True)
            
        print(f"Recording video to: {video_filepath}")
        video = cv2.VideoWriter(video_filepath, fourcc, 50.0, (1920, 1080))
        # video = cv2.VideoWriter(video_filepath, fourcc, 25.0, (1280, 720))


        # video_dir = os.path.join(LEGGED_GYM_ROOT_DIR, 'videos')
        # experiment_dir = os.path.join(LEGGED_GYM_ROOT_DIR, 'videos', train_cfg.runner.experiment_name)
        # dir = os.path.join(experiment_dir, datetime.now().strftime('%b%d_%H-%M-%S')+ args.run_name + '.mp4')
        # if not os.path.exists(video_dir):
        #     os.makedirs(video_dir,exist_ok=True)
        # if not os.path.exists(experiment_dir):
        #     os.makedirs(experiment_dir,exist_ok=True)
        # video = cv2.VideoWriter(dir, fourcc, 50.0, (1920, 1080))
    
    obs = env.get_observations()
    frame_count = 0
    np.set_printoptions(formatter={'float': '{:0.4f}'.format})

    # =========== 新增：初始化速度累加器 ===========
    vel_sum = 0.0       # 速度总和
    step_accum = 0      # 步数计数器
    # ===========================================

    for i in range(10*stop_state_log):
        
        actions = policy(obs.detach()) # * 0.
        
        if FIX_COMMAND:
            env.commands[:, 0] = 1.5   # 1.0
            env.commands[:, 1] = 0
            env.commands[:, 2] = 0
            env.commands[:, 3] = 0.
            
        else:
            env.commands[:, 0] = x_vel_cmd
            env.commands[:, 1] = y_vel_cmd
            env.commands[:, 2] = yaw_vel_cmd
            env.commands[:, 3] = 0.
        # 定义一个计数器在循环外
        
        obs, critic_obs, rews, dones, infos = env.step(actions.detach())
        # =========== 新增：每一帧都更新统计数据 ===========
        # 即使不录制这一帧，也要统计这一帧的数据，这样平均值才准确
        current_vel_x = env.base_lin_vel[0, 0].item()
        vel_sum += current_vel_x
        step_accum += 1
        # ===============================================
        if RENDER:
            frame_count += 1
            env.gym.fetch_results(env.sim, True)
            env.gym.step_graphics(env.sim)
            env.gym.render_all_camera_sensors(env.sim)

            if frame_count % 2 == 0:
                img = env.gym.get_camera_image(env.sim, env.envs[0], h1, gymapi.IMAGE_COLOR)
                # img = np.reshape(img, (720, 1280, 4))
                img = np.reshape(img, (1080, 1920, 4))
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

                # # ==================== 添加前进速度记录 ====================

                # # 1. 获取数据
                # target_vel = env.commands[0, 0].item()
                
                # # 计算平均速度 (防止除以0)
                # avg_vel = vel_sum / step_accum if step_accum > 0 else 0.0
                
                # # 2. 准备显示的文本 (稍微长一点)
                # # 格式：CMD(指令) | REAL(瞬时) | AVG(平均)
                # info_text = f"CMD: {target_vel:.2f} | REAL: {current_vel_x:.2f} | AVG: {avg_vel:.2f}"
                
                # # 3. 计算文字位置
                # # 因为文字变长了，为了不跑出画面，我们需要把起始位置往左移
                # img_h, img_w = img.shape[:2]
                # text_pos = (img_w - 950, 60)  # 从 -550 改为 -750，留出更多空间

                # # 4. 绘制文字 (黑边 + 青字)
                # cv2.putText(img, info_text, text_pos, 
                #             cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4, cv2.LINE_AA)
                # cv2.putText(img, info_text, text_pos, 
                #             cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 0), 2, cv2.LINE_AA)
                # # ==================== 前进速度结束 ====================
                # ==================== 1. 获取基础数据 ====================
                # 速度数据
                target_vel = env.commands[0, 0].item()
                current_vel_x = env.base_lin_vel[0, 0].item()
                avg_vel = vel_sum / step_accum if step_accum > 0 else 0.0
                
                # 接触力数据 (使用动态获取的脚部索引)
                left_force = env.contact_forces[0, left_foot_idx, 2].item()
                right_force = env.contact_forces[0, right_foot_idx, 2].item()
                
                # 接触判断 (阈值 1.0 N)
                l_on = left_force > 1.0
                r_on = right_force > 1.0

                # ==================== 2. 定义显示布局 ====================
                img_h, img_w = img.shape[:2]
                base_x = img_w - 1150  # 起始 X 坐标
                base_y = 60           # 起始 Y 坐标
                line_height = 50      # 行高

                # 辅助函数：快速绘制带描边的文字
                def draw_outlined_text(image, text, pos, color, scale=0.9):
                    # 黑描边
                    cv2.putText(image, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
                    # 彩色字
                    cv2.putText(image, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)

                # ==================== 3. 绘制第一行：速度信息 ====================
                speed_text = f"CMD: {target_vel:.2f} | REAL: {current_vel_x:.2f} | AVG: {avg_vel:.2f}"
                draw_outlined_text(img, speed_text, (base_x, base_y), (255, 255, 0), 1.0) # 青色

                # ==================== 4. 绘制第二、三行：单脚状态 ====================
                # 左脚
                l_color = (0, 255, 0) if l_on else (0, 0, 255) # 绿/红
                l_text = f"L-FOOT: {'ON ' if l_on else 'OFF'} ({left_force:.1f} N)"
                draw_outlined_text(img, l_text, (base_x, base_y + line_height), l_color)

                # 右脚
                r_color = (0, 255, 0) if r_on else (0, 0, 255) # 绿/红
                r_text = f"R-FOOT: {'ON ' if r_on else 'OFF'} ({right_force:.1f} N)"
                draw_outlined_text(img, r_text, (base_x, base_y + line_height * 2), r_color)

                # ==================== 5. 绘制第四行：步态全局状态 (新增) ====================
                
                state_text = "STATE: SINGLE SUPPORT" # 默认单支撑
                state_color = (200, 200, 200)        # 默认灰色

                if l_on and r_on:
                    # 双脚着地 (Double Support)
                    state_text = "STATE: *** DOUBLE SUPPORT ***"
                    state_color = (0, 255, 255) # 黄色 (BGR: Yellow)
                
                elif not l_on and not r_on:
                    # 双脚离地 (Flight Phase)
                    state_text = "STATE: >>> FLIGHT PHASE <<<"
                    state_color = (255, 0, 255) # 紫色 (BGR: Magenta)

                # 绘制状态
                draw_outlined_text(img, state_text, (base_x, base_y + line_height * 3), state_color, 1.0)

                # ==================== 结束绘制 ====================
               

                video.write(img[..., :3])
        real_cmd_x = env.commands[robot_index, 0].item()

        if i > stop_state_log*0.2 and i < stop_state_log:
            dict = {
                    'base_height' : env.root_states[robot_index, 2].item(),
                    'foot_z_l' : env.rigid_state[robot_index, left_foot_idx, 2].item(),
                    'foot_z_r' : env.rigid_state[robot_index, right_foot_idx, 2].item(),
                    'foot_forcez_l' : env.contact_forces[robot_index, left_foot_idx, 2].item(),
                    'foot_forcez_r' : env.contact_forces[robot_index, right_foot_idx, 2].item(),
                    'base_vel_x': env.base_lin_vel[robot_index, 0].item(),
                    # 'command_x': x_vel_cmd,
                    'command_x': real_cmd_x,
                    'base_vel_y':  env.base_lin_vel[robot_index, 1].item(),
                    'command_y': y_vel_cmd,
                    'base_vel_z':  env.base_lin_vel[robot_index, 2].item(),
                    'base_vel_yaw':  env.base_ang_vel[robot_index, 2].item(),
                    'command_yaw': yaw_vel_cmd,
                    'dof_pos_target': actions[robot_index, 0].item() * env.cfg.control.action_scale,
                    'dof_pos': env.dof_pos[robot_index, 0].item(),
                    'dof_vel': env.dof_vel[robot_index, 0].item(),
                    'dof_torque': env.torques[robot_index, 0].item(),
                    'command_sin': obs[0,0].item(),
                    'command_cos': obs[0,1].item(),
                }

            # add dof_pos_target
            for i in range(env_cfg.env.num_actions):
                dict[f'dof_pos_target[{i}]'] = actions[robot_index, i].item() * env.cfg.control.action_scale,

            # add dof_pos
            for i in range(env_cfg.env.num_actions):
                dict[f'dof_pos[{i}]'] = env.dof_pos[robot_index, i].item(),

            # add dof_torque
            for i in range(env_cfg.env.num_actions):
                dict[f'dof_torque[{i}]'] = env.torques[robot_index, i].item(),

            # add dof_vel
            for i in range(env_cfg.env.num_actions):
                dict[f'dof_vel[{i}]'] = env.dof_vel[robot_index, i].item(),

            logger.log_states(dict=dict)
        
        elif _== stop_state_log:
            logger.plot_states()
        elif i == stop_state_log:
            logger.plot_states()

        # ====================== Log states ======================
        if infos["episode"]:
            num_episodes = torch.sum(env.reset_buf).item()
            if num_episodes>0:
                logger.log_rewards(infos["episode"], num_episodes)

    if RENDER:
        video.release()

if __name__ == '__main__':
    EXPORT_POLICY = False
    RENDER = True
    FIX_COMMAND = True
    args = get_args()
    play(args)
