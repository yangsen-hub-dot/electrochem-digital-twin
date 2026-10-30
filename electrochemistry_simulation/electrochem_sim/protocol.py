"""步骤清单：读入并校验操作，体积从 μL 转为内部 L。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    step_id: str
    kind: str
    source: str | None = None
    target: str | None = None
    volume_l: float | None = None
    component: str | None = None
    targets: tuple[str, ...] = ()
    pipette_id: str | None = None
    duration_s: float | None = None


def build_protocol(config: dict) -> list[Action]:
    allowed = {"load_tip", "aspirate", "dispense", "mix", "mix_pair", "discard_tip",
               "park_pipette", "pick_pipette", "pick_electrode", "place_electrode",
               "place_electrodes", "pick_salt_bridge", "connect_salt_bridge",
               "pick_lead", "connect_lead", "measure_voltage"}
    actions: list[Action] = []
    identifiers: set[str] = set()
    for definition in config["protocol"]:
        identifier = str(definition["step_id"])
        kind = definition["action"]
        if not identifier or identifier in identifiers:
            raise ValueError("步骤编号必须非空且唯一")
        if kind not in allowed:
            raise ValueError(f"不支持的动作: {kind}")
        source, target = definition.get("source"), definition.get("target")
        raw_volume = definition.get("volume_ul")
        volume = None if raw_volume is None else float(raw_volume) / 1_000_000.0
        if volume is not None and (not math.isfinite(volume) or volume <= 0):
            raise ValueError(f"{identifier}: 操作体积必须有限且大于零")
        if kind == "aspirate" and (source is None or volume is None):
            raise ValueError(f"{identifier}: 吸液需要来源和体积")
        raw_targets = definition.get("targets", ())
        targets = tuple(raw_targets) if isinstance(raw_targets, (list, tuple)) else ()
        if kind in {"dispense", "mix"} and target is None:
            raise ValueError(f"{identifier}: 操作需要目标容器")
        if kind == "mix_pair":
            if len(targets) != 2 or targets[0] == targets[1]:
                raise ValueError(f"{identifier}: 双池混匀必须指定两个不同容器")
            target = targets[0]
        if kind == "pick_electrode" and (source is None or target is None):
            raise ValueError(f"{identifier}: 抓取电极需要来源支架和目标位置")
        if kind == "place_electrode" and target is None:
            raise ValueError(f"{identifier}: 安装电极需要目标侧")
        if kind == "pick_salt_bridge" and (source != "salt_bridge_rack" or target != "salt_bridge_holder"):
            raise ValueError(f"{identifier}: 盐桥须从盐桥架送往双池承托位")
        if kind in {"pick_salt_bridge", "connect_salt_bridge"} and targets and set(targets) != {"left_half_cell", "right_half_cell"}:
            raise ValueError(f"{identifier}: 盐桥必须连接左右两个小池")
        if kind in {'pick_lead', 'connect_lead'} and target not in {'left_half_cell', 'right_half_cell'}:
            raise ValueError(f'{identifier}: 引线须连接左右小池的电极端子')
        if kind == 'pick_lead' and source != 'measurement_lead_rack':
            raise ValueError(f'{identifier}: 须从引线架取件')
        if kind == 'measure_voltage' and target != 'virtual_potentiostat':
            raise ValueError(f'{identifier}: 测量仪器目标无效')
        external_ids = {"electrode_rack", "pipette_stand", "salt_bridge_rack", "salt_bridge_holder",
                        'measurement_lead_rack', 'virtual_potentiostat'}
        for container in (source, target, *targets):
            if (container is not None and container not in config["containers"]
                    and not (kind in {"pick_electrode", "place_electrode", "park_pipette", "pick_pipette", "pick_salt_bridge", "connect_salt_bridge", 'pick_lead', 'connect_lead', 'measure_voltage'}
                             and (container in external_ids or
                                  (kind in {"pick_pipette", "park_pipette"}
                                   and str(container).startswith("pipette_stand_"))))):
                raise ValueError(f"{identifier}: 不存在的容器 {container}")
        if kind not in {"aspirate", "dispense"} and volume is not None:
            raise ValueError(f"{identifier}: 此动作不接受体积参数")
        pipette_id = definition.get("pipette_id")
        if pipette_id is not None and (not isinstance(pipette_id, str) or not pipette_id.strip()):
            raise ValueError(f"{identifier}: 移液器编号必须是非空字符串")
        raw_duration = definition.get("duration_s")
        duration = None if raw_duration is None else float(raw_duration)
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError(f"{identifier}: 持续时间必须是有限正数")
        actions.append(Action(identifier, kind, source, target, volume,
                              definition.get("component"), targets, pipette_id, duration))
        identifiers.add(identifier)
    if not actions:
        raise ValueError("实验步骤不能为空")
    return actions
