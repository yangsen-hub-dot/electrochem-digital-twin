"""Metre-based, length-constrained cable geometry for the measurement proxy.

The declared 0.65 m per conductor is a user-authorized scene assumption, not
manufacturer data. Distance constraints and downward relaxation preserve a
discrete cable's length; static USD bounding envelopes prevent penetration.
Version 3 also rejects centerline self-contact using the declared scene wire
radius. This is quasi-static geometry, not calibrated dynamics, friction,
tension, electrical contact or bend-stiffness validation.
"""
import math
import numpy as np

MODEL_ID = 'fixed_length_gravity_relaxation_v2'
SCENE_LENGTH_M = .65
SCENE_SLACK_SEED_M = ((.746,-.13,.035),(.75,-.30,.002),(.79,-.36,.002),(.74,-.38,.002))
PAIR_CLEARANCE_RADIUS_M = .0016  # 0.7 mm radius + 0.85 mm colour-line offset
WIRE_RADIUS_M = .0007  # visual conductor radius; scenario geometry, not calibration
WIRE_DIAMETER_M = 2.0 * WIRE_RADIUS_M
LENGTH_TOLERANCE_M = .0005
LINK_TOLERANCE_M = .00005


def segment_hits(points, lower, upper):
    """All straight links against a closed axis-aligned envelope."""
    p = np.asarray(points, dtype=float)
    a, d = p[:-1], np.diff(p, axis=0)
    enter, leave = np.zeros(len(d)), np.ones(len(d))
    valid = np.ones(len(d), dtype=bool)
    for axis in range(3):
        parallel = abs(d[:, axis]) < 1e-12
        valid &= ~parallel | ((a[:, axis] >= lower[axis]) & (a[:, axis] <= upper[axis]))
        divisor = np.where(parallel, 1., d[:, axis])
        t0 = (lower[axis]-a[:, axis])/divisor
        t1 = (upper[axis]-a[:, axis])/divisor
        enter = np.maximum(enter, np.where(parallel, -np.inf, np.minimum(t0, t1)))
        leave = np.minimum(leave, np.where(parallel, np.inf, np.maximum(t0, t1)))
    mask = valid & (enter <= leave)
    return np.flatnonzero(mask), (enter+leave)*.5


def segment_distance(a0, a1, b0, b1):
    """Minimum Euclidean distance between two finite 3-D line segments."""
    p, q = np.asarray(a0,dtype=float), np.asarray(b0,dtype=float)
    u, v = np.asarray(a1,dtype=float)-p, np.asarray(b1,dtype=float)-q
    w=p-q; aa=float(u@u); bb=float(u@v); cc=float(v@v)
    dd=float(u@w); ee=float(v@w); denom=aa*cc-bb*bb
    if aa<=1e-24 or cc<=1e-24: raise ValueError('Degenerate cable segment')
    if denom<=1e-24:
        s=0.
        t=float(np.clip(ee/cc,0.,1.))
    else:
        s=float(np.clip((bb*ee-cc*dd)/denom,0.,1.))
        t=(bb*s+ee)/cc
        if t<0.:
            t=0.;s=float(np.clip(-dd/aa,0.,1.))
        elif t>1.:
            t=1.;s=float(np.clip((bb-dd)/aa,0.,1.))
    return float(np.linalg.norm((p+s*u)-(q+t*v)))


def polyline_self_clearance(points, minimum_m=WIRE_DIAMETER_M):
    """Return non-adjacent self-contact violations for a cable centreline."""
    p=np.asarray(points,dtype=float)
    violations=[]
    for i in range(len(p)-1):
        for j in range(i+2,len(p)-1):
            distance=segment_distance(p[i],p[i+1],p[j],p[j+1])
            if distance < minimum_m-1e-9:
                violations.append({'links':[i,j],'distance_m':distance,
                                   'minimum_m':minimum_m})
    return violations


def polylines_clearance(first,second,minimum_m=WIRE_DIAMETER_M):
    """Check minimum clearance between every pair of segments in two cables."""
    a,b=np.asarray(first,dtype=float),np.asarray(second,dtype=float)
    violations=[]
    for i in range(len(a)-1):
        for j in range(len(b)-1):
            distance=segment_distance(a[i],a[i+1],b[j],b[j+1])
            if distance < minimum_m-1e-9:
                violations.append({'links':[i,j],'distance_m':distance,
                                   'minimum_m':minimum_m})
    return violations


def conductor_length_violations(points,length_m,links=None,
                                length_tolerance_m=LENGTH_TOLERANCE_M,
                                link_tolerance_m=LINK_TOLERANCE_M):
    p=np.asarray(points,dtype=float)
    if p.ndim!=2 or p.shape[1]!=3 or len(p)<2 or not np.isfinite(p).all():
        raise ValueError('Invalid conductor points')
    count=len(p)-1 if links is None else int(links)
    if count!=len(p)-1 or length_m<=0:
        raise ValueError('Conductor discretization does not match declared length')
    segment_lengths=np.linalg.norm(np.diff(p,axis=0),axis=1)
    error=abs(float(np.sum(segment_lengths))-float(length_m))
    maximum_link_error=float(np.max(np.abs(segment_lengths-float(length_m)/count)))
    violations=[]
    if error>length_tolerance_m:
        violations.append({'type':'arc_length','error_m':error,
                           'tolerance_m':length_tolerance_m})
    if maximum_link_error>link_tolerance_m:
        violations.append({'type':'link_length','error_m':maximum_link_error,
                           'tolerance_m':link_tolerance_m})
    return violations


def paired_conductor_points(centerline,half_spacing_m=.00085):
    """Offset a paired lead along a continuous horizontal normal frame.

    Endpoints retain the connector's world-X separation. The small twist is
    distributed over several links so the two strands do not cross at bends.
    This is a geometric cable-pair layout, not torsional mechanics.
    """
    p=np.asarray(centerline,dtype=float)
    if p.ndim!=2 or p.shape[1]!=3 or len(p)<8 or not np.isfinite(p).all():
        raise ValueError('Invalid cable centreline for paired conductors')
    tangents=np.empty_like(p)
    tangents[0]=p[1]-p[0];tangents[-1]=p[-1]-p[-2]
    tangents[1:-1]=p[2:]-p[:-2]
    normals=[]
    for tangent in tangents:
        horizontal=tangent[:2];norm=float(np.linalg.norm(horizontal))
        if norm<=1e-10:
            normal=np.array([1.,0.,0.]) if not normals else normals[-1].copy()
        else:
            normal=np.array([-horizontal[1]/norm,horizontal[0]/norm,0.])
            if not normals and normal[0]<0: normal=-normal
            elif normals and normal@normals[-1]<0: normal=-normal
        normals.append(normal)
    normals=np.asarray(normals)
    # Connector ports use fixed +/-X offsets; smoothly twist into the routed
    # pair frame and back, rather than imposing a world-axis offset at bends.
    start_axis=np.array([1.,0.,0.]);end_axis=np.array([1.,0.,0.])
    for rank,index in enumerate(range(4)):
        fraction=rank/3
        value=(1.-fraction)*start_axis+fraction*normals[index]
        length=float(np.linalg.norm(value))
        if length>1e-12: normals[index]=value/length
    for rank,index in enumerate(range(len(p)-4,len(p))):
        fraction=rank/3
        value=(1.-fraction)*normals[index]+fraction*end_axis
        length=float(np.linalg.norm(value))
        if length>1e-12: normals[index]=value/length
    return p-half_spacing_m*normals,p+half_spacing_m*normals


def project_coupled_links(points, boxes, weights):
    """Resolve a link against adjacent envelopes together, avoiding ping-pong.

    Individually clearing opposite corners can leave both vertices outside
    solids while their connecting link still crosses both. Use an available
    geometric gap; keep pinned endpoints fixed and all clearance unchanged.
    """
    contacts = {}
    for index,(lo,hi) in enumerate(boxes):
        ids,_ = segment_hits(points,lo+1e-8,hi-1e-8)
        for link in ids: contacts.setdefault(int(link),[]).append(index)
    for link,members in contacts.items():
        if len(members)<2: continue
        options=[]
        pair=points[link:link+2]
        for a in members:
            for b in members:
                if a==b: continue
                for axis in range(3):
                    low,high=boxes[a][1][axis]+1e-6,boxes[b][0][axis]-1e-6
                    if low<high:
                        target=np.clip(pair[:,axis],low,high)
                        cost=float(np.sum((target-pair[:,axis])**2))
                        options.append((cost,axis,target))
        if options:
            _,axis,target=min(options,key=lambda row:row[0])
            for offset,node in enumerate((link,link+1)):
                if weights[node]: points[node,axis]=target[offset]


def project_link_outside_box(pair, lo, hi, weights):
    """Place the entire link in a separating half-space, not only its midpoint."""
    options=[]
    for axis in range(3):
        for upper in (False,True):
            if axis==2 and not upper and lo[2]<=PAIR_CLEARANCE_RADIUS_M: continue
            target=(np.maximum(pair[:,axis],hi[axis]+1e-6) if upper
                    else np.minimum(pair[:,axis],lo[axis]-1e-6))
            delta=target-pair[:,axis]
            if np.any((weights==0)&(abs(delta)>1e-10)): continue
            options.append((float(sum(delta*delta)),axis,target))
    if options:
        _,axis,target=min(options,key=lambda row:row[0])
        pair[:,axis]=target


class ConstrainedCable:
    def __init__(self, start, endpoint, envelopes=(), *, length_m=SCENE_LENGTH_M, links=64):
        self.start = np.asarray(start, dtype=float)
        self.endpoint = np.asarray(endpoint, dtype=float)
        if (self.start.shape != (3,) or self.endpoint.shape != (3,)
                or not np.isfinite([*self.start, *self.endpoint, length_m]).all()
                or length_m <= 0 or links < 16):
            raise ValueError('Invalid cable endpoints or declared length')
        self.length_m, self.link_length = float(length_m), float(length_m)/links
        self.envelopes = list(envelopes)
        self.boxes = [(np.asarray(b['lower_m'])-PAIR_CLEARANCE_RADIUS_M,
                       np.asarray(b['upper_m'])+PAIR_CLEARANCE_RADIUS_M) for b in self.envelopes]
        self.lowers = np.array([b[0] for b in self.boxes]).reshape(-1,3)
        self.uppers = np.array([b[1] for b in self.boxes]).reshape(-1,3)
        # Lay spare cable on the clear right/front bench at scene creation.
        # The old seed threaded slack into the narrow IKA/tray gap. These
        # points initialize the free cable; none remains a fixed guide.
        anchors = np.array([self.start, *SCENE_SLACK_SEED_M, self.endpoint])
        distances = np.r_[0., np.cumsum(np.linalg.norm(np.diff(anchors, axis=0), axis=1))]
        at = np.linspace(0, distances[-1], links+1)
        self.points = np.stack([np.interp(at, distances, anchors[:,i]) for i in range(3)], axis=1)
        self.weights = np.ones(links+1); self.weights[[0, -1]] = 0
        self.previous_endpoint = None
        self.metrics = None

    def _pin(self):
        self.points[0], self.points[-1] = self.start, self.endpoint

    def _length_constraints(self):
        # Solve the linearized distance constraints together. A local
        # odd/even projection propagated endpoint motion too slowly around
        # equipment corners and could remain stuck with stretched links.
        delta=np.diff(self.points,axis=0)
        distance=np.linalg.norm(delta,axis=1)
        unit=delta/np.maximum(distance[:,None],1e-12)
        diagonal=self.weights[:-1]+self.weights[1:]+1e-9
        off=-self.weights[1:-1]*np.sum(unit[:-1]*unit[1:],axis=1)
        rhs=distance-self.link_length
        # Thomas solve of J W J^T. The tiny diagonal term regularizes a
        # straight subchain; final acceptance still uses the original limits.
        for index in range(1,len(rhs)):
            factor=off[index-1]/diagonal[index-1]
            diagonal[index]-=factor*off[index-1]
            rhs[index]-=factor*rhs[index-1]
        multipliers=np.empty(len(rhs))
        multipliers[-1]=rhs[-1]/diagonal[-1]
        for index in range(len(rhs)-2,-1,-1):
            multipliers[index]=(rhs[index]-off[index]*multipliers[index+1])/diagonal[index]
        vectors=unit*multipliers[:,None]
        correction=np.zeros_like(self.points)
        correction[:-1]+=vectors;correction[1:]-=vectors
        correction*=self.weights[:,None]
        maximum=float(max(np.linalg.norm(correction,axis=1)))
        self.points+=correction*min(1.,.005/max(maximum,1e-12))
        self._pin()

    def _contacts(self, check_links=False):
        self.points[1:-1,2] = np.maximum(self.points[1:-1,2], PAIR_CLEARANCE_RADIUS_M)
        overall_low, overall_high = self.points.min(axis=0), self.points.max(axis=0)
        relevant = np.flatnonzero(np.all(self.uppers>=overall_low,axis=1)
                                 & np.all(self.lowers<=overall_high,axis=1))
        for box_index in relevant:
            lo,hi = self.boxes[box_index]
            inside = np.all((self.points > lo) & (self.points < hi), axis=1)
            inside[[0,-1]] = False
            indices = np.flatnonzero(inside)
            if len(indices):
                delta = np.concatenate([self.points[indices]-lo, hi-self.points[indices]], axis=1)
                # A bench-supported obstacle cannot eject a node through its
                # underside into the tabletop. Choose a feasible side/top.
                if lo[2] <= PAIR_CLEARANCE_RADIUS_M: delta[:,2] = np.inf
                faces = np.argmin(delta, axis=1)
                for index, face in zip(indices, faces):
                    self.points[index,face%3] = (lo[face%3]-1e-7 if face<3 else hi[face%3]+1e-7)
            if check_links:
                indices, times = segment_hits(self.points, lo+1e-8, hi-1e-8)
                for index in indices:
                    project_link_outside_box(self.points[index:index+2],lo,hi,
                                             self.weights[index:index+2])
        if check_links:
            project_coupled_links(self.points,[self.boxes[i] for i in relevant],self.weights)
        self.points[1:-1,2] = np.maximum(self.points[1:-1,2], PAIR_CLEARANCE_RADIUS_M)
        self._pin()

    def _measure(self):
        lengths = np.linalg.norm(np.diff(self.points, axis=0), axis=1)
        collisions = []
        relevant = np.flatnonzero(np.all(self.uppers>=self.points.min(axis=0),axis=1)
                                 & np.all(self.lowers<=self.points.max(axis=0),axis=1))
        for box_index in relevant:
            box = self.envelopes[box_index];lo,hi = self.boxes[box_index]
            hits,_ = segment_hits(self.points, lo+1e-8, hi-1e-8)
            if len(hits): collisions.append({'path': box['path'], 'links': hits.tolist()})
        self_contacts=polyline_self_clearance(self.points)
        return {'arc_length_m': float(sum(lengths)),
                'length_error_m': float(sum(lengths)-self.length_m),
                'max_link_error_m': float(max(abs(lengths-self.link_length))),
                'minimum_height_m': float(min(self.points[:,2])),
                'envelope_intersections': collisions,
                'self_contact_violations':self_contacts}

    def update(self, endpoint):
        endpoint = np.asarray(endpoint,dtype=float)
        if endpoint.shape != (3,) or not np.isfinite(endpoint).all():
            raise ValueError('Invalid moving cable endpoint')
        if self.previous_endpoint is not None and np.array_equal(endpoint,self.previous_endpoint):
            return self.points
        self.endpoint = endpoint
        if math.dist(self.start, endpoint) >= self.length_m-.001:
            raise RuntimeError('导线端点超出登记线长，停止；不延长导线')
        self._pin()
        initial = self.previous_endpoint is None
        # Numerical equilibrium iterations carry no physical time or force claim.
        relaxation = 2500 if initial else 500
        for index in range(relaxation+8000):
            if index < relaxation:
                self.points[1:-1,2] -= .00003
                self.points[1:-1] += .02*(.5*(self.points[:-2]+self.points[2:])-self.points[1:-1])
            self._length_constraints()
            self._contacts(check_links=index%20 == 0)
            if index >= relaxation+50 and index%50 == 0:
                metrics = self._measure()
                if (abs(metrics['length_error_m']) <= LENGTH_TOLERANCE_M
                        and metrics['max_link_error_m'] <= LINK_TOLERANCE_M
                        and metrics['minimum_height_m'] >= PAIR_CLEARANCE_RADIUS_M-1e-9
                        and not metrics['envelope_intersections']):
                    self.metrics = metrics
                    self.previous_endpoint = endpoint.copy()
                    return self.points
        raise RuntimeError('导线定长/设备包围体约束未收敛，停止；不使用拉长或穿透的曲线: '+str(self._measure()))


def scene_envelopes(stage, root_path, extra_prefixes=()):
    """Read static visible USD mesh/cube bounds; no invented invisible guide."""
    from pxr import Usd, UsdGeom
    prefixes = ('/IKA_CHEM_008/', '/DualCellElectrodeSupport/', '/Microcell',
                '/Gamry_CHEM_103/', '/LeadTray/', '/HalfCellHolder/')+tuple(extra_prefixes)
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default','render'])
    result = []
    for prim in Usd.PrimRange(stage.GetPrimAtPath(root_path), Usd.TraverseInstanceProxies()):
        name = str(prim.GetPath())
        relative = name[len(root_path):]
        if not any(relative.startswith(prefix) for prefix in prefixes): continue
        if not (prim.IsA(UsdGeom.Mesh) or prim.IsA(UsdGeom.Cube)): continue
        if UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible: continue
        bound = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        lower,upper = list(bound.GetMin()), list(bound.GetMax())
        if not np.isfinite(lower+upper).all() or any(a>=b for a,b in zip(lower,upper)): continue
        result.append({'path':name, 'lower_m':lower, 'upper_m':upper,
                       'basis':'conservative_visible_usd_primitive_world_aabb'})
    return result
