"""v4: same as v3 but with the canonical rotation direction fixed.

The source walks toward +y with yaw ~98 deg. After removing yaw0, forward
must map to +x in the canonical frame: v_canon = R(-yaw0) v_world, i.e.
x' = cos x + sin y, y' = -sin x + cos y applied to WORLD vectors. v3 wrongly
reused this matrix on the already-canonical root_pos derivative in a way
that flipped the sense; here velocities are computed from the canonicalized
root positions directly (no double rotation), and joint/quat velocities from
their canonical splines.
"""
import csv
import numpy as np
from scipy.interpolate import CubicSpline

SRC = r"czy\diff\yz\07_03_walk_yup_recwalk_base_lowerbody_smooth_p8_120_180_groundfit_minima_safe.csv"
DST = r"resources\motions\x1\motion_walk_yz_0.26ms_x1_12d_100hz.npz"

LEG_JOINTS = ("left_hip_pitch_joint","left_hip_roll_joint","left_hip_yaw_joint","left_knee_pitch_joint","left_ankle_pitch_joint","left_ankle_roll_joint","right_hip_pitch_joint","right_hip_roll_joint","right_hip_yaw_joint","right_knee_pitch_joint","right_ankle_pitch_joint","right_ankle_roll_joint")
SRC_RATE=30.0; DST_RATE=100.0; N_CYCLES=2; SEAM=50

def yaw_of(q):
    x,y,z,w=q; return np.arctan2(2*(w*z+x*y),1-2*(y*y+z*z))

def hermite(n):
    t=np.linspace(0,1,n,endpoint=False); return (3*t**2-2*t**3)[:,None]

rows=list(csv.DictReader(open(SRC,encoding='utf-8')))
t=np.array([float(r['timestamp']) for r in rows]); tu=np.arange(len(t))/SRC_RATE
qpos=np.array([[float(r[j]) for j in LEG_JOINTS] for r in rows])
rp=np.array([[float(r['root_pos_%s'%a]) for a in 'xyz'] for r in rows])
rq=np.array([[float(r['root_quat_%s'%a]) for a in ('x','y','z','w')] for r in rows])

n_new=int(round((len(t)-1)*DST_RATE/SRC_RATE))+1
t_new=np.arange(n_new)/DST_RATE
sp=lambda a: CubicSpline(tu,a,axis=0)(t_new)
q=sp(qpos); rp=sp(rp); rqn=sp(rq); rqn/=np.linalg.norm(rqn,axis=1,keepdims=True)

yaw0=yaw_of(rqn[0])
# world -> canonical (remove yaw0): x' = c*x + s*y ; y' = -s*x + c*y
c,s=np.cos(yaw0),np.sin(yaw0)
def w2c(v):
    x,y = v[:,0],v[:,1]
    return np.stack([c*x+s*y, -s*x+c*y, v[:,2]],axis=1)

rpc=rp.copy(); rpc[:,:2]-=rpc[0,:2]; rpc=w2c(rpc)
h=.5*yaw0; ch,sh=np.cos(h),np.sin(h)
xq,yq,zq,wq=rqn.T
qc=np.stack([ch*xq+sh*yq, ch*yq-sh*xq, ch*zq-sh*wq, ch*wq+sh*zq],axis=1)
qc/=np.linalg.norm(qc,axis=1,keepdims=True)

cand=int(round(2.4*DST_RATE)); best=None
for off in range(-25,26):
    i=cand+off
    if 0<i<n_new:
        cost=np.abs(q[i]-q[0]).sum()
        if best is None or cost<best[1]: best=(i,cost)
cf=best[0]; print('cycle boundary frame %d (%.2fs) cost %.4f'%(cf,cf/DST_RATE,best[1]))

cyc=dict(qpos=q[:cf],root_pos=rpc[:cf],root_quat=qc[:cf])
blend=hermite(SEAM)
out={}
for k in cyc:
    blocks=[cyc[k]]
    for _ in range(1,N_CYCLES):
        blk=cyc[k].copy()
        if k=='root_pos':
            pl=blocks[-1][-1]
            blk[:,:2]+=pl[:2]-cyc[k][0,:2]; blk[:,2]+=pl[2]-cyc[k][0,2]
        else:
            pl=blocks[-1][-1]; hd=cyc[k][0]
            blk[:SEAM]=(1-blend)*(pl+(blk[:SEAM]-hd))+blend*blk[:SEAM]
            if k=='root_quat': blk[:SEAM]/=np.linalg.norm(blk[:SEAM],axis=1,keepdims=True)
        blocks.append(blk)
    out[k]=np.concatenate(blocks,axis=0)

n=len(out['qpos']); tt=np.arange(n)/DST_RATE
# velocities: derivatives of the FINAL canonical signals (no double rotation)
qvel=CubicSpline(tt,out['qpos'],axis=0)(tt,1)
rlv=CubicSpline(tt,out['root_pos'],axis=0)(tt,1)
dq=CubicSpline(tt,out['root_quat'],axis=0)(tt,1)
rav=(2*(dq*out['root_quat']*np.array([1,1,1,-1])))[:,:3]

np.savez(DST, time=tt.astype(np.float32), rate_hz=np.float32(DST_RATE),
    joint_names=np.array(LEG_JOINTS),
    qpos=out['qpos'].astype(np.float32), qvel=qvel.astype(np.float32),
    root_pos=out['root_pos'].astype(np.float32), root_quat_xyzw=out['root_quat'].astype(np.float32),
    root_lin_vel=rlv.astype(np.float32), root_ang_vel=rav.astype(np.float32))
print('saved v4:', n, 'frames')

# audit
print('vx mean %.3f (expect ~0.25) | net x disp %.2f m' % (rlv[:,0].mean(), out['root_pos'][-1,0]))
fd=np.gradient(out['qpos'],axis=0)*DST_RATE
print('qvel-deriv err: max %.4f mean %.4f'%(np.abs(fd-qvel).max(), np.abs(fd-qvel).mean()))
print('quat dot min %.5f' % np.abs(np.sum(out['root_quat'][1:]*out['root_quat'][:-1],axis=1)).min())
print('ang vel norm mean %.3f max %.3f' % (np.linalg.norm(rav,axis=1).mean(), np.linalg.norm(rav,axis=1).max()))
