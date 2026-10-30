"""状态账本：只定义液体、容器、枪头及实验状态，不执行操作。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .state_ledger import UnifiedStateLedger


@dataclass
class LiquidState:
    """内部统一使用 L、mol；浓度 M = mol/L，不独立保存浓度。"""

    identifier: str
    volume_l: float = 0.0
    amounts_mol: dict[str, float] = field(default_factory=dict)
    mixed: bool = True
    mixing_uniformity: float = 1.0
    capacity_l: float | None = None
    # Conceptual liquid parcels preserve concentration differences between
    # additions. They are a lightweight mixing model, not geometric CFD cells.
    mixing_components: list[dict] = field(default_factory=list)
    mixing_reference_spread_m: dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        if not math.isfinite(self.volume_l) or self.volume_l < 0:
            raise ValueError(f"{self.identifier}: 液体体积必须有限且非负")
        if self.capacity_l is not None:
            if not math.isfinite(self.capacity_l) or self.capacity_l <= 0:
                raise ValueError(f"{self.identifier}: 容量必须有限且大于零")
            if self.volume_l > self.capacity_l:
                raise ValueError(f"{self.identifier}: 超过容器容量")
        for amount in self.amounts_mol.values():
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"{self.identifier}: 物质的量必须有限且非负")
        if self.volume_l == 0 and any(self.amounts_mol.values()):
            raise ValueError(f"{self.identifier}: 本模型不支持零液量中存在溶质")
        if self.mixing_components:
            component_volume = math.fsum(float(part["volume_l"]) for part in self.mixing_components)
            if not math.isclose(component_volume, self.volume_l, rel_tol=1e-9, abs_tol=1e-15):
                raise ValueError(f"{self.identifier}: 混匀分区体积与容器总量不一致")
            species = set(self.amounts_mol)
            for part in self.mixing_components:
                part_volume = float(part["volume_l"])
                if not math.isfinite(part_volume) or part_volume <= 0:
                    raise ValueError(f"{self.identifier}: 混匀分区体积必须有限且大于零")
                for amount in part.get("amounts_mol", {}).values():
                    if not math.isfinite(float(amount)) or float(amount) < 0:
                        raise ValueError(f"{self.identifier}: 混匀分区物质量必须有限且非负")
                species.update(part.get("amounts_mol", {}))
            for name in species:
                component_amount = math.fsum(
                    float(part.get("amounts_mol", {}).get(name, 0.0))
                    for part in self.mixing_components
                )
                if not math.isclose(
                    component_amount, self.amounts_mol.get(name, 0.0),
                    rel_tol=1e-9, abs_tol=1e-15,
                ):
                    raise ValueError(f"{self.identifier}: 混匀分区物质量与容器总量不一致")

    def concentration_m(self, species: str) -> float | None:
        """未混匀时仍可求总体平均浓度，但不代表局部取样浓度。"""
        if self.volume_l == 0:
            return None
        return self.amounts_mol.get(species, 0.0) / self.volume_l

    def component_concentrations_m(self, species: str) -> list[tuple[float, float]]:
        """Return (component volume, concentration) pairs for local scenarios."""
        if self.volume_l <= 0:
            return []
        components = self.mixing_components or [{
            "volume_l": self.volume_l,
            "amounts_mol": self.amounts_mol,
        }]
        return [
            (float(part["volume_l"]), float(part.get("amounts_mol", {}).get(species, 0.0)) / float(part["volume_l"]))
            for part in components
            if float(part["volume_l"]) > 0
        ]

    def concentration_spread_m(self, species: str) -> float:
        """Volume-weighted standard deviation across conceptual liquid parcels."""
        pairs = self.component_concentrations_m(species)
        if not pairs:
            return 0.0
        total_volume = math.fsum(volume for volume, _ in pairs)
        mean = self.concentration_m(species)
        if mean is None:
            return 0.0
        variance = math.fsum(volume * (concentration - mean) ** 2 for volume, concentration in pairs) / total_volume
        return math.sqrt(max(0.0, variance))

    def concentration_range_m(self, species: str) -> tuple[float | None, float | None]:
        values = [concentration for _, concentration in self.component_concentrations_m(species)]
        if not values:
            mean = self.concentration_m(species)
            return (mean, mean)
        return min(values), max(values)

    def refresh_mixing_status(self) -> None:
        """Derive a continuous mixing score from component concentration spread."""
        species = set(self.amounts_mol) | set(self.mixing_reference_spread_m)
        scores = []
        for name in species:
            reference = float(self.mixing_reference_spread_m.get(name, 0.0))
            spread = self.concentration_spread_m(name)
            if reference <= 1e-15:
                scores.append(1.0 if spread <= 1e-15 else 0.0)
            else:
                scores.append(min(1.0, max(0.0, 1.0 - spread / reference)))
        self.mixing_uniformity = min(scores, default=1.0)
        # This flag now means exactly homogeneous within numerical tolerance;
        # it is not a threshold for whether voltage may be calculated.
        self.mixed = self.mixing_uniformity >= 1.0 - 1e-9


@dataclass
class TipState:
    loaded: bool = False
    liquid: LiquidState = field(default_factory=lambda: LiquidState("tip"))


@dataclass
class ExperimentState:
    containers: dict[str, LiquidState]
    temperature_k: float
    tip: TipState = field(default_factory=TipState)
    waste: LiquidState = field(default_factory=lambda: LiquidState("waste"))
    cell_setup: dict = field(default_factory=dict)
    completed_steps: int = 0
    chemical_model_id: str | None = None
    chemical_model_state: dict = field(default_factory=dict)
    ledger: UnifiedStateLedger = field(default_factory=UnifiedStateLedger)

    def inventory(self) -> dict:
        """守恒范围包含所有容器、枪头和废弃液。"""
        stores = [*self.containers.values(), self.tip.liquid, self.waste]
        totals: dict[str, float] = {}
        for store in stores:
            for species, amount in store.amounts_mol.items():
                totals[species] = totals.get(species, 0.0) + amount
        return {
            "volume_l": math.fsum(store.volume_l for store in stores),
            "amounts_mol": totals,
        }

    def snapshot(self) -> dict:
        data = asdict(self)
        data["state_ledger"] = data.pop("ledger")
        for identifier, store in self.containers.items():
            data["containers"][identifier]["concentrations_m"] = {
                species: store.concentration_m(species)
                for species in store.amounts_mol
            }
        return data


def build_initial_state(config: dict) -> ExperimentState:
    temperature = float(config["temperature_k"])
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("温度必须有限且大于 0 K")
    containers: dict[str, LiquidState] = {}
    for identifier, definition in config["containers"].items():
        volume = float(definition["volume_ml"]) / 1000.0
        concentrations = definition.get("concentrations_m", {})
        for concentration in concentrations.values():
            if not math.isfinite(float(concentration)) or float(concentration) < 0:
                raise ValueError(f"{identifier}: 初始浓度必须有限且非负")
        capacity = definition.get("capacity_ml")
        store = LiquidState(
            identifier=identifier,
            volume_l=volume,
            amounts_mol={key: float(value) * volume for key, value in concentrations.items()},
            mixed=bool(definition.get("mixed", True)),
            capacity_l=None if capacity is None else float(capacity) / 1000.0,
            mixing_components=([{
                "source_id": identifier,
                "volume_l": volume,
                "amounts_mol": {key: float(value) * volume for key, value in concentrations.items()},
            }] if volume > 0 else []),
        )
        store.validate()
        containers[identifier] = store
    if not containers:
        raise ValueError("至少需要一个容器")
    bridge = config.get("cell_setup", {}).get("salt_bridge", {})
    state = ExperimentState(
        containers=containers,
        temperature_k=temperature,
        cell_setup={
            "electrodes_placed": False,
            "left_electrode_placed": False,
            "right_electrode_placed": False,
            "active_tool": "pipette",
            "pipette_parked": False,
            "held_component": None,
            "salt_bridge_connected": False,
            "salt_bridge_type": bridge.get("type"),
        },
    )
    for identifier in containers:
        state.ledger.register_entity(identifier, "container", state_ref=f"/containers/{identifier}")
        contents = f"contents:{identifier}"
        state.ledger.register_entity(contents, "chemical_contents",
                                     state_ref=f"/containers/{identifier}")
        state.ledger.add_initial_relation(contents, "contained_in", identifier)
    state.ledger.register_entity("tip", "pipette_tip", state_ref="/tip")
    state.ledger.register_entity("waste", "waste_container", state_ref="/waste")
    state.ledger.register_entity("contents:tip", "chemical_contents", state_ref="/tip/liquid")
    state.ledger.add_initial_relation("contents:tip", "contained_in", "tip")
    state.ledger.register_entity("contents:waste", "chemical_contents", state_ref="/waste")
    state.ledger.add_initial_relation("contents:waste", "contained_in", "waste")
    state.ledger.register_entity("robot_arm", "device")
    state.ledger.register_entity("robot_gripper", "end_effector")
    return state
