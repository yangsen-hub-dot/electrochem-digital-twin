"""Deterministic motion rules shared by planning and live executor guards.

Project engineering limits are versioned in physical_constraints.json; joint
limits come from the actual articulation. Passing these checks never claims
complete contact dynamics, real hardware safety, or calibrated precision.
"""
from pathlib import Path
import hashlib
import json
import math
import numpy as np
from pregrasp_planner import orientation_error_deg


class PhysicalConstraintError(RuntimeError):
    def __init__(self, code, message, **evidence):
        self.code, self.evidence = code, evidence
        super().__init__(message)


def load_policy():
    path=Path(__file__).with_name('physical_constraints.json')
    raw=path.read_bytes(); policy=json.loads(raw)
    policy['sha256']=hashlib.sha256(raw).hexdigest()
    return policy


def vector(value, length=3):
    raw=np.asarray(value)
    if (raw.shape!=(length,) or raw.dtype.kind not in 'fiu'
            or not np.isfinite(raw).all()
            or any(isinstance(x,(bool,np.bool_)) for x in value)):
        raise PhysicalConstraintError('invalid_motion_evidence','运动证据不是有限数值向量')
    return raw.astype(float)


def prepare_waypoints(points, tolerance=1e-6):
    result=[]
    for value in points:
        point=vector(value)
        if not result or np.linalg.norm(point-result[-1])>tolerance:
            result.append(point)
    if not result:
        raise PhysicalConstraintError('empty_motion','运动路径为空')
    # Repeated geometry may indicate an A-B-A-B trial loop. Deliberate
    # pick/release/return actions are separate motion calls, not this pattern.
    for index in range(3,len(result)):
        if (np.linalg.norm(result[index]-result[index-2])<=tolerance
                and np.linalg.norm(result[index-1]-result[index-3])<=tolerance):
            raise PhysicalConstraintError('repeated_motion_loop','路径包含反复往返的试探段')
    return [tuple(map(float,p)) for p in result]


class CorrectionBudget:
    def __init__(self, policy, target_tolerance):
        self.maximum=policy['motion']['maximum_local_corrections']
        self.tolerance=float(target_tolerance)
        self.previous=None;self.observations=[]

    def observe(self,error):
        norm=float(np.max(np.abs(vector(error))))
        self.observations.append(norm)
        if norm<=self.tolerance: return True
        if self.previous is not None and norm>=self.previous-self.tolerance:
            raise PhysicalConstraintError('correction_not_converging',
                                          '局部位置修正未继续收敛，停止反复试探',errors_m=self.observations)
        self.previous=norm
        if len(self.observations)>=self.maximum:
            raise PhysicalConstraintError('correction_budget','局部位置修正达到次数上限，停止并重新规划',
                                          errors_m=self.observations)
        return False


class HeldOrientation:
    def __init__(self,policy):
        self.policy=policy;self.key=None;self.orientation=None

    def choose(self,key,actual,requested):
        if key is None:
            self.key,self.orientation=None,None
            return requested
        if key!=self.key:
            self.key,self.orientation=key,list(actual)
        if requested is None: return list(self.orientation)
        if orientation_error_deg(requested,self.orientation)>self.policy['motion']['orientation_deadband_deg']:
            raise PhysicalConstraintError('unplanned_held_rotation',
                '持物姿态不允许隐式切换；需先提供支持刚性附件旋转的明确动作',component=key)
        return requested


class MotionMonitor:
    def __init__(self,policy):
        self.policy=policy;self.key=None;self.axes=None;self.last=None

    def observe(self,key,position,orientation):
        p=vector(position);q=vector(orientation,4)
        # Include orientation changes via q/-q-invariant angles from a fixed
        # reference; positional axes and angular excursions share the policy.
        if key!=self.key:
            self.key=key;self.reference=q.copy();self.axes=None;self.last=None
        value=np.r_[p,orientation_error_deg(q.tolist(),self.reference.tolist())]
        if self.last is None:
            self.axes=[{'low':float(v),'high':float(v),'direction':0,'reversals':0} for v in value]
            self.last=value;return
        limits=[self.policy['motion']['position_deadband_m']]*3+[self.policy['motion']['orientation_deadband_deg']]
        for axis,(v,deadband) in enumerate(zip(value,limits)):
            state=self.axes[axis];direction=state['direction']
            state['low']=min(state['low'],float(v));state['high']=max(state['high'],float(v))
            if direction>=0 and state['high']-v>deadband:
                if direction==1: state['reversals']+=1
                state.update(direction=-1,low=float(v))
            elif direction<=0 and v-state['low']>deadband:
                if direction==-1: state['reversals']+=1
                state.update(direction=1,high=float(v))
            if state['reversals']>=self.policy['motion']['significant_reversals_before_stop']:
                raise PhysicalConstraintError('motion_oscillation','机械臂持续反复摆动，停止当前动作',
                                              axis=axis,reversals=state['reversals'],deadband=deadband)
        self.last=value


def check_joint_limits(position,velocity,position_limits,velocity_limits,tolerance=1e-6):
    p=np.asarray(position,dtype=float);v=np.asarray(velocity,dtype=float)
    bounds=np.asarray(position_limits,dtype=float);speed=np.asarray(velocity_limits,dtype=float)
    if (p.ndim!=1 or v.shape!=p.shape or bounds.shape!=(len(p),2) or speed.shape!=p.shape
            or not np.isfinite(p).all() or not np.isfinite(v).all()
            or not np.isfinite(bounds).all() or not np.isfinite(speed).all()
            or np.any(bounds[:,0]>=bounds[:,1]) or np.any(speed<=0)):
        raise PhysicalConstraintError('joint_limit_evidence','缺少有效的实际关节位置/速度限值，不能运行')
    invalid=np.flatnonzero((p<bounds[:,0]-tolerance)|(p>bounds[:,1]+tolerance)|(abs(v)>speed+tolerance))
    if len(invalid):
        raise PhysicalConstraintError('joint_limit','实际关节位置或速度超出模型登记限值',joint_indices=invalid.tolist())


def require_self_collision(enabled):
    if enabled is not True:
        raise PhysicalConstraintError('self_collision_disabled',
            '机器人资产未明确启用 PhysX 自碰撞解算，停止启动仿真')


def segment_box(start,end,lower,upper):
    start,end,lower,upper=map(np.asarray,(start,end,lower,upper))
    delta=end-start;near,far=0.,1.
    for axis in range(3):
        if abs(delta[axis])<1e-12:
            if start[axis]<lower[axis] or start[axis]>upper[axis]: return False
        else:
            a,b=(lower[axis]-start[axis])/delta[axis],(upper[axis]-start[axis])/delta[axis]
            near=max(near,min(a,b));far=min(far,max(a,b))
    return near<=far


def choose_overhead_route(start,target_xy,body_lower_offset,body_upper_offset,obstacles,policy):
    """Select the shortest feasible simple transfer at geometry-derived height.

    This plans the observed hand/carried envelope, not a full joint-space IK
    solution. All-robot checks still run on the actual articulation each step.
    """
    start=vector(start);target=vector([*target_xy,start[2]])
    low,high=vector(body_lower_offset),vector(body_upper_offset)
    if np.any(low>=high) or not obstacles:
        raise PhysicalConstraintError('planning_geometry','缺少有效的手部/携带物或硬件几何')
    limits=policy['motion'];clearance=limits['transit_clearance_m']
    epsilon=limits['numeric_tolerance']
    zmin,zmax=limits['registered_transit_z_m'];xb,yb=limits['registered_xy_m']
    if any(not (xb[0]<=p[0]<=xb[1] and yb[0]<=p[1]<=yb[1]) for p in (start,target)):
        raise PhysicalConstraintError('workspace_bounds','转移路径超出登记平面工作范围')
    xy_routes=[[start[:2],target[:2]]]
    for row in obstacles:
        a,b=vector(row['lower_m']),vector(row['upper_m'])
        for y in (a[1]-high[1]-clearance-epsilon,b[1]-low[1]+clearance+epsilon):
            if yb[0]<=y<=yb[1]: xy_routes.append([start[:2],[start[0],y],[target[0],y],target[:2]])
        for x in (a[0]-high[0]-clearance-epsilon,b[0]-low[0]+clearance+epsilon):
            if xb[0]<=x<=xb[1]: xy_routes.append([start[:2],[x,start[1]],[x,target[1]],target[:2]])
    candidates=[];seen=set()
    for route in xy_routes:
        key=tuple(tuple(round(float(v),8) for v in p) for p in route)
        if key in seen: continue
        seen.add(key);height=max(zmin,float(start[2]))
        for a,b in zip(route,route[1:]):
            for row in obstacles:
                lo=vector(row['lower_m'])-high-clearance
                hi=vector(row['upper_m'])-low+clearance
                if segment_box([*a,0.],[*b,0.],[*lo[:2],-1.],[*hi[:2],1.]):
                    height=max(height,float(row['upper_m'][2])-low[2]+clearance+epsilon)
        if height>zmax: continue
        points=[start.tolist(),[start[0],start[1],height]]+[[*p,height] for p in route[1:]]
        points=prepare_waypoints(points)
        blocked=False
        for a,b in zip(points,points[1:]):
            for row in obstacles:
                if segment_box(a,b,vector(row['lower_m'])-high-clearance,
                               vector(row['upper_m'])-low+clearance):
                    # A confirmed contact/near-contact start may retreat
                    # vertically out of the planning allowance. It must still
                    # avoid the actual hardware envelope throughout the lift.
                    escaping=(np.linalg.norm(np.asarray(a)-start)<epsilon
                              and abs(a[0]-b[0])<epsilon and abs(a[1]-b[1])<epsilon
                              and b[2]>a[2]
                              and not segment_box(a,b,vector(row['lower_m'])-high+epsilon,
                                                     vector(row['upper_m'])-low-epsilon))
                    if not escaping: blocked=True;break
            if blocked: break
        if not blocked:
            length=sum(math.dist(a,b) for a,b in zip(points,points[1:]))
            candidates.append((length,len(points),height,points))
    if not candidates:
        raise PhysicalConstraintError('no_clear_transfer','登记工作空间内没有满足避让和高度约束的转移路径')
    length,_,height,points=min(candidates,key=lambda row:(row[1],row[0],row[2]))
    return {'waypoints_m':points[1:] or points,'height_m':height,'path_length_m':length,
            'clearance_m':clearance,'basis':'observed_hand_and_carried_envelope_vs_static_hardware'}
