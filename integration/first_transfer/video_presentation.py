"""Readable video annotations; completed quantities come only from receipts."""
from pathlib import Path
import math
from PIL import Image, ImageDraw, ImageFont


def load_font(size, explicit=None):
    candidates = [explicit] if explicit else []
    candidates += [str(Path(__file__).with_name('video_font.ttc')),
                   'C:/Windows/Fonts/msyh.ttc',
                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size), True
    return ImageFont.truetype('DejaVuSans.ttf', size), False


def project_point(point, camera_position, camera_target, width, height, focal, aperture):
    """Project a fixed Z-up look-at camera; points behind it are not labelled."""
    import numpy as np
    eye, target, p = map(lambda x: np.array(x, dtype=float),
                         (camera_position, camera_target, point))
    forward = target - eye
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0, 0, 1])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    v = p - eye
    depth = float(np.dot(v, forward))
    if depth <= 0:
        return None
    pixels = width * focal / aperture
    return (width / 2 + pixels * float(np.dot(v, right)) / depth,
            height / 2 - pixels * float(np.dot(v, up)) / depth)


def completed_message(command, chinese=True):
    if not command:
        return '等待执行回执；尚无已确认动作' if chinese else 'Awaiting receipt: no confirmed action'
    action = command['action']
    water = command.get('material_id') == 'water'
    if action in {'pick_lead','connect_lead'}:
        side = '右侧' if command.get('target') == 'right_half_cell' else '左侧'
        return ((f'{side}引线已夹取' if action == 'pick_lead' else f'{side}引线已夹紧电极端子，机械臂已释放')
                if chinese else 'Measurement lead picked' if action=='pick_lead' else 'Lead connected; robot released')
    if action == 'measure_voltage':
        value = command['voltage']['virtual_meter_mv']
        return (f'开路电压虚拟读数：{value:+.3f} mV（右减左）；实验记录已保存'
                if chinese else f'Virtual OCP: {value:+.3f} mV; record saved')
    if action == 'load_tip':
        identity = command.get('pipette_display_name')
        if not identity:
            model = command.get('pipette_id')
            tip = command.get('tip_id')
            model_label = 'P200' if model == 'gilson_pipetman_p200' else 'P1000' if model == 'gilson_pipetman_p1000' else None
            tip_label = 'D200' if tip == 'PIPETMAN_DIAMOND_D200' else 'D1200' if tip == 'PIPETMAN_DIAMOND_D1200' else None
            identity = f'{model_label} / {tip_label}' if model_label and tip_label else None
        purpose = '稀释水' if water else '母液'
        if chinese:
            suffix = f' · {identity}（参考型号）' if identity else ''
            return f'新枪头安装完成：用于{purpose}转移{suffix}'
        suffix = f' · {identity} (reference model)' if identity else ''
        return f'Fresh tip attached for {"water" if water else "stock"}{suffix}'
    if action == 'mix':
        efficiency = float(command.get('mixing_efficiency', 0.0))
        return (f'左侧溶液混匀完成：现有混匀误差模型效率 {efficiency*100:.1f}% · 液量不变'
                if chinese else f'Left solution mixing confirmed: model efficiency {efficiency*100:.1f}%; volume unchanged')
    if action == 'mix_pair':
        efficiencies = command.get('mixing_efficiencies', {})
        left = float(efficiencies.get('left_half_cell', 0.0))
        right = float(efficiencies.get('right_half_cell', 0.0))
        return (f'左右溶液同步混匀完成：左 {left*100:.1f}% · 右 {right*100:.1f}% · 液量不变'
                if chinese else f'Both solutions mixed simultaneously: left {left*100:.1f}%; right {right*100:.1f}%; volume unchanged')
    if action == 'park_pipette':
        name = command.get('pipette_display_name', '移液器')
        return (f'{name} 已放回对应支架，夹爪已释放' if chinese
                else f'{name} parked in its dedicated stand; gripper released')
    if action == 'pick_pipette':
        name = command.get('pipette_display_name', '移液器')
        return (f'已从专用支架取用 {name}；运动学夹持示意' if chinese
                else f'{name} retrieved from its dedicated stand; kinematic grasp proxy')
    if action == 'pick_salt_bridge':
        return '盐桥已从支架取出，夹持帽已夹紧' if chinese else 'Prefilled bridge picked from rack'
    if action == 'connect_salt_bridge':
        return '盐桥已由支架承托，两端进入左右溶液；接线与测量待执行' if chinese else 'Bridge seated; both ends immersed; wiring and measurement pending'
    if action == 'pick_electrode':
        side = '右侧' if command.get('target') == 'right_half_cell' else '左侧'
        return (f'已从电极架抓取{side}铜电极组件；夹持为运动学示意' if chinese
                else 'Copper electrode picked from rack; kinematic grasp proxy')
    if action == 'place_electrode':
        side = '右侧' if command.get('target') == 'right_half_cell' else '左侧'
        if command.get('target') == 'right_half_cell':
            return (f'{side}铜电极已就位：由支撑架承托；盐桥仍待连接' if chinese
                    else 'Right copper electrode seated on support; salt bridge remains pending')
        return (f'{side}铜电极已就位：由支撑架承托；右侧电极与盐桥仍待安装' if chinese
                else 'Left copper electrode seated on support; right electrode and salt bridge remain pending')
    amount = float(command['actual_volume_ul']) / 1000
    if not math.isfinite(amount) or amount < 0:
        raise ValueError('Invalid confirmed quantity')
    if action == 'discard_tip':
        material = '水' if water else '母液'
        return (f'弃头完成：枪头已入废弃盒；{material}残液 {amount:.6f} mL 计入废弃物' if chinese
                else f'Tip discarded; existing residual {amount:.6f} mL moved to waste')
    if action == 'aspirate':
        source = '纯水' if water else '母液'
        return (f'吸液完成：从{source}源吸取 {amount:.6f} mL（模型执行量）' if chinese
                else f'Aspiration confirmed: {amount:.6f} mL from {"water" if water else "stock"} (model amount)')
    material = '稀释水' if water else '母液'
    target_name = command.get('target') or 'left_half_cell'
    side = '右侧' if target_name == 'right_half_cell' else '左侧'
    return (f'分液完成：向{side}容器注入 {amount:.6f} mL {material}（模型执行量）' if chinese
            else f'Dispense confirmed: {amount:.6f} mL into {"right" if target_name == "right_half_cell" else "left"} vessel (model amount)')


class Presentation:
    def __init__(self, width, height, font_path=None):
        self.width, self.height = width, height
        self.font, self.chinese = load_font(min(round(height * .027), round(width * .017)), font_path)
        self.small, _ = load_font(min(round(height * .021), round(width * .013)), font_path)
        self.title, _ = load_font(min(round(height * .032), round(width * .020)), font_path)

    def draw(self, image, *, run_id, phase, sim_time, volumes, stock_concentration,
             completed, planned_ul, anchors, view):
        d = ImageDraw.Draw(image)
        w, h = self.width, self.height
        zh = self.chinese
        phases = {'pick_pipette': '取用移液器', 'park_pipette': '放回移液器',
                  'load_tip': '装枪头', 'aspirate': '吸取母液', 'dispense': '注入母液',
                  'mix': '左侧溶液磁力混匀', 'mix_pair': '双池同步磁力混匀',
                  'process_completed': '双池配液与混匀完成'}
        parts = phase.split('/')
        sub = {'dock': '对准枪头', 'retract': '抬起撤离', 'position': '定位下降',
               'volume_transfer': '液量转移', 'settle': '停留确认', 'confirmed': '回执已确认',
               'robot_clear': '机械臂撤离搅拌区', 'rotation': '搅拌子转动 · 液面旋动（动画）'}
        label = phases.get(parts[0], parts[0]) if zh else phase
        active_command = completed or {}
        if parts[0] == 'mix' and active_command.get('action') == 'mix_pair':
            label = '双池同步磁力混匀' if zh else 'Simultaneous magnetic mixing'
        if zh and len(parts) > 1:
            label += ' · ' + sub.get(parts[1], parts[1])
        d.rectangle((0, 0, w, 112), fill='#101e2c')
        d.text((26, 12), '自动化电化学实验 · 母液转移' if zh else 'Automated electrochemistry | Stock transfer', font=self.title, fill='white')
        d.text((26, 63), f'{label}  |  {sim_time:.2f} s  |  {run_id[:12]}', font=self.font, fill='#86def1')
        d.text((w - 230, 18), ('分液近景' if view == 'close' else '实验台全景') if zh else view.upper(), font=self.font, fill='white')
        names = {'copper_stock': (f'CuSO4 母液 · Cu2+ {stock_concentration:g} M' if zh else f'CuSO4 stock | Cu2+ {stock_concentration:g} M'),
                 'left_half_cell': '左侧接液池' if zh else 'Left receiving cell',
                 'right_half_cell': '右侧接液池' if zh else 'Right receiving cell',
                 'tip': '移液枪头' if zh else 'Pipette tip'}
        for name, point in anchors.items():
            if point is None or not (0 < point[0] < w and 112 < point[1] < h - 150):
                continue
            lines = [names[name], ('液量示意 ' if zh else 'Visual volume ') + f'{volumes[name] / 1000:.6f} mL']
            box_w = min(w - 52, max(d.textbbox((0, 0), line, font=self.small)[2] for line in lines) + 30)
            x = max(18, min(w - box_w - 18, point[0] + (30 if name != 'left_half_cell' else -box_w - 30)))
            y = max(125, min(h - 238, point[1] - (110 if name == 'tip' else 20)))
            d.line((point[0], point[1], x + (0 if x > point[0] else box_w), y + 32), fill='#89e7ed', width=2)
            d.ellipse((point[0]-4, point[1]-4, point[0]+4, point[1]+4), fill='#89e7ed')
            d.rounded_rectangle((x, y, x + box_w, y + 72), radius=10, fill='#152737', outline='#89e7ed', width=2)
            for i, line in enumerate(lines):
                d.text((x + 12, y + 7 + i * 30), line, font=self.small, fill='white')
        d.rectangle((0, h - 146, w, h), fill='#101e2c')
        plan = '--' if planned_ul is None else f'{planned_ul / 1000:.6f} mL'
        d.text((26, h - 136), ('本动作计划量：' if zh else 'Current planned volume: ') + plan, font=self.small, fill='#b4c4d3')
        d.text((26, h - 94), completed_message(completed, zh), font=self.font, fill='#89e7bd')
        d.text((26, h - 44), '简化器具与液体示意 · 非真实移液/流体精度验证 · mL 显示位数不代表精度' if zh else 'Semantic tool/fluid proxy; not physical validation. Decimal places do not imply accuracy.', font=self.small, fill='#b4c4d3')
        return image

    def compose_overview(self, scene, *, run_id, phase, sim_time, volumes,
                         stock_concentration, completed, planned_ul, anchors, tip_attached=None,
                         water_concentration=0.0, detail_scene=None,
                         left_liquid_height_mm=None, command=None, plan=(),
                         completed_steps=0, confirmed_volumes=None, cell_concentrations=None, voltage=None):
        """Compose the action-first sidebar from this run's real plan/ledger."""
        from action_first_panel import compose
        return compose(self, scene, run_id=run_id, phase=phase, sim_time=sim_time,
                       volumes=volumes, stock_concentration=stock_concentration,
                       completed=completed, planned_ul=planned_ul, anchors=anchors,
                       tip_attached=tip_attached, water_concentration=water_concentration,
                       detail_scene=detail_scene, left_liquid_height_mm=left_liquid_height_mm,
                       command=command, plan=plan, completed_steps=completed_steps,
                       confirmed_volumes=confirmed_volumes,
                       cell_concentrations=cell_concentrations, voltage=voltage)
