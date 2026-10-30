"""Action-first MP4 composition. Every panel is static, not a fake UI control.

Visual liquid interpolation stays in the scene; the two numeric cell rows use
only the separately supplied, receipt-confirmed ledger (microlitres and mol/L).
"""
from PIL import Image, ImageDraw, ImageOps
from workflow_display import workflow_progress


def draw_object_names(draw, anchors, *, chinese, font, scene_w, scene_h, header):
    """Short names outside object anchors; no unexplained numeric identifiers."""
    names = [('copper_stock', '母液', 'Stock'), ('water', '水源', 'Water'),
             ('left_half_cell', '左池', 'Left'), ('right_half_cell', '右池', 'Right'),
             ('tip', '移液器', 'Pipette'), ('waste', '弃头盒', 'Waste')]
    font = font.font_variant(size=max(14, round(font.size*.8)))
    placed = []
    for key, zh, en in names:
        point = anchors.get(key)
        if not point or not (20 < point[0] < scene_w-40 and 20 < point[1] < scene_h-40):
            continue
        label = zh if chinese else en
        bounds = draw.textbbox((0, 0), label, font=font)
        width, height = bounds[2]-bounds[0]+12, bounds[3]-bounds[1]+10
        ax, ay = point[0], point[1]+header
        preferred_left = key in {'left_half_cell', 'water'}
        for dy in (-height-6, 10, -height-30, 34, -height-58, 62):
            x = ax-width-12 if preferred_left else ax+12
            x = max(4, min(scene_w-width-4, x))
            y = max(header+4, min(header+scene_h-height-4, ay+dy))
            box = (x, y, x+width, y+height)
            if any(not (box[2]+4 < b[0] or b[2]+4 < box[0] or
                        box[3]+4 < b[1] or b[3]+4 < box[1]) for b in placed):
                continue
            draw.line((ax, ay, x+width/2, y+height/2), fill='#86def1', width=1)
            draw.rounded_rectangle(box, radius=4, fill='#193245', outline='#71cad5')
            draw.text((x+6-bounds[0], y+5-bounds[1]), label, font=font, fill='#e0faff')
            placed.append(box)
            break
    return placed


def _text(draw, box, text, font, color, *, right=False):
    """Fit both axes using actual glyph bounds, including CJK ascenders."""
    left, top, right_edge, bottom = box
    fitted = font
    while True:
        bounds = draw.textbbox((0, 0), text, font=fitted)
        if (bounds[2] - bounds[0] <= right_edge - left
                and bounds[3] - bounds[1] <= bottom - top):
            break
        if fitted.size <= 10:
            raise ValueError(f'Video text does not fit: {text}')
        fitted = fitted.font_variant(size=fitted.size - 1)
    x = right_edge - (bounds[2] - bounds[0]) if right else left
    draw.text((x - bounds[0], top - bounds[1]), text, font=fitted, fill=color)


def compose(p, scene, *, run_id, phase, sim_time, volumes,
            stock_concentration, completed, planned_ul, anchors,
            tip_attached=None, water_concentration=0.0, detail_scene=None,
            left_liquid_height_mm=None, command=None, plan=(),
            completed_steps=0, confirmed_volumes=None, cell_concentrations=None, voltage=None):
    w, h = p.width, p.height
    header, footer, scene_w = 90, 120, w * 3 // 4
    scene_h = h - header - footer
    scale = min(h / 1440, (w - scene_w) / 480)
    px = lambda value: max(1, round(value * scale))
    image = Image.new('RGB', (w, h), '#101e2c')
    image.paste(scene.resize((scene_w, scene_h)), (0, header))
    d = ImageDraw.Draw(image)
    zh = p.chinese
    active = command or completed or {}
    parts = phase.split('/')
    action = active.get('action') or parts[0].replace('_water', '')
    water = active.get('material_id') == 'water' or 'water' in parts[0]
    side = '右侧' if active.get('target') == 'right_half_cell' else '左侧'
    material = '稀释水' if water else '母液'
    pipette_name = active.get('pipette_display_name') or ('P200' if active.get('pipette_id') == 'gilson_pipetman_p200' else 'P1000')
    labels = {'pick_pipette': f'从专用支架取用{pipette_name}',
              'load_tip': f'安装{material}用新枪头', 'aspirate': f'吸取{material}',
              'dispense': f'向{side}加入{material}', 'discard_tip': f'弃置{material}枪头',
              'mix': '左池磁力混匀', 'mix_pair': '双池同步磁力混匀',
              'park_pipette': f'将{pipette_name}放回专用支架', 'pick_electrode': f'抓取{side}铜电极',
              'place_electrode': f'安装{side}铜电极', 'initializing': '初始化场景',
              'pick_salt_bridge': '从支架夹取盐桥', 'connect_salt_bridge': '连接左右池盐桥',
              'pick_lead': f'夹取{side}测量引线', 'connect_lead': f'连接{side}电极引线',
              'measure_voltage': '读取虚拟开路电压'}
    subs = {'dock': '对准枪头', 'position': '定位下降', 'volume_transfer': '液量转移',
            'settle': '停留确认', 'retract': '抬起撤离', 'confirmed': '本步回执已确认',
            'fall': '枪头落入废弃盒', 'robot_clear': '机械臂撤离搅拌区',
            'rotation': '搅拌子与液面旋动', 'approach': '接近操作位置',
            'support': '对准承托位', 'align': '确认夹持位置', 'lift': '提起并移动',
            'align_and_insert': ('双端对准池口并插入' if action == 'connect_salt_bridge'
                                 else f'沿{side}定位插入'), 'release_and_clear': '释放并撤离',
            'open_gripper': '打开夹爪',
            'close_gripper': ('夹紧盐桥夹持帽' if action == 'pick_salt_bridge'
                              else '夹紧引线操作片' if action == 'pick_lead' else '夹紧夹持部位'),
            'horizontal_pose_probe': '高位确认左右夹持姿态',
            'repair_high_route': '沿高位通道避架转场',
            'align_terminal': '对准电极接线端子', 'clamp_terminal': '夹紧端子并保持接触',
            'release': '释放操作片，保留端子连接', 'acquire': '调用现有引擎 · 理想虚拟仪表'}
    ended = phase in {'process_completed', 'diagnostic_segment_complete'}
    label = labels.get(action, action) if zh else action.replace('_', ' ').capitalize()
    if ended:
        label = ('局部检查片段结束' if phase == 'diagnostic_segment_complete' else '本段动作已完成') if zh else 'Recorded segment finished'
    if parts[0] == 'initializing':
        label = '初始化场景' if zh else 'Initializing scene'
    subtitle = (subs.get(parts[1], parts[1]) if zh else parts[1].replace('_', ' ')) if len(parts) > 1 else ''
    if ended:
        bridge_done = any(step.get('action') == 'connect_salt_bridge' for step in plan[:completed_steps])
        subtitle = (('接线与开路电压虚拟测量已完成' if (voltage or {}).get('valid') else
                     '盐桥已连接 · 接线与测量待执行' if bridge_done else '盐桥与接线测量待后续执行')
                    if zh else 'Wiring and measurement remain pending')
    total = len(plan)
    step_index = next((i + 1 for i, step in enumerate(plan)
                       if step.get('step_id') == active.get('step_id')), None)
    step_text = (f'步骤 {step_index or completed_steps} / {total}' if zh
                 else f'Step {step_index or completed_steps} / {total}') if total else ('步骤待载入' if zh else 'Plan unavailable')

    _text(d, (24, 13, w - 320, 48), '自动化电化学实验' if zh else 'Automated electrochemistry', p.title, 'white')
    _text(d, (24, 56, w - 320, 80), f'{run_id[:12]}  ·  {sim_time:.2f} s', p.small, '#b4c4d3')
    _text(d, (w - 300, 20, w - 24, 52), '实验台全景' if zh else 'Lab overview', p.small, '#86def1', right=True)
    x, edge = scene_w + px(20), w - px(16)
    width = edge - x
    y = header + px(18)
    _text(d, (x, y, x + width * .38, y + px(25)), '当前动作' if zh else 'Current action', p.small, '#b4c4d3')
    _text(d, (x + width * .38, y, edge, y + px(25)), step_text, p.small, '#86def1', right=True)
    y += px(42)
    _text(d, (x, y, edge, y + px(36)), label, p.font, 'white')
    y += px(49)
    _text(d, (x, y, edge, y + px(25)), subtitle, p.small, '#86def1')
    y += px(40)
    track_h = px(5)
    d.rounded_rectangle((x, y, edge, y + track_h), radius=track_h, fill='#294154')
    if total and completed_steps:
        progress_w = width * min(completed_steps, total) / total
        d.rectangle((x, y, x + progress_w, y + track_h), fill='#71d8bd')

    y += px(28)
    _text(d, (x, y, edge, y + px(27)), '整体实验流程' if zh else 'Overall workflow', p.small, 'white')
    y += px(40)
    # A completed receipt can finish a stage; it cannot start the next stage.
    current_id = active.get('step_id') if not ended and parts[0] != 'initializing' else None
    stages = workflow_progress(plan, completed_steps, current_id,
                               None if ended or parts[0] == 'initializing' else action)
    for index, row in enumerate(stages):
        state = row['state']
        color = '#89e7bd' if state == 'done' else '#86def1' if state == 'current' else '#9aafbf'
        if state == 'current':
            d.rounded_rectangle((x - px(6), y - px(5), edge, y + px(30)), radius=px(6), fill='#1c3447')
        marker = '✓' if state == 'done' else str(index + 1)
        status = {'done': '完成', 'current': '进行中', 'pending': '待执行', 'future': '后续'}[state] if zh else state
        if state == 'done':
            # The CJK font used by the cloud renderer lacks the checkmark glyph.
            d.line([(x+px(3),y+px(13)),(x+px(10),y+px(21)),(x+px(24),y+px(3))],
                   fill=color,width=px(3))
        else:
            _text(d, (x, y, x + px(26), y + px(25)), marker, p.small, color)
        _text(d, (x + px(35), y, edge - px(82), y + px(25)), row['zh'] if zh else row['en'], p.small, color)
        _text(d, (edge - px(80), y, edge - px(6), y + px(25)), status, p.small, color, right=True)
        y += px(39)

    y += px(18)
    is_mix = action in {'mix', 'mix_pair'}
    _text(d, (x, y, edge, y + px(30)), ('同步俯视特写' if is_mix else '同步动作特写') if zh else 'Synchronized detail', p.small, 'white')
    y += px(42)
    # Reserve room for two compact confirmed-state rows before sizing detail.
    has_measurement = any(step.get('action') == 'measure_voltage' for step in plan)
    show_measurement = has_measurement and not is_mix
    inset_h = max(px(140), min(width, h - footer - px(210 + (135 if show_measurement else 0)) - y))
    if detail_scene is None:
        focus = ('tip' if action == 'load_tip' else 'waste' if action == 'discard_tip'
                 else active.get('target') if action in {'dispense', 'place_electrode'}
                 else active.get('source') or 'left_half_cell')
        points = [point for point in (anchors.get('tip'), anchors.get(focus)) if point]
        if points:
            cx = sum(point[0] for point in points) / len(points)
            cy = sum(point[1] for point in points) / len(points)
            crop_w = min(scene.width, max(220, abs(points[0][0] - points[-1][0]) + 140))
            crop_h = min(scene.height, max(220, abs(points[0][1] - points[-1][1]) + 160))
            left = max(0, min(scene.width - crop_w, cx - crop_w / 2))
            top = max(0, min(scene.height - crop_h, cy - crop_h / 2))
            detail_scene = scene.crop((int(left), int(top), int(left + crop_w), int(top + crop_h)))
    if detail_scene is not None:
        image.paste(ImageOps.pad(detail_scene.convert('RGB'), (width, inset_h), color='#1c3447'), (x, y))
    else:
        d.rectangle((x, y, edge, y + inset_h), fill='#1c3447')
        _text(d, (x + px(15), y + px(20), edge - px(15), y + px(50)), '等待同步画面' if zh else 'Awaiting detail', p.small, '#b4c4d3')
    d.rectangle((x, y, edge, y + inset_h), outline='#71cad5', width=2)
    y += inset_h + px(22)
    _text(d, (x, y, edge, y + px(25)), '溶液状态 · 已确认量' if zh else 'Confirmed solution state', p.small, '#b4c4d3')
    y += px(38)
    for cell_id, name in (('left_half_cell', 'L 左池'), ('right_half_cell', 'R 右池')):
        value = (confirmed_volumes or {}).get(cell_id)
        concentration = (cell_concentrations or {}).get(cell_id)
        amount = '—' if value is None else f'{value / 1000:.4f} mL'
        concentration_text = '— M' if concentration is None else f'{concentration:.4g} M'
        _text(d, (x, y, x + px(95), y + px(27)), name if zh else cell_id.split('_')[0].capitalize(), p.small, 'white')
        _text(d, (x + px(95), y, edge, y + px(27)), f'{amount}  ·  {concentration_text}', p.small, '#86def1', right=True)
        y += px(42)

    if show_measurement:
        y += px(8)
        voltage_title = ('虚拟电压 · 液接参考估算' if (voltage or {}).get('liquid_junction_reference_estimate')
                         else '虚拟开路电压 · 右减左')
        _text(d,(x,y,edge,y+px(23)),voltage_title if zh else 'Virtual OCP · right minus left',p.small,'white')
        y += px(31)
        for label,key in (('无操作误差基准','baseline_theoretical_mv'),('本次状态预测','predicted_cell_mv'),('理想虚拟读数','virtual_meter_mv')):
            v = (voltage or {}).get(key)
            text = '—' if v is None else f'{v:+.3f} mV'
            _text(d,(x,y,x+width*.53,y+px(22)),label,p.small,'#b4c4d3')
            _text(d,(x+width*.53,y,edge,y+px(22)),text,p.small,'#86def1',right=True)
            y += px(28)

    draw_object_names(d, anchors, chinese=zh, font=p.small,
                      scene_w=scene_w, scene_h=scene_h, header=header)

    from video_presentation import completed_message
    y = h - footer
    d.line((0, y, w, y), fill='#294154', width=1)
    plan_text = ('本动作计划量：' if zh else 'Planned volume: ') + (f'{planned_ul / 1000:.4f} mL' if planned_ul is not None else '—')
    _text(d, (24, y + 9, w - 24, y + 33), plan_text, p.small, '#b4c4d3')
    receipt = ('最近已确认：' if zh else 'Last confirmed: ') + completed_message(completed, zh)
    _text(d, (24, y + 45, w - 24, y + 78), receipt, p.font, '#89e7bd')
    note = ('仅母液来源液位放大；接液池按实际量展示 · 动作为仿真适配 · 数值明细见网页' if zh
            else 'Only stock fill is enlarged; receiving volume follows ledger. Simulation proxies; details on webpage.')
    _text(d, (24, y + 91, w - 24, h - 7), note, p.small, '#b4c4d3')
    return image
