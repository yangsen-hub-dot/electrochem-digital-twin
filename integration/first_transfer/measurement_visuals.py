"""CHEM-103 body + custom robot-handled two-electrode cable adapters, SI metres.

Flexible cables are animated geometric curves, not a deformable-body solver.
Jaw/terminal alignment is checked, not electrical contact resistance or force.
"""
import math
from pathlib import Path
import numpy as np
from electrode_visuals import _cube, CELL_AXIS_X_OFFSET_M
from measurement_session import LEAD_MAP
from cable_constraints import (ConstrainedCable, scene_envelopes, MODEL_ID,
                               SCENE_LENGTH_M, PAIR_CLEARANCE_RADIUS_M,
                               LENGTH_TOLERANCE_M, LINK_TOLERANCE_M, SCENE_SLACK_SEED_M,
                               WIRE_DIAMETER_M)

INSTRUMENT_XY = (.765, .055)
LEAD_RACK_LAYOUT_VERSION = 'lead_rack_layout_v2'
LEAD_RACK_CENTER_M = (.720, -.240, .004)
LEAD_RACK_SLOT_SPACING_M = .064
LEAD_RACK_BASE_SIZE_M = (LEAD_RACK_SLOT_SPACING_M+.038,.052,.008)
# The first full-cloud run showed the robot could not reach the original 82 mm
# grip height at the rack's rear-right XY position (it settled 20 mm high).
# Raise the clip and its support together into a reachable envelope.
RACK_GRIP_Z_M = .115
CLIP_GRIP_SIZE_M = (.010, .012, .012)
CONTACT_OFFSET_M = (0., .021, -.024)


def target_grip(cell, electrode_center):
    return (cell.xy[0] + CELL_AXIS_X_OFFSET_M, cell.xy[1] - .024,
            electrode_center[2] + .034)


def rack_grip(side):
    return (LEAD_RACK_CENTER_M[0] + (-.5 if side == 'left' else .5)*LEAD_RACK_SLOT_SPACING_M,
            LEAD_RACK_CENTER_M[1], RACK_GRIP_Z_M)


def electrode_terminal_spec(electrode):
    """The conductive extension is part of the electrode, not a fixed fixture.

    A full run starts with electrodes on the storage rack. Parenting the pin
    here keeps it supported during storage, grasping and installation while
    preserving the previously accepted final terminal location.
    """
    return {'path':electrode['root_path']+'/MeasurementTerminal',
            'local_center_m':(0.,-.003,.003),'size_m':(.0012,.0012,.026)}


def add_measurement_asset(stage, root_path, usd_path):
    from pxr import Gf, Usd, UsdGeom
    root = UsdGeom.Xform.Define(stage, root_path + '/Gamry_CHEM_103')
    geometry = stage.DefinePrim(str(root.GetPath()) + '/Geometry', 'Xform')
    if not geometry.GetReferences().AddReference(str(Path(usd_path).resolve())):
        raise RuntimeError('CHEM-103 USD 引用失败')
    def bound():
        return UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render']).ComputeWorldBound(geometry).ComputeAlignedRange()
    raw = bound(); size = raw.GetSize()
    xf = UsdGeom.Xformable(geometry)
    # Source GLB is Y-up. Recover X=90, Y~274.4, Z=190 mm without resizing it.
    if size[1] < size[2]:
        xf.AddRotateXOp(opSuffix='sourceAxis').Set(90.)
    scale = .090 / size[0]
    xf.AddScaleOp(opSuffix='importUnits').Set(Gf.Vec3f(scale, scale, scale))
    bounds = bound(); actual = list(bounds.GetSize())
    if any(abs(actual[i] - d) > .004 for i, d in enumerate((.090, .2744, .190))):
        raise RuntimeError('CHEM-103 轴/单位不符: ' + str(actual))
    feet = [p for p in Usd.PrimRange(geometry) if p.IsA(UsdGeom.Mesh) and '__feet' in str(p.GetPath()).lower()]
    if not feet:
        raise RuntimeError('CHEM-103 未找到支承脚，不能凭机身中心摆放')
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render'])
    foot_z = min(cache.ComputeWorldBound(p).ComputeAlignedRange().GetMin()[2] for p in feet)
    center_x = (bounds.GetMin()[0] + bounds.GetMax()[0]) / 2
    center_y = (bounds.GetMin()[1] + bounds.GetMax()[1]) / 2
    root.AddTranslateOp().Set(Gf.Vec3d(INSTRUMENT_XY[0] - center_x,
                                     INSTRUMENT_XY[1] - center_y, -foot_z))
    connector = [p for p in Usd.PrimRange(geometry) if p.IsA(UsdGeom.Mesh) and 'cell_connector' in str(p.GetPath()).lower()]
    if not connector:
        raise RuntimeError('CHEM-103 电池接口网格缺失')
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render'])
    boxes = [cache.ComputeWorldBound(p).ComputeAlignedRange() for p in connector]
    low = [min(b.GetMin()[i] for b in boxes) for i in range(3)]
    high = [max(b.GetMax()[i] for b in boxes) for i in range(3)]
    port = [(low[0]+high[0])/2, low[1]-.003, (low[2]+high[2])/2]
    _cube(stage, root_path + '/CellCablePlug', tuple(port), (.022, .008, .012), (.16,.18,.20))
    return {'asset_id': 'CHEM-103', 'asset_version': '1.1.0', 'imported_dimensions_m': actual,
            'desktop_contact_z_m': 0., 'body_scale': 1., 'import_unit_correction': scale,
            'cable_port_m': port, 'feet_mesh_count': len(feet),
            'built_in_synthetic_runtime_invoked': False, 'electrical_output_enabled': False}


def _curve(stage, path, color):
    from pxr import Gf, UsdGeom
    curve = UsdGeom.BasisCurves.Define(stage, path)
    curve.CreateTypeAttr('linear'); curve.CreateWrapAttr('nonperiodic')
    curve.CreateWidthsAttr([.0014]); curve.SetWidthsInterpolation('constant')
    curve.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    return curve


def cable_points(start, endpoint, side):
    """CPU geometry helper; runtime always also supplies USD envelopes."""
    return [tuple(p) for p in ConstrainedCable(start, endpoint).update(endpoint)]


def create_measurement_visuals(stage, root_path, asset_usd, cells, electrodes):
    from pxr import Gf, UsdGeom
    instrument = add_measurement_asset(stage, root_path, asset_usd)
    _cube(stage, root_path+'/LeadTray/Base', LEAD_RACK_CENTER_M, LEAD_RACK_BASE_SIZE_M, (.36,.44,.49))
    clips = {}
    for side in ('left','right'):
        cell = cells[side+'_half_cell']
        electrode_center = electrodes[side][side+'_cell_center_m']
        desired = target_grip(cell, electrode_center)
        rp = rack_grip(side)
        parent = root_path+'/MeasurementLead_'+side.title()
        root = UsdGeom.Xform.Define(stage, parent)
        translate = root.AddTranslateOp(); translate.Set(Gf.Vec3d(*rp))
        colors = ((.95,.95,.89),(.80,.08,.06)) if side=='left' else ((.08,.28,.88),(.10,.58,.20))
        _cube(stage,parent+'/RobotGrip',(0.,0.,0.),CLIP_GRIP_SIZE_M,colors[0])
        _cube(stage,parent+'/SecondaryRoleStripe',(0.,-.002,.0062),(.010,.004,.0005),colors[1])
        _cube(stage,parent+'/InsulatedNeck',(0.,.012,-.013),(.004,.018,.026),colors[0])
        jaw_ops=[]
        for name, sign in (('JawLeft',-1),('JawRight',1)):
            cube=_cube(stage,parent+'/'+name,(sign*.002,CONTACT_OFFSET_M[1],CONTACT_OFFSET_M[2]),
                       (.0012,.008,.004),(.73,.65,.44))
            jaw_ops.append((UsdGeom.Xformable(cube).GetOrderedXformOps()[0],sign))
        # Conductive terminal is a project adapter continuing the existing plate
        # through the insulating cap. It does not move the accepted electrode.
        terminal_z=electrode_center[2]+.010
        terminal=electrode_terminal_spec(electrodes[side])
        _cube(stage,terminal['path'],terminal['local_center_m'],
              terminal['size_m'],(.78,.42,.16))
        top=rp[2]-CLIP_GRIP_SIZE_M[2]/2
        _cube(stage,root_path+'/LeadTray/'+side.title()+'Post',
              (rp[0],rp[1]+.012,(.008+top)/2),(.004,.004,top-.008),(.43,.51,.56))
        for name,dx in (('Left',-.004),('Right',.004)):
            _cube(stage,root_path+'/LeadTray/'+side.title()+name+'Fork',
                  (rp[0]+dx,rp[1],top-.001),(.002,.019,.002),(.67,.75,.79))
        cables=[_curve(stage,root_path+'/Cable_'+side.title()+str(i),color) for i,color in enumerate(colors)]
        clips[side]={'translate':translate,'jaw_ops':jaw_ops,'cables':cables,
                     'rack_center_m':rp,'target_center_m':desired,'position':rp,
                     'terminal_contact_m':(desired[0],cell.xy[1]-.003,terminal_z),
                     'source_port_m':instrument['cable_port_m'],'side':side,'connected':False}
    envelopes = scene_envelopes(stage, root_path)
    if not envelopes:
        raise RuntimeError('未取得设备/支架包围体，不能生成受约束引线')
    for side, clip in clips.items():
        rp = clip['rack_center_m']
        clip['cable_solver'] = ConstrainedCable(instrument['cable_port_m'],
                                               (rp[0],rp[1]-.006,rp[2]), envelopes)
        set_lead_center(clips[side],rp)
    return {'instrument':instrument,'clips':clips,
            'metadata':{'instrument':instrument,'lead_map':LEAD_MAP,
                        'tray_desktop_contact_z_m':0.,'clip_adapter':'custom_robot_tab_and_spring_jaws',
                        'flexible_cable_model':MODEL_ID,
                        'cable_constraints': {'model_id':MODEL_ID,
                            'conductor_length_m':SCENE_LENGTH_M,
                            'length_basis':'user_authorized_uncalibrated_scene_assumption',
                            'initial_slack_seed_m':[list(p) for p in SCENE_SLACK_SEED_M],
                            'seed_is_fixed_guide':False,
                            'pair_clearance_radius_m':PAIR_CLEARANCE_RADIUS_M,
                            'length_tolerance_m':LENGTH_TOLERANCE_M,
                            'max_link_tolerance_m':LINK_TOLERANCE_M,
                            'static_envelopes':envelopes,
                            'limitations':['quasi_static_geometric_relaxation',
                                'no_calibrated_tension_friction_or_bending_stiffness',
                                'no_calibrated_cable_self_or_inter_cable_or_robot_contact',
                                'conservative_aabb_contacts_not_exact_mesh_contacts']},
                        'contact_force_and_resistance_validated':False,
                        'conductive_terminals_parented_to_electrode_roots':True,
                        'unused_orange_black':'isolated_at_instrument_cable_adapter',
                        'device_end_cable':'preconnected_before_experiment',
                        'rack_grip_centers_m':{s:list(rack_grip(s)) for s in clips},
                        'rack_layout':{'version':LEAD_RACK_LAYOUT_VERSION,
                            'center_m':list(LEAD_RACK_CENTER_M),'base_size_m':list(LEAD_RACK_BASE_SIZE_M),
                            'slot_spacing_m':LEAD_RACK_SLOT_SPACING_M,
                            'user_requested_right_shift_m':.030,
                            'previous_center_m':[.690,-.240,.004],'previous_slot_spacing_m':.032,
                            'applied_at_scene_initialization':True},
                        'targets_m':{s:list(c['target_center_m']) for s,c in clips.items()}}}


def set_lead_center(clip, position, *, closed=False):
    from pxr import Gf
    clip['translate'].Set(Gf.Vec3d(*position)); clip['position']=tuple(position)
    for op, sign in clip['jaw_ops']:
        op.Set(Gf.Vec3d(sign*(.0012 if closed else .002),CONTACT_OFFSET_M[1],CONTACT_OFFSET_M[2]))
    end=(position[0],position[1]-.006,position[2])
    solver = clip.get('cable_solver')
    if solver is None:
        raise RuntimeError('导线缺少定长求解器，不能回退到随端点拉长的旧曲线')
    base_points = solver.update(end)
    for i, curve in enumerate(clip['cables']):
        offset=(-1 if i==0 else 1)*.00085
        points=[Gf.Vec3f(x+offset,y,z) for x,y,z in base_points]
        curve.CreatePointsAttr(points);curve.CreateCurveVertexCountsAttr([len(points)])


def cable_frame_evidence(clips):
    """Read the actual USD conductor vertices for the synchronized frame."""
    return {side:{'position_m':list(clip['position']),
                  'source_port_m':list(clip['source_port_m']),
                  'conductors_m':[[list(map(float,p)) for p in curve.GetPointsAttr().Get()]
                                  for curve in clip['cables']]}
            for side,clip in clips.items()}


def lead_geometry(clip, observed, cell, electrode_center, liquid_surface_z, other_cell):
    contact=tuple(observed[i]+CONTACT_OFFSET_M[i] for i in range(3))
    error=math.dist(contact,clip['terminal_contact_m'])
    bridge_axis=(cell.xy[0]+.0025,cell.xy[1])
    expected_end=(observed[0],observed[1]-.006,observed[2])
    continuous=True
    if clip.get('cables'):
        for i,curve in enumerate(clip['cables']):
            points=curve.GetPointsAttr().Get()
            offset=(-1 if i==0 else 1)*.00085
            start=(clip['source_port_m'][0]+offset,*clip['source_port_m'][1:])
            end=(expected_end[0]+offset,*expected_end[1:])
            continuous=continuous and bool(points) and math.dist(points[0],start)<1e-6 and math.dist(points[-1],end)<1e-6
    else:
        points=cable_points(clip['source_port_m'],expected_end,clip['side'])
        continuous=points[0]==tuple(clip['source_port_m']) and points[-1]==expected_end
    checks={'terminal_contact':error<=.00025,
            'above_liquid':contact[2]-.002>liquid_surface_z+.005,
            'electrode_supported':abs(electrode_center[2]-(cell.support_z+cell.floor_thickness+.003+.042+.006))<1e-7,
            'bridge_clear':math.dist(contact[:2],bridge_axis)>.004,
            'cable_continuous':continuous,
            'opposite_terminal_clear':abs(observed[0]-(other_cell.xy[0]+CELL_AXIS_X_OFFSET_M))>CLIP_GRIP_SIZE_M[0]+.004}
    return {'checks':checks,'contact_error_m':error,'contact_center_m':list(contact),
            'terminal_center_m':list(clip['terminal_contact_m']),
            'liquid_surface_z_m':liquid_surface_z,
            'opposite_terminal_xy_m':[other_cell.xy[0]+CELL_AXIS_X_OFFSET_M,other_cell.xy[1]-.003]}
