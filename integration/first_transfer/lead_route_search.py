"""Compute lead approach alternatives before any robot motion is dispatched."""
import math
from motion_constraints import PhysicalConstraintError


def sampled_route(start,waypoints,spacing=.02):
    points=[];current=list(start)
    for target in waypoints:
        count=max(1,math.ceil(math.dist(current,target)/spacing))
        points.extend([[current[k]+(target[k]-current[k])*i/count for k in range(3)]
                       for i in range(1,count+1)])
        points[-1]=list(target)
        current=list(target)
    return points


def search_approach(start,target,minimum_height,maximum_height,geometry_check,reachability_check):
    """Search registered approach heights; retain a final vertical descent.

    Both callbacks are read-only calculations. They may reject a candidate;
    no trial movement, fixture relocation or acceptance-limit change occurs.
    """
    if minimum_height<=target[2] or maximum_height<minimum_height:
        raise ValueError('invalid lead approach height range')
    count=max(1,math.ceil((maximum_height-minimum_height)/.04))
    heights={minimum_height,maximum_height,min(max(start[2],minimum_height),maximum_height)}
    heights.update(min(maximum_height,minimum_height+.04*i) for i in range(count+1))
    candidates=[]
    for height in sorted(heights):
        above=[target[0],target[1],height]
        routes=[[above],[[start[0],start[1],height],above],
                [[start[0],start[1],height],[target[0],start[1],height],above],
                [[start[0],start[1],height],[start[0],target[1],height],above]]
        for route in routes:
            clean=[];last=list(start)
            for p in route:
                if math.dist(last,p)>1e-8:clean.append(p);last=p
            length=sum(math.dist(a,b) for a,b in zip([start]+clean,clean+[target]))
            candidates.append((len(clean),length,height,clean))
    rejected=[]
    for _,length,height,route in sorted(candidates,key=lambda x:(x[0],x[1],x[2])):
        full=sampled_route(start,route+[list(target)])
        try:
            geometry_check(full)
            result=reachability_check(full)
        except (ValueError,RuntimeError) as exc:
            rejected.append({'height_ee_m':height,'reason':str(exc),
                             'code':getattr(exc,'code',None),'evidence':getattr(exc,'evidence',None)})
            continue
        return {'waypoints_m':route,'height_m':height,'path_length_m':length,
                'search_height_range_ee_m':[minimum_height,maximum_height],
                'computed_candidate_count':len(rejected)+1,'available_candidate_count':len(candidates),
                'maximum_kinematic_error_m':result.get('maximum_kinematic_error_m'),
                'minimum_joint_limit_margin_rad':result.get('minimum_joint_limit_margin_rad'),
                'basis':'static_hardware_clearance_and_live_joint_reachability_search',
                'final_descent':'constant_target_xy','physical_trial_motions':0}
    raise PhysicalConstraintError('no_clear_reachable_approach',
        '计算搜索未找到同时满足设备避让和关节可达性的接近路线',
        rejected_candidates=rejected)
