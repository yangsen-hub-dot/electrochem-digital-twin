from pathlib import Path
import sys
import tempfile
import json
import copy
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from process_validation.cable_checks import check_cables
from process_validation.report import Report
from first_transfer.cable_constraints import ConstrainedCable


class CableAuditTests(unittest.TestCase):
    def make_case(self):
        source=[.765,-.085,.0724];clip=[.674,-.240,.115]
        cable=ConstrainedCable(source,[clip[0],clip[1]-.006,clip[2]])
        points=cable.update([clip[0],clip[1]-.006,clip[2]])
        wires=[(points+[offset,0,0]).tolist() for offset in (-.00085,.00085)]
        far={'path':'far','lower_m':[1,1,1],'upper_m':[2,2,2]}
        model={'model_id':'fixed_length_gravity_relaxation_v1','conductor_length_m':.65,
               'length_tolerance_m':.0005,'max_link_tolerance_m':.00005,'static_envelopes':[far]}
        manifest={'fps':12.,'frames':[{'lead_centers_m':{'left':clip,'right':clip}}],
                  'presentation':{'measurement':{'instrument':{'cable_port_m':source},'cable_constraints':model}}}
        sample={'frame_index':0,'measurement_cables':{side:{'conductors_m':copy.deepcopy(wires)} for side in ('left','right')}}
        return manifest,sample

    def audit(self,manifest,sample):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'trace.jsonl').write_text(json.dumps(sample)+'\n',encoding='utf8')
            report=Report('a'*32,{'policy_id':'synthetic_cable_fault_injection','threshold_basis':{}})
            check_cables(root,manifest,report)
            return report.finish()

    def test_valid_geometry_passes_with_mechanics_still_uncovered(self):
        result=self.audit(*self.make_case())
        self.assertEqual(result['counts']['fail'],0)
        self.assertEqual(result['counts']['pass'],1)
        self.assertEqual(result['counts']['uncovered'],1)

    def test_visible_curve_stretch_is_caught_even_when_endpoints_stay_attached(self):
        m,s=self.make_case();s['measurement_cables']['left']['conductors_m'][0][30][2]+=.05
        result=self.audit(m,s)
        self.assertTrue(any(i['code']=='cable_length_or_attachment' for i in result['issues']))

    def test_thin_equipment_penetration_between_vertices_is_caught(self):
        m,s=self.make_case();p=np.array(s['measurement_cables']['left']['conductors_m'][0])
        center=(p[25]+p[26])/2
        m['presentation']['measurement']['cable_constraints']['static_envelopes']=[
            {'path':'synthetic_thin_wall','lower_m':(center-.0001).tolist(),'upper_m':(center+.0001).tolist()}]
        result=self.audit(m,s)
        self.assertTrue(any(i['code']=='cable_envelope_penetration' for i in result['issues']))

    def test_missing_frame_evidence_fails_instead_of_passing(self):
        m,s=self.make_case();del s['measurement_cables']['right']
        self.assertGreater(self.audit(m,s)['counts']['fail'],0)

    def test_rendered_conductor_self_crossing_fails(self):
        m,s=self.make_case()
        m['presentation']['measurement']['cable_constraints']['model_id']='fixed_length_gravity_relaxation_v3'
        points=s['measurement_cables']['left']['conductors_m'][0]
        points[20]=[.70,-.20,.10];points[21]=[.72,-.20,.10]
        points[22]=[.70,-.20,.10];points[23]=[.72,-.20,.10]
        result=self.audit(m,s)
        self.assertTrue(any(i['code']=='cable_self_contact' for i in result['issues']))


if __name__=='__main__':unittest.main()
