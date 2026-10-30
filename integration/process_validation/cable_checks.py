"""Independent checks of rendered conductor vertices; no cable solver imports."""
import json
import numpy as np

WIRE_DIAMETER_M = .0014


def segment_distance(a0,a1,b0,b1):
    p,q=np.asarray(a0,dtype=float),np.asarray(b0,dtype=float)
    u,v=np.asarray(a1,dtype=float)-p,np.asarray(b1,dtype=float)-q
    w=p-q;aa=float(u@u);bb=float(u@v);cc=float(v@v);dd=float(u@w);ee=float(v@w)
    denominator=aa*cc-bb*bb
    if denominator<=1e-24:
        s=0.;t=float(np.clip(ee/cc,0.,1.))
    else:
        s=float(np.clip((bb*ee-cc*dd)/denominator,0.,1.));t=(bb*s+ee)/cc
        if t<0.:
            t=0.;s=float(np.clip(-dd/aa,0.,1.))
        elif t>1.:
            t=1.;s=float(np.clip((bb-dd)/aa,0.,1.))
    return float(np.linalg.norm((p+s*u)-(q+t*v)))


def clearance_violations(first,second=None,minimum_m=WIRE_DIAMETER_M):
    a=np.asarray(first,dtype=float);b=a if second is None else np.asarray(second,dtype=float)
    violations=[]
    for i in range(len(a)-1):
        for j in range(i+2 if second is None else 0,len(b)-1):
            distance=segment_distance(a[i],a[i+1],b[j],b[j+1])
            if distance<minimum_m-1e-9:
                violations.append({'links':[i,j],'distance_m':distance,'minimum_m':minimum_m})
    return violations


def crosses_box(points, lo, hi):
    a, delta = points[:-1], np.diff(points,axis=0)
    tmin, tmax = np.zeros(len(delta)), np.ones(len(delta))
    valid = np.ones(len(delta),dtype=bool)
    for axis in range(3):
        parallel = abs(delta[:,axis]) < 1e-12
        valid &= ~parallel | ((a[:,axis] >= lo[axis]) & (a[:,axis] <= hi[axis]))
        d = np.where(parallel,1.,delta[:,axis])
        near, far = (lo[axis]-a[:,axis])/d, (hi[axis]-a[:,axis])/d
        tmin = np.maximum(tmin,np.where(parallel,-np.inf,np.minimum(near,far)))
        tmax = np.minimum(tmax,np.where(parallel,np.inf,np.maximum(near,far)))
    return bool(np.any(valid & (tmin <= tmax)))


def check_cables(root, manifest, report):
    model = manifest.get('presentation',{}).get('measurement',{}).get('cable_constraints')
    if model is None:
        report.check('geometry','cable_length_contact','uncovered',
                     '旧录像没有定长导线和接触包围体证据，不能据此认定导线未被拉长或穿透')
        return
    failures, count, maximum_error, maximum_shift = 0,0,0.,0.
    previous = {}
    try:
        length = float(model['conductor_length_m'])
        tolerance = float(model['length_tolerance_m'])
        link_tolerance = float(model['max_link_tolerance_m'])
        clearance = float(model.get('wire_self_and_pair_clearance_m',WIRE_DIAMETER_M))
        wire_radius = float(model.get('wire_radius_m',WIRE_DIAMETER_M/2))
        boxes = [(b['path'],np.asarray(b['lower_m'],dtype=float),np.asarray(b['upper_m'],dtype=float))
                 for b in model['static_envelopes']]
        if (model['model_id'] not in {'fixed_length_gravity_relaxation_v1','fixed_length_gravity_relaxation_v2','fixed_length_gravity_relaxation_v3'}
                or not 0 < length <= 2 or not 0 < tolerance <= .0005
                or not 0 < link_tolerance <= .00005 or not boxes
                or not np.isfinite([clearance,wire_radius]).all()
                or not 0 < wire_radius <= .01 or abs(clearance-2*wire_radius)>1e-9
                or any(lo.shape!=(3,) or hi.shape!=(3,) or not np.isfinite([lo,hi]).all()
                       or np.any(lo>=hi) for _,lo,hi in boxes)):
            raise ValueError('invalid cable constraints or static envelope')
        with (root/'trace.jsonl').open(encoding='utf8') as stream:
            for line in stream:
                sample = json.loads(line)
                index = sample['frame_index']; frame = manifest['frames'][index]
                source = manifest['presentation']['measurement']['instrument']['cable_port_m']
                for side in ('left','right'):
                    loc = {'frame_index':index,'video_time_s':index/manifest['fps'],
                           'step_id':sample.get('step_id'),'side':side}
                    evidence = sample['measurement_cables'][side]
                    clip = frame['lead_centers_m'][side]
                    side_points=[]
                    for colour, vertices in enumerate(evidence['conductors_m']):
                        p = np.asarray(vertices,dtype=float)
                        if p.ndim!=2 or p.shape[1]!=3 or len(p)<17 or not np.isfinite(p).all():
                            raise ValueError('missing or invalid rendered conductor vertices')
                        offset = (-1 if colour==0 else 1)*.00085
                        start = np.array(source)+[offset,0,0]
                        end = np.array(clip)+[offset,-.006,0]
                        lengths = np.linalg.norm(np.diff(p,axis=0),axis=1)
                        error = abs(float(sum(lengths))-length)
                        maximum_error = max(maximum_error,error)
                        wrong = (error>tolerance or max(abs(lengths-length/(len(p)-1)))>link_tolerance
                                 or np.linalg.norm(p[0]-start)>1e-6 or np.linalg.norm(p[-1]-end)>1e-6)
                        hits = [path for path,lo,hi in boxes
                                if crosses_box(p,lo+.0000001-.0007,hi-.0000001+.0007)]
                        if wrong:
                            failures += 1
                            report.issue('geometry','cable_length_or_attachment','fail',
                                         '实际绘制导线弧长/分段长度或两端连接偏离登记约束',
                                         arc_length_m=float(sum(lengths)),length_error_m=error,**loc)
                        if min(p[:,2]) < .0007-1e-7 or hits:
                            failures += 1
                            report.issue('geometry','cable_envelope_penetration','fail',
                                         '实际导线线段进入台面或设备保守包围体',obstacles=hits,**loc)
                        self_hits=(clearance_violations(p,minimum_m=clearance)
                                   if model['model_id']=='fixed_length_gravity_relaxation_v3' else [])
                        if self_hits:
                            failures+=1
                            report.issue('geometry','cable_self_contact','fail',
                                         '同一导体非相邻线段间距小于登记线径',
                                         violations=self_hits[:8],**loc)
                        side_points.append(p)
                        key = (side,colour)
                        if key in previous and previous[key].shape==p.shape:
                            maximum_shift=max(maximum_shift,float(np.max(np.linalg.norm(p-previous[key],axis=1))))
                        previous[key]=p
                        count += 1
                    pair_hits=(clearance_violations(side_points[0],side_points[1],minimum_m=clearance)
                               if model['model_id']=='fixed_length_gravity_relaxation_v3' and len(side_points)==2 else [])
                    if model['model_id']=='fixed_length_gravity_relaxation_v3' and (len(side_points)!=2 or pair_hits):
                        failures+=1
                        report.issue('geometry','paired_conductor_clearance','fail',
                                     '同一引线的两根导体间距小于登记线径',
                                     violations=pair_hits[:8],**loc)
        if count != len(manifest['frames'])*4: raise ValueError('cable frame coverage is incomplete')
    except (ValueError,TypeError,KeyError,IndexError,OSError) as exc:
        report.issue('geometry','cable_evidence','fail','定长导线证据不完整或无效：'+str(exc))
        return
    if not failures:
        report.check('geometry','cable_length_contact','pass',
                     '逐帧实际导线顶点满足登记弧长/分段长度、端点、台面与静态包围体约束',
                     samples=count,maximum_length_error_m=maximum_error,
                     maximum_sampled_vertex_shift_m=maximum_shift)
    report.check('geometry','cable_mechanics','uncovered',
                 '定长与有限线径间隙检查不求解真实张力、摩擦、弯曲刚度、滞回或导线与机械臂接触')
