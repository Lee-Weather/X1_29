"""Numerical helpers for X1 trajectory preprocessing.

This module intentionally depends only on NumPy so reference transforms can be
validated without importing Isaac Gym.
"""

import numpy as np


def canonicalize_root_trajectory(
    root_pos,
    root_quat_xyzw,
    root_lin_vel,
    root_ang_vel,
    anchor_frame,
):
    """Express world-frame root states in the anchor frame's planar heading.

    The anchor's x/y position and yaw become zero. Height, roll, and pitch are
    retained. Linear and angular velocities are assumed to be world-frame
    vectors and are rotated by the same heading transform.
    """
    root_pos = np.asarray(root_pos)
    root_quat_xyzw = np.asarray(root_quat_xyzw)
    root_lin_vel = np.asarray(root_lin_vel)
    root_ang_vel = np.asarray(root_ang_vel)
    frame_count = len(root_pos)
    if not 0 <= anchor_frame < frame_count:
        raise ValueError(f"Anchor frame {anchor_frame} is outside [0, {frame_count - 1}]")
    if root_pos.shape != (frame_count, 3):
        raise ValueError("root_pos must have shape (frames, 3)")
    if root_quat_xyzw.shape != (frame_count, 4):
        raise ValueError("root_quat_xyzw must have shape (frames, 4)")
    if root_lin_vel.shape != (frame_count, 3) or root_ang_vel.shape != (frame_count, 3):
        raise ValueError("root velocities must have shape (frames, 3)")

    anchor_quat = root_quat_xyzw[anchor_frame]
    x, y, z, w = anchor_quat
    anchor_yaw = np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    cos_yaw = np.cos(anchor_yaw)
    sin_yaw = np.sin(anchor_yaw)

    def rotate_world_vectors(vectors):
        rotated = vectors.copy()
        world_x = vectors[:, 0]
        world_y = vectors[:, 1]
        rotated[:, 0] = cos_yaw * world_x + sin_yaw * world_y
        rotated[:, 1] = -sin_yaw * world_x + cos_yaw * world_y
        return rotated

    canonical_pos = root_pos.copy()
    canonical_pos[:, :2] -= root_pos[anchor_frame, :2]
    canonical_pos = rotate_world_vectors(canonical_pos)

    canonical_lin_vel = rotate_world_vectors(root_lin_vel)
    canonical_ang_vel = rotate_world_vectors(root_ang_vel)

    # Left-multiply every xyzw quaternion by the inverse anchor-yaw quaternion.
    half_yaw = 0.5 * anchor_yaw
    cos_half = np.cos(half_yaw)
    sin_half = np.sin(half_yaw)
    quat_x = root_quat_xyzw[:, 0]
    quat_y = root_quat_xyzw[:, 1]
    quat_z = root_quat_xyzw[:, 2]
    quat_w = root_quat_xyzw[:, 3]
    canonical_quat = np.empty_like(root_quat_xyzw)
    canonical_quat[:, 0] = cos_half * quat_x + sin_half * quat_y
    canonical_quat[:, 1] = cos_half * quat_y - sin_half * quat_x
    canonical_quat[:, 2] = cos_half * quat_z - sin_half * quat_w
    canonical_quat[:, 3] = cos_half * quat_w + sin_half * quat_z
    canonical_quat /= np.linalg.norm(canonical_quat, axis=1, keepdims=True)

    return (
        canonical_pos,
        canonical_quat,
        canonical_lin_vel,
        canonical_ang_vel,
        float(anchor_yaw),
    )
