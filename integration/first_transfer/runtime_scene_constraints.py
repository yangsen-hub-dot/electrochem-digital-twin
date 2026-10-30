"""Franka runtime geometry/limits adapter. No chemistry or scene mutations.

Uses actual PhysX robot-link poses and visible static hardware envelopes.
Imported AABBs conservatively block overlap; they do not certify exact mesh
contact. Whole-arm continuous self-collision and force dynamics remain outside
this adapter's coverage.
"""
import numpy as np
from cable_constraints import scene_envelopes
from motion_constraints import (PhysicalConstraintError,check_joint_limits,
                                 choose_overhead_route,segment_box)
from pregrasp_planner import orientation_error_deg
from process_validation.geometry import transform,triangles_in_box
from process_validation.scene_trace import body_ancestor_path
from swept_mesh_constraints import swept_triangles_in_aabb


def array(value):
    if hasattr(value,'detach'): value=value.detach().cpu().numpy()
    return np.asarray(value,dtype=float)


def pose_matrix(position,quaternion):
    w,x,y,z=map(float,quaternion)
    matrix=np.eye(4)
    rotation=np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                       [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                       [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    matrix[:3,:3]=rotation.T;matrix[3,:3]=position
    return matrix


class RuntimeSceneConstraints:
    def __init__(self,stage,root_path,robot,policy,extra_static_roots=()):
        from pxr import Usd,UsdGeom
        self.stage,self.robot,self.policy=stage,robot,policy
        self.body_names=list(robot.body_names);self.meshes=[]
        self.hardware=scene_envelopes(stage,root_path,extra_prefixes=(
            '/CopperElectrodeRack/','/PipetteStand_','/SharedToolBank/',
            '/SaltBridgeRack/','/SaltBridgeHolder/','/tip_rack_','/tip_waste_station/'))
        bbox=UsdGeom.BBoxCache(Usd.TimeCode.Default(),['default','render'])
        for root in extra_static_roots:
            for prim in Usd.PrimRange(stage.GetPrimAtPath(root),Usd.TraverseInstanceProxies()):
                if prim.IsA(UsdGeom.Mesh) and UsdGeom.Imageable(prim).ComputeVisibility()!=UsdGeom.Tokens.invisible:
                    b=bbox.ComputeWorldBound(prim).ComputeAlignedRange()
                    lo,hi=list(b.GetMin()),list(b.GetMax())
                    if np.isfinite(lo+hi).all() and all(a<b for a,b in zip(lo,hi)):
                        self.hardware.append({'path':str(prim.GetPath()),'lower_m':lo,'upper_m':hi,
                                              'basis':'initial_static_source_vessel_usd_envelope'})
        cache=UsdGeom.XformCache(Usd.TimeCode.Default())
        found=set()
        for prim in Usd.PrimRange(stage.GetPseudoRoot(),Usd.TraverseInstanceProxies()):
            path=str(prim.GetPath())
            if not prim.IsA(UsdGeom.Mesh) or any(t in path.lower() for t in ('/collision/','/collisions/')): continue
            binding=body_ancestor_path(path,self.body_names)
            if binding is None or UsdGeom.Imageable(prim).ComputeVisibility()==UsdGeom.Tokens.invisible: continue
            body_path,index=binding;body_name=self.body_names[index]
            if not body_name.startswith('panda_'): continue
            mesh=UsdGeom.Mesh(prim);points=array(mesh.GetPointsAttr().Get())
            counts=list(mesh.GetFaceVertexCountsAttr().Get() or [])
            indices=list(mesh.GetFaceVertexIndicesAttr().Get() or [])
            if points.ndim!=2 or points.shape[1]!=3 or not np.isfinite(points).all() or not counts:
                raise PhysicalConstraintError('robot_mesh_evidence','机械臂可见网格缺失或无效',body=path)
            triangles=[];offset=0
            for count in counts:
                face=indices[offset:offset+count];offset+=count
                if count<3: continue
                triangles.extend([[face[0],face[j],face[j+1]] for j in range(1,count-1)])
            if not triangles: raise PhysicalConstraintError('robot_mesh_evidence','机械臂网格缺少可检查面',body=path)
            body_world=cache.GetLocalToWorldTransform(stage.GetPrimAtPath(body_path))
            mesh_world=cache.GetLocalToWorldTransform(prim)
            scaling=np.eye(4);scaling[:3,:3]=np.diag(np.linalg.norm(np.asarray(body_world)[:3,:3],axis=1))
            local=np.asarray(mesh_world*body_world.GetInverse())@scaling
            local_points=transform(points,local)
            if not np.isfinite(local_points).all() or np.max(np.ptp(local_points,axis=0))>1.5:
                raise PhysicalConstraintError('robot_mesh_units','机械臂网格单位或尺寸异常',body=path)
            self.meshes.append({'path':path,'name':body_name,'index':index,
                                'points':local_points,'triangles':np.asarray(triangles,dtype=int)})
            found.add(body_name)
        expected={f'panda_link{i}' for i in range(1,8)}|{'panda_hand','panda_leftfinger','panda_rightfinger'}
        if not self.hardware or not expected.issubset(found):
            raise PhysicalConstraintError('runtime_geometry_coverage','缺少完整登记机械臂连杆或硬件几何，停止',
                                          missing_bodies=sorted(expected-found))
        self.check_count=0;self.planned_path_checks=0
        self.path_mesh_refinements=0;self.path_box_false_positives_cleared=0
        self.coverage={'robot_bodies':sorted(found),'hardware_envelopes':len(self.hardware),
                       'robot_pose_source':'actual_physx_articulation',
                       'hardware_source':'visible_static_usd_envelopes',
                       'hand_path_preflight':'continuous_translating_triangles_vs_static_hardware_envelopes',
                       'rotation_path_preflight':'conservative_envelope_or_explicit_stop_on_overlap',
                       'whole_arm_runtime':'sampled_visible_link_meshes_vs_static_envelopes',
                       'carried_tools':'existing_action_gates_and_registered_overhead_envelope',
                       'continuous_collision_certified':False,'complete_physics_certified':False,
                       'uncovered':list(policy['uncovered'])}

    def check_path(self,ee,points,orientation,actual_orientation):
        """Broad-phase boxes, then continuous mesh sweep for fixed orientation."""
        ee=np.asarray(ee,dtype=float);route=[ee,*map(lambda p:np.asarray(p,dtype=float),points)]
        angle=np.deg2rad(orientation_error_deg(actual_orientation,orientation))
        epsilon=self.policy['motion']['numeric_tolerance']
        for mesh,world in self.world_meshes():
            if mesh['name'] not in ('panda_hand','panda_leftfinger','panda_rightfinger'): continue
            local=world-ee
            # Bound all intermediate orientations by the maximum displacement
            # of any vertex at this rotation angle about the measured EE.
            padding=2.*float(max(np.linalg.norm(local,axis=1)))*np.sin(angle/2.)
            low,high=local.min(axis=0)-padding,local.max(axis=0)+padding
            for segment_index,(a,b) in enumerate(zip(route,route[1:])):
                for row in self.hardware:
                    if segment_box(a,b,np.asarray(row['lower_m'])-high+epsilon,
                                      np.asarray(row['upper_m'])-low-epsilon):
                        evidence={'body':mesh['path'],'obstacle':row['path'],
                                  'segment_index':segment_index,'ee_from_m':a.tolist(),'ee_to_m':b.tolist()}
                        if angle>1e-7:
                            raise PhysicalConstraintError('planned_rotation_clearance_unresolved',
                                '转向扫掠包围体接近硬件，当前精细预检未覆盖该转向，停止派发',
                                **evidence,orientation_change_rad=float(angle))
                        self.path_mesh_refinements=getattr(self,'path_mesh_refinements',0)+1
                        at_start=local+a
                        if swept_triangles_in_aabb(at_start,mesh['triangles'],b-a,
                                np.asarray(row['lower_m'])-padding,
                                np.asarray(row['upper_m'])+padding,epsilon):
                            raise PhysicalConstraintError('planned_hardware_mesh_intersection',
                                '预期平移路径的实际手部网格与登记硬件包围体相交，停止派发',
                                **evidence,method='continuous_triangle_translation_sweep',
                                hardware_basis=row.get('basis'))
                        self.path_box_false_positives_cleared=getattr(self,'path_box_false_positives_cleared',0)+1
        self.planned_path_checks+=1

    def world_meshes(self):
        positions=array(self.robot.data.body_pos_w[0]);quaternions=array(self.robot.data.body_quat_w[0])
        for mesh in self.meshes:
            points=transform(mesh['points'],pose_matrix(positions[mesh['index']],quaternions[mesh['index']]))
            if not np.isfinite(points).all(): raise PhysicalConstraintError('robot_pose_evidence','实际连杆位姿无效')
            yield mesh,points

    def joint_limits(self):
        data=self.robot.data
        position=getattr(data,'joint_pos_limits',None)
        if position is None: position=getattr(data,'default_joint_pos_limits',None)
        velocity=getattr(data,'joint_vel_limits',None)
        if velocity is None: velocity=getattr(data,'soft_joint_vel_limits',None)
        if position is None or velocity is None:
            raise PhysicalConstraintError('joint_limit_evidence','运行环境未提供实际关节限值')
        speed=array(velocity[0])
        actuator=getattr(data,'soft_joint_vel_limits',None)
        if actuator is not None: speed=np.minimum(speed,array(actuator[0]))
        check_joint_limits(array(data.joint_pos[0]),array(data.joint_vel[0]),array(position[0]),
                           speed,self.policy['motion']['numeric_tolerance'])

    def check(self,label):
        self.joint_limits();tolerance=self.policy['motion']['numeric_tolerance']
        for mesh,points in self.world_meshes():
            lo,hi=points.min(axis=0),points.max(axis=0)
            # Fixed mounting/base geometry may intentionally meet the bench.
            if mesh['name'] not in ('panda_link0','panda_link1') and lo[2]<-tolerance:
                raise PhysicalConstraintError('bench_penetration','机械臂进入台面以下',body=mesh['path'],phase=label)
            for obstacle in self.hardware:
                lower,upper=np.asarray(obstacle['lower_m']),np.asarray(obstacle['upper_m'])
                if np.any(hi<lower) or np.any(lo>upper): continue
                center=(lower+upper)/2;size=upper-lower
                inverse=np.eye(4);inverse[:3,:3]=np.diag(1./size);inverse[3,:3]=-center/size
                if triangles_in_box(points,mesh['triangles'],inverse,1.,tolerance):
                    raise PhysicalConstraintError('hardware_envelope_overlap',
                         '机械臂进入登记硬件的保守包围体，停止动作',
                         body=mesh['path'],obstacle=obstacle['path'],phase=label,
                         basis=obstacle['basis'])
        self.check_count+=1

    def overhead_route(self,ee,target_xy,carried_bounds=None):
        parts=[points for mesh,points in self.world_meshes()
               if mesh['name'] in ('panda_hand','panda_leftfinger','panda_rightfinger')]
        if carried_bounds is not None: parts.append(np.asarray(carried_bounds,dtype=float))
        points=np.vstack(parts);ee=np.asarray(ee,dtype=float)
        return choose_overhead_route(ee,target_xy,points.min(axis=0)-ee,points.max(axis=0)-ee,
                                     self.hardware,self.policy)

    def object_bounds(self,path):
        from pxr import Usd,UsdGeom
        prim=self.stage.GetPrimAtPath(path)
        if not prim.IsValid(): raise PhysicalConstraintError('carried_geometry','缺少携带物几何',path=path)
        box=UsdGeom.BBoxCache(Usd.TimeCode.Default(),['default','render']).ComputeWorldBound(prim).ComputeAlignedRange()
        lo,hi=array(box.GetMin()),array(box.GetMax())
        if not np.isfinite([lo,hi]).all() or np.any(lo>=hi):
            raise PhysicalConstraintError('carried_geometry','携带物包围尺寸无效',path=path)
        return np.array([[x,y,z] for x in (lo[0],hi[0]) for y in (lo[1],hi[1]) for z in (lo[2],hi[2])])
