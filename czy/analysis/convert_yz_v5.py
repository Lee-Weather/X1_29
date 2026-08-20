"""v5: regenerate the yz AMP demo NPZ from the FULL source CSV.

Changes vs v4 (which tiled one cycle with Hermite seams):
- keep ALL 1381 resampled frames (13.8 s, 5.5 gait cycles) - no tiling, no
  synthetic seam data;
- canonicalize at frame 0 as before (frame 0 values are what spawn/default
  pose consume, so training behavior stays identical);
- velocities from spline derivatives of the full signals;
- trim the last partial cycle? No: keep it - the discriminator sampler
  (updated to use the full window) clamps history taps, and a partial cycle
  adds valid data.
"""
import csv
import os
import numpy as np
from scipy.interpolate import CubicSpline

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, r"czy\diff\yz\07_03_walk_yup_recwalk_base_lowerbody_smooth_p8_120_180_groundfit_minima_safe.csv")
DST = os.path.join(ROOT, r"resources\motions\x1\motion_walk_yz_0.26ms_x1_12d_100hz.npz")

LEG_JOINTS = ("left_hip_pitch_joint","left_hip_roll_joint","left_hip_yaw_joint","left_knee_pitch_joint","left_ankle_pitch_joint","left_ankle_roll_joint","right_hip_pitch_joint","right_hip_roll_joint","right_hip_yaw_joint","right_knee_pitch_joint","right_ankle_pitch_joint","right_ankle_roll_joint")
SRC_RATE = 30.0
DST_RATE = 100.0

def yaw_of(q):
    x, y, z, w = q
    return np.arctan2(2*(w*z+x*y), 1-2*(y*y+z*z))

rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
t = np.array([float(r["timestamp"]) for r in rows])
tu = np.arange(len(t)) / SRC_RATE
qpos = np.array([[float(r[j]) for j in LEG_JOINTS] for r in rows])
rp = np.array([[float(r["root_pos_%s" % a]) for a in "xyz"] for r in rows])
rq = np.array([[float(r["root_quat_%s" % a]) for a in ("x","y","z","w")] for r in rows])

n_new = int(round((len(t)-1) * DST_RATE / SRC_RATE)) + 1
t_new = np.arange(n_new) / DST_RATE
print(f"resample {len(t)} @30Hz -> {n_new} @100Hz ({(n_new-1)/DST_RATE:.2f}s, no tiling)")

sp = lambda a: CubicSpline(tu, a, axis=0)(t_new)
q = sp(qpos); rpn = sp(rp); rqn = sp(rq)
rqn /= np.linalg.norm(rqn, axis=1, keepdims=True)

yaw0 = yaw_of(rqn[0]); c, s = np.cos(yaw0), np.sin(yaw0)
w2c = lambda v: np.stack([c*v[:,0]+s*v[:,1], -s*v[:,0]+c*v[:,1], v[:,2]], axis=1)
rpc = rpn.copy()
rpc[:, :2] -= rpc[0, :2]
rpc = w2c(rpc)
h = .5*yaw0; ch, sh = np.cos(h), np.sin(h)
xq, yq, zq, wq = rqn.T
qc = np.stack([ch*xq+sh*yq, ch*yq-sh*xq, ch*zq-sh*wq, ch*wq+sh*zq], axis=1)
qc /= np.linalg.norm(qc, axis=1, keepdims=True)

qvel = CubicSpline(t_new, q, axis=0)(t_new, 1)
rlv = CubicSpline(t_new, rpc, axis=0)(t_new, 1)
dq = CubicSpline(t_new, qc, axis=0)(t_new, 1)
rav = (2*(dq*qc*np.array([1,1,1,-1])))[:, :3]

np.savez(DST, time=t_new.astype(np.float32), rate_hz=np.float32(DST_RATE),
    joint_names=np.array(LEG_JOINTS),
    qpos=q.astype(np.float32), qvel=qvel.astype(np.float32),
    root_pos=rpc.astype(np.float32), root_quat_xyzw=qc.astype(np.float32),
    root_lin_vel=rlv.astype(np.float32), root_ang_vel=rav.astype(np.float32))

# audit
print('saved v5:', n_new, 'frames (%.2f s = ~%.1f cycles)' % ((n_new-1)/DST_RATE, (n_new-1)/DST_RATE/2.33))
print('vx mean %.3f | net x %.2f m' % (rlv[:,0].mean(), rpc[-1,0]))
print('root z %.3f-%.3f' % (rpc[:,2].min(), rpc[:,2].max()))
fd = np.gradient(q, axis=0)*DST_RATE
print('qvel-deriv err max %.4f mean %.4f' % (np.abs(fd-qvel).max(), np.abs(fd-qvel).mean()))
print('quat dot min %.5f' % np.abs(np.sum(qc[1:]*qc[:-1],axis=1)).min())
print('frame0 qpos == src frame0:', np.allclose(q[0], qpos[0], atol=1e-6))
