"""Joint-limit-aware preflight and redundant-IK preference for lead motions.

The rigid serial-chain model is reconstructed from the live articulation's
geometric Jacobian and pose. It predicts kinematic reachability only; live
hardware/contact and actual arrival gates remain in the executor.
"""
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

CONTROLLER_POLICY={'nullspace_preference_gain':.2,'joint_command_speed_fraction':.75,
    'kinematic_position_tolerance_m':.001,'kinematic_orientation_tolerance_deg':1.,
    'basis':'project_engineering_settings_not_calibrated_hardware_limits'}


def posture_preference(step_id):
    electrode_pick=(step_id.startswith('ui_pick_') and 'electrode' in step_id)
    return {'task':step_id,'selection':'feasible_pose_then_short_joint_motion_and_joint_margin',
            'elbow_extension_preference_weight':.03 if electrode_pick else 0.,
            'fixed_elbow_height':False,'fixed_elbow_direction':False}


def rotation(wxyz):
    q=np.asarray(wxyz,dtype=float)
    return Rotation.from_quat(q[[1,2,3,0]]).as_matrix()


class SerialKinematics:
    def __init__(self,joints,position,quaternion,jacobian):
        self.reference=np.asarray(joints,dtype=float)
        self.home=np.eye(4);self.home[:3,:3]=rotation(quaternion)
        self.home[:3,3]=position
        j=np.asarray(jacobian,dtype=float)
        self.axes=j[3:].T.copy()
        if j.shape!=(6,len(self.reference)) or not np.isfinite(j).all():
            raise ValueError('invalid live articulation Jacobian')
        if not np.allclose(np.linalg.norm(self.axes,axis=1),1.,atol=1e-3):
            raise ValueError('preflight requires revolute serial joints')
        self.linear=j[:3].T-np.cross(self.axes,np.asarray(position))

    def transform(self,joints,count=None):
        result=np.eye(4)
        count=len(self.reference) if count is None else count
        for axis,linear,angle in zip(self.axes[:count],self.linear[:count],
                                     (np.asarray(joints)-self.reference)[:count]):
            r=Rotation.from_rotvec(axis*angle).as_matrix()
            delta=np.eye(4);delta[:3,:3]=r
            delta[:3,3]=(np.eye(3)-r)@np.cross(axis,linear)+axis*np.dot(axis,linear)*angle
            result=result@delta
        return result

    def forward(self,joints):
        return self.transform(joints)@self.home

    def plan(self,positions,quaternion,limits,posture=None):
        limits=np.asarray(limits,dtype=float);low,high=limits.T
        if limits.shape!=(len(self.reference),2) or np.any(low>=high):
            raise ValueError('invalid live joint limits')
        center=(low+high)/2;span=high-low;desired_r=rotation(quaternion)
        previous=self.reference.copy();solutions=[];maximum_error=0.;minimum_margin=float('inf')
        minimum_elbow=float('inf')
        def elbow_point(q):
            t=self.transform(q,posture['upstream_joints'])
            return t[:3,:3]@np.asarray(posture['point_base'])+t[:3,3]
        for index,position in enumerate(positions):
            position=np.asarray(position,dtype=float)
            def residual(q):
                pose=self.forward(q)
                values=np.r_[(pose[:3,3]-position)/.001,
                    Rotation.from_matrix(desired_r@pose[:3,:3].T).as_rotvec()/.01,
                    .04*(q-center)/span]
                return values
            candidates=[]
            seeds=[previous,.8*previous+.2*center,center]
            # Additional bounded shoulder/elbow branches are computation-only
            # warm starts, used only when the nearby seeds have no solution.
            for sign in (-1.,1.):
                bend=center.copy()
                bend[0]+=sign*.2*span[0]
                if len(bend)>2:bend[2]-=sign*.2*span[2]
                seeds.append(bend)
            for seed_index,seed in enumerate(seeds):
                if seed_index>=3 and candidates:
                    break
                result=least_squares(residual,np.clip(seed,low+1e-7,high-1e-7),
                    bounds=(low+1e-7,high-1e-7),max_nfev=180,
                    ftol=1e-9,xtol=1e-9,gtol=1e-9)
                pose=self.forward(result.x)
                error=float(np.linalg.norm(pose[:3,3]-position))
                angle=float(np.linalg.norm(Rotation.from_matrix(desired_r@pose[:3,:3].T).as_rotvec()))
                margin=np.minimum(result.x-low,high-result.x)
                height=(float(np.dot(posture['world_up_base'],elbow_point(result.x))
                              +posture['base_height_world']) if posture else float('inf'))
                if error<=.001 and angle<=np.deg2rad(1.):
                    score=float(np.sum(((result.x-previous)/span)**2)
                        +.002*np.sum(1./(margin/span+.025)**2))
                    if posture:
                        # Outward elbow extension is a task-specific soft
                        # preference. It never rejects a reachable pose or
                        # fixes the elbow to a height or direction.
                        shoulder=np.asarray(posture['shoulder_point_base'])
                        radial=np.linalg.norm((elbow_point(result.x)-shoulder)[:2])
                        score-=posture['preference']['elbow_extension_preference_weight']*radial
                    candidates.append((score,result.x,error,float(min(margin)),height))
            if not candidates:
                raise ValueError(f'登记关节限位内未找到路径点{index}的可达姿态，目标{position.tolist()}')
            _,previous,error,margin,height=min(candidates,key=lambda row:row[0])
            solutions.append(previous.tolist());maximum_error=max(maximum_error,error)
            minimum_margin=min(minimum_margin,margin)
            minimum_elbow=min(minimum_elbow,height)
        return {'joint_targets_rad':solutions,'maximum_kinematic_error_m':maximum_error,
                'minimum_joint_limit_margin_rad':minimum_margin,
                'elbow_posture':({**posture,'minimum_planned_height_world':minimum_elbow} if posture else None),
                'basis':'live_articulation_jacobian_serial_chain_bounded_IK'}


def plan_live_route(term,positions_world,orientation_world,step_id=''):
    p,q=term._compute_frame_pose()
    jac=term.kinematic_frame_jacobian().detach().cpu().numpy()[0].copy()
    data=term._asset.data;ids=term._joint_ids
    base_r=rotation(data.root_quat_w[0].detach().cpu().tolist())
    base_p=data.root_pos_w[0].detach().cpu().numpy()
    positions=[base_r.T@(np.asarray(v)-base_p) for v in positions_world]
    target_r=base_r.T@rotation(orientation_world)
    xyzw=Rotation.from_matrix(target_r).as_quat()
    joints=data.joint_pos[0,ids].detach().cpu().numpy()
    model=SerialKinematics(joints,p[0].detach().cpu().numpy(),q[0].detach().cpu().numpy(),jac)
    limits=data.joint_pos_limits[0,ids].detach().cpu().numpy()
    elbow_index=term._asset.body_names.index('panda_link4')
    elbow_world=data.body_pos_w[0,elbow_index].detach().cpu().numpy()
    if getattr(term,'elbow_reference_height_world',None) is None:
        term.elbow_reference_height_world=float(elbow_world[2])
    term.elbow_body_index=elbow_index
    term.elbow_shoulder_body_index=term._asset.body_names.index('panda_link2')
    term.elbow_wrist_body_index=term._asset.body_names.index('panda_hand')
    shoulder_world=data.body_pos_w[0,term.elbow_shoulder_body_index].detach().cpu().numpy()
    posture={'body':'panda_link4','upstream_joints':4,
             'point_base':(base_r.T@(elbow_world-base_p)).tolist(),
             'world_up_base':(base_r.T@np.array([0.,0.,1.])).tolist(),
             'base_height_world':float(base_p[2]),
             'shoulder_point_base':(base_r.T@(shoulder_world-base_p)).tolist(),
             'preference':posture_preference(step_id),'rule':'task_specific_soft_preference'}
    return {**model.plan(positions,xyzw[[3,0,1,2]],limits,posture),'controller_policy':CONTROLLER_POLICY}


def action_type():
    import torch
    from isaaclab.envs.mdp.actions.task_space_actions import DifferentialInverseKinematicsAction
    from isaaclab.utils import math as math_utils

    class JointAwareAction(DifferentialInverseKinematicsAction):
        preferred_joint_positions=None

        def kinematic_frame_jacobian(self):
            # PhysX supplies the body-origin Jacobian in world axes. Rotate
            # both blocks to base axes, then rotate the tool offset from the
            # hand frame to the same base axes before translating the origin.
            # Angular velocity expressed in base axes is unchanged by a fixed
            # orientation offset on the hand.
            j=self.jacobian_w.clone()
            data=self._asset.data
            inverse=math_utils.matrix_from_quat(math_utils.quat_inv(data.root_quat_w))
            j[:,:3,:]=inverse@j[:,:3,:];j[:,3:,:]=inverse@j[:,3:,:]
            if self.cfg.body_offset is not None:
                body_rotation=math_utils.matrix_from_quat(data.body_quat_w[:,self._body_idx])
                offset=(inverse@body_rotation@self._offset_pos.unsqueeze(-1)).squeeze(-1)
                j[:,:3,:]-=math_utils.skew_symmetric_matrix(offset)@j[:,3:,:]
            return j

        def _compute_frame_jacobian(self):
            if self.preferred_joint_positions is None:
                return super()._compute_frame_jacobian()
            return self.kinematic_frame_jacobian()

        def apply_actions(self):
            if self.preferred_joint_positions is None:
                return super().apply_actions()
            p,q=self._compute_frame_pose();j=self._compute_frame_jacobian()
            current=self._asset.data.joint_pos[:,self._joint_ids]
            task=self._ik_controller.compute(p,q,j,current)-current
            inverse=torch.linalg.pinv(j,rtol=1e-4)
            projector=torch.eye(current.shape[-1],device=self.device)[None]-inverse@j
            preference=torch.as_tensor(self.preferred_joint_positions,device=self.device,dtype=current.dtype)[None]
            null=(projector@(CONTROLLER_POLICY['nullspace_preference_gain']*(preference-current)).unsqueeze(-1)).squeeze(-1)
            delta=task+null
            limits=self._asset.data.joint_pos_limits[:,self._joint_ids]
            speeds=self._asset.data.joint_vel_limits[:,self._joint_ids]
            step=speeds*float(self._env.physics_dt)*CONTROLLER_POLICY['joint_command_speed_fraction']
            scale=torch.clamp((step/(delta.abs()+1e-9)).amin(-1,keepdim=True),max=1.)
            target=current+delta*scale
            target=torch.clamp(target,min=limits[:,:,0]+1e-6,max=limits[:,:,1]-1e-6)
            self._asset.set_joint_position_target(target,self._joint_ids)

    return JointAwareAction
