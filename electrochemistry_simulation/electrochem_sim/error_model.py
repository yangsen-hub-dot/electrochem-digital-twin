"""操作误差：产生模拟执行量，不更新状态，也不给最终电压加噪声。"""

from __future__ import annotations

import math
import random
from statistics import NormalDist
from dataclasses import dataclass

from .protocol import Action
from .pipette_reference_model import MODEL_ID as CONTINUOUS_PIPETTE_MODEL_ID, reference_at_volume


@dataclass(frozen=True)
class RealizedAction:
    command: Action
    volume_l: float | None
    systematic_offset_l: float = 0.0
    random_offset_l: float = 0.0
    tip_residual_l: float = 0.0
    tip_residual_fraction: float = 0.0
    mixing_efficiency: float = 1.0
    error_status: str = "not_applicable"
    pipette_id: str | None = None
    pipette_nominal_range_ul: tuple[float, float] | None = None
    pipette_evidence_status: str | None = None
    pipette_systematic_limit_ul: float | None = None
    pipette_repeatability_sd_ul: float | None = None


class ErrorModel:
    """Apply mechanism-specific operation errors to protocol actions.

    Systematic pipetting bias is shared within a run and sampled from its
    documented bounds; repeatability is sampled per aspiration as a zero-mean
    Gaussian approximation using the selected CV. Tip residual volume and
    mixing efficiency use separate, factor-specific truncated-Gaussian priors
    so their draws remain within physical/scenario bounds. The sampled
    parameters are shared within one run; residual liquid still affects mass
    balance, and mixing efficiency still affects homogenization state. When a
    single action mixes two cells simultaneously, one shared latent draw and
    one independent local draw per cell produce correlated, non-identical
    efficiencies from the same selected prior.
    """

    def __init__(self, parameters: dict | None = None, rng: random.Random | None = None) -> None:
        parameters = parameters or {}
        rng = rng or random.Random(0)
        self.enabled = bool(parameters.get("enabled", False))
        factors = parameters.get("factors", {})
        pipetting_selection = factors.get("pipetting", {})
        carryover_selection = factors.get("tip_carryover", {})
        mixing_selection = factors.get("mixing", {})
        pipetting = dict(pipetting_selection.get("parameters", parameters.get("pipetting", {})))
        carryover = dict(carryover_selection.get("parameters", parameters.get("tip_carryover", {})))
        mixing = dict(mixing_selection.get("parameters", parameters.get("mixing", {})))
        if "pipetting" in parameters:
            pipetting = dict(parameters["pipetting"])
        if "tip_carryover" in parameters:
            carryover = dict(parameters["tip_carryover"])
        if "mixing" in parameters:
            mixing = dict(parameters["mixing"])

        # Legacy numeric keys remain accepted for older configs/tests.
        for legacy_key in ("proportional_bias", "absolute_bias_ul", "repeatability_sd_ul", "source_proportional_bias"):
            if legacy_key in parameters:
                pipetting[legacy_key] = parameters[legacy_key]
        legacy_pipetting_override = any(
            key in parameters
            for key in ("proportional_bias", "absolute_bias_ul", "repeatability_sd_ul", "source_proportional_bias")
        )
        if legacy_pipetting_override:
            pipetting.pop("systematic_relative_bias_range", None)
            pipetting.pop("repeatability_cv_range", None)
            pipetting.pop("applicable_volume_ul_range", None)
            pipetting.pop("volume_profiles", None)
            pipetting.setdefault("systematic_relative_bias", 0.0)
            # Older configurations only described pipetting. Do not silently
            # add the new carryover/mixing scenarios to those legacy tests or
            # user configs; opt into each added factor explicitly.
            if "tip_carryover" not in parameters:
                carryover = {"residual_volume_ul": 0.0}
            if "mixing" not in parameters:
                mixing = {"efficiency_per_action": 1.0}
        if "proportional_bias" in parameters:
            pipetting["systematic_relative_bias"] = parameters["proportional_bias"]

        requested_pipetting_model = pipetting_selection.get("model", "shared_bounded_bias_gaussian_repeatability")
        if legacy_pipetting_override:
            requested_pipetting_model = "shared_bounded_bias_gaussian_repeatability"
        # Keep compatibility with configs created before the factor taxonomy
        # made it explicit that only repeatability uses a Gaussian.
        self.pipetting_model = {
            "shared_bias_gaussian_repeatability": "shared_bounded_bias_gaussian_repeatability",
        }.get(requested_pipetting_model, requested_pipetting_model)
        requested_tip_model = carryover_selection.get("model", "fixed_residual_volume")
        requested_mixing_model = mixing_selection.get("model", "fixed_efficiency")
        if "rate_range_s_inv" not in mixing and ("efficiency_per_action" in mixing or "efficiency_range" in mixing):
            # Explicit legacy overrides replace the selected rate parameters.
            # Do not try drawing k from missing bounds for historical configs.
            requested_mixing_model = ("truncated_gaussian_efficiency_per_action"
                                      if mixing.get("efficiency_range") and "efficiency_prior_mean" in mixing
                                      else "bounded_efficiency_per_action")
        self.tip_model = {"fixed_residual_volume": "bounded_residual_volume"}.get(requested_tip_model, requested_tip_model)
        self.mixing_model = {"fixed_efficiency": "bounded_efficiency_per_action"}.get(requested_mixing_model, requested_mixing_model)
        supported = {
            "pipetting": {"shared_bounded_bias_gaussian_repeatability", CONTINUOUS_PIPETTE_MODEL_ID},
            "tip_carryover": {"uniform_residual_fraction", "bounded_residual_volume", "truncated_gaussian_residual_volume"},
            "mixing": {"uniform_efficiency", "bounded_efficiency_per_action", "truncated_gaussian_efficiency_per_action", "truncated_gaussian_rate_per_second"},
        }
        for factor_name, model_name, choices in (
            ("pipetting", self.pipetting_model, supported["pipetting"]),
            ("tip_carryover", self.tip_model, supported["tip_carryover"]),
            ("mixing", self.mixing_model, supported["mixing"]),
        ):
            if model_name not in choices:
                raise ValueError(f"不支持的误差模型: {factor_name}/{model_name}")

        bias_range = pipetting.get("systematic_relative_bias_range")
        cv_range = pipetting.get("repeatability_cv_range")
        self.proportional_bias = (
            self._sample_range(bias_range, rng, "移液系统偏差")
            if bias_range and self.enabled else
            (0.0 if bias_range else float(pipetting.get("systematic_relative_bias", pipetting.get("proportional_bias", 0.0))))
        )
        self.repeatability_cv = self._sample_range(cv_range, rng, "移液重复性 CV") if cv_range and self.enabled else None
        self.absolute_bias_l = float(pipetting.get("absolute_bias_ul", 0.0)) / 1_000_000.0
        self.repeatability_sd_l = float(pipetting.get("repeatability_sd_ul", 0.0)) / 1_000_000.0
        self.source_biases = {
            key: float(value)
            for key, value in pipetting.get("source_proportional_bias", {}).items()
        }
        self.tip_residual_l = float(carryover.get("residual_volume_ul", 0.0)) / 1_000_000.0
        self.tip_residual_volume_range_ul = carryover.get("residual_volume_ul_range")
        self.tip_residual_volume_prior_mean_ul = carryover.get("residual_volume_ul_prior_mean")
        self.tip_residual_volume_prior_sd_ul = carryover.get("residual_volume_ul_prior_sd")
        self.tip_residual_fraction_range = carryover.get("residual_fraction_range")
        self.mixing_efficiency = float(mixing.get("efficiency_per_action", 1.0))
        self.mixing_efficiency_range = mixing.get("efficiency_range")
        self.mixing_efficiency_prior_mean = mixing.get("efficiency_prior_mean")
        self.mixing_efficiency_prior_sd = mixing.get("efficiency_prior_sd")
        self.mixing_rate_range = mixing.get("rate_range_s_inv")
        self.mixing_rate_prior_mean = mixing.get("rate_prior_mean_s_inv")
        self.mixing_rate_prior_sd = mixing.get("rate_prior_sd_s_inv")
        self.mixing_rate_s_inv = None
        # Paired-cell mixing uses one shared (device/run-level) latent effect
        # plus a cell-specific deviation. This correlation is a provisional
        # scenario assumption until measured for the actual two-cell setup.
        self.paired_mixing_correlation = float(mixing.get("paired_cell_correlation", 0.8))
        if self.tip_model == "truncated_gaussian_residual_volume" and self.tip_residual_volume_range_ul:
            self._validate_truncated_gaussian_parameters(
                self.tip_residual_volume_range_ul,
                self.tip_residual_volume_prior_mean_ul,
                self.tip_residual_volume_prior_sd_ul,
                "枪头残液体积",
            )
        if self.mixing_model == "truncated_gaussian_rate_per_second" and self.mixing_rate_range:
            self._validate_truncated_gaussian_parameters(
                self.mixing_rate_range, self.mixing_rate_prior_mean,
                self.mixing_rate_prior_sd, "混匀速率 k"
            )
        elif self.mixing_model == "truncated_gaussian_efficiency_per_action" and self.mixing_efficiency_range:
            self._validate_truncated_gaussian_parameters(
                self.mixing_efficiency_range,
                self.mixing_efficiency_prior_mean,
                self.mixing_efficiency_prior_sd,
                "混匀效率",
            )
        # Sample one scenario parameter for each of these factors per run.
        # The selected residual/efficiency is then reused by the protocol;
        # only pipetting repeatability varies independently per aspiration.
        self.sampled_tip_residual_fraction = (
            self._sample_range(self.tip_residual_fraction_range, rng, "枪头残液比例")
            if self.enabled and self.tip_residual_fraction_range else 0.0
        )
        self.sampled_tip_residual_volume_ul = None
        if self.enabled and self.tip_residual_volume_range_ul:
            if self.tip_model == "truncated_gaussian_residual_volume":
                self.sampled_tip_residual_volume_ul = self._sample_truncated_gaussian(
                    self.tip_residual_volume_range_ul,
                    self.tip_residual_volume_prior_mean_ul,
                    self.tip_residual_volume_prior_sd_ul,
                    rng,
                    "枪头残液体积",
                )
            else:
                self.sampled_tip_residual_volume_ul = self._sample_range(
                    self.tip_residual_volume_range_ul, rng, "枪头残液体积"
                )
            self.tip_residual_l += self.sampled_tip_residual_volume_ul / 1_000_000.0
        if self.enabled and self.mixing_efficiency_range:
            if self.mixing_model == "truncated_gaussian_efficiency_per_action":
                self.mixing_efficiency = self._sample_truncated_gaussian(
                    self.mixing_efficiency_range,
                    self.mixing_efficiency_prior_mean,
                    self.mixing_efficiency_prior_sd,
                    rng,
                    "混匀效率",
                )
            else:
                self.mixing_efficiency = self._sample_range(
                    self.mixing_efficiency_range, rng, "混匀效率"
                )
        self.pipetting_volume_ul_range = pipetting.get("applicable_volume_ul_range")
        self.volume_profiles = pipetting.get("volume_profiles")
        self.pipette_profiles = pipetting.get("pipette_profiles", [])
        self.pipette_scenario_multiplier = float(pipetting.get("scenario_multiplier", 1.0))
        self._volume_samples: dict[str, tuple[float, float | None]] = {}
        self._pipette_bias_fractions: dict[str, float] = {}
        self._continuous_action_samples: dict[str, dict] = {}
        values = [self.proportional_bias, self.absolute_bias_l, self.repeatability_sd_l,
                  self.tip_residual_l, self.mixing_efficiency, *self.source_biases.values()]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("误差参数必须有限")
        if self.repeatability_cv is not None and (not math.isfinite(self.repeatability_cv) or self.repeatability_cv < 0):
            raise ValueError("移液重复性 CV 必须有限且非负")
        if self.repeatability_sd_l < 0:
            raise ValueError("重复性标准差不得为负")
        if self.tip_residual_l < 0:
            raise ValueError("枪头残液体积不得为负")
        if not 0 <= self.mixing_efficiency <= 1:
            raise ValueError("每次混匀效率必须在 0 到 1 之间")
        self._validate_range(self.tip_residual_fraction_range, "枪头残液比例", 0.0, 1.0)
        self._validate_range(self.tip_residual_volume_range_ul, "枪头残液体积", 0.0, 1_000_000.0)
        self._validate_range(self.mixing_efficiency_range, "混匀效率", 0.0, 1.0)
        self._validate_range(self.mixing_rate_range, "混匀速率 k", 0.0, 1e9)
        if (not math.isfinite(self.paired_mixing_correlation)
                or not 0.0 <= self.paired_mixing_correlation < 1.0):
            raise ValueError("双池混匀相关系数必须有限且位于 [0, 1) 区间")
        self._validate_range(self.pipetting_volume_ul_range, "移液误差适用体积", 0.0, 1_000_000.0)
        if self.volume_profiles:
            for band in self.volume_profiles:
                self._validate_range(band.get("applicable_volume_ul_range"), "分体积移液适用体积", 0.0, 1_000_000.0)
                self._validate_range(band.get("systematic_relative_bias_range"), "分体积系统偏差", -1e9, 1e9)
                self._validate_range(band.get("repeatability_cv_range"), "分体积重复性CV", 0.0, 1e9)
        pipetting_profiled = bool(pipetting_selection.get("parameters")) or any(
            key in parameters for key in ("proportional_bias", "absolute_bias_ul", "repeatability_sd_ul", "source_proportional_bias")
        ) or bool(parameters.get("pipetting")) or bool(self.volume_profiles) or bool(self.pipette_profiles)
        carryover_profiled = bool(carryover_selection.get("parameters")) or bool(parameters.get("tip_carryover"))
        mixing_profiled = bool(mixing_selection.get("parameters")) or bool(parameters.get("mixing"))
        self._realized_profile = {
            "enabled": self.enabled,
            "pipetting": {
                "status": "parameterized" if pipetting_profiled else "not_parameterized",
                "model": self.pipetting_model,
                "mechanism": {
                    "systematic_bias": ("one signed manufacturer-limit fraction shared within each pipette model per run; absolute limit is interpolated by requested volume"
                                        if self.pipetting_model == CONTINUOUS_PIPETTE_MODEL_ID else
                                        "one bounded calibration offset shared within a run and volume band"),
                    "repeatability": ("independent zero-mean Gaussian approximation per aspiration, using interpolated manufacturer random-error scale"
                                      if self.pipetting_model == CONTINUOUS_PIPETTE_MODEL_ID else
                                      "independent zero-mean Gaussian approximation per aspiration, using CV"),
                    "parameter_range_sampling": "uniform only expresses uncertainty within published bounds; it is not a measured distribution",
                },
                "level": pipetting_selection.get("level"),
                "parameter_source": pipetting_selection.get("parameter_source", parameters.get("parameter_source", "user_configured")),
                "systematic_relative_bias_range": bias_range,
                "repeatability_cv_range": cv_range,
                "applicable_volume_ul_range": self.pipetting_volume_ul_range,
                "volume_profiles": self.volume_profiles,
                "pipette_profiles": self.pipette_profiles,
                "scenario_multiplier": self.pipette_scenario_multiplier,
                "scope_note": ("厂家规格锚点间线性插值的参考情景；低档倍率 1.0，中档 1.5，高档 2.0；不是当前机器人/枪头/操作流程的校准。"
                               if self.pipetting_model == CONTINUOUS_PIPETTE_MODEL_ID else None),
                "sampled_by_volume_band": self._volume_samples,
                "sampled_bias_fraction_by_pipette": self._pipette_bias_fractions,
                "sampled_continuous_actions": self._continuous_action_samples,
                # This legacy scalar belongs to the older relative-bias model.
                # Continuous commercial references instead use a shared signed
                # fraction of each pipette's interpolated absolute limit, kept
                # in sampled_bias_fraction_by_pipette. Reporting the legacy
                # default (usually 0.0) here falsely suggests no systematic error.
                "sampled_systematic_relative_bias": (
                    None if self.pipetting_model == CONTINUOUS_PIPETTE_MODEL_ID
                    else self.proportional_bias
                ),
                "sampled_repeatability_cv": self.repeatability_cv,
            },
            "tip_carryover": {
                "status": "parameterized" if carryover_profiled else "not_parameterized",
                "model": self.tip_model,
                "mechanism": "one absolute residual-liquid volume is sampled per run and reused; each dispense derives its relative underdelivery from residual volume / aliquot volume; not voltage noise",
                "scope_note": "provisional generic aqueous disposable-tip prior; not conditioned on tip model, liquid properties, or dispense technique; cross-sample analyte contamination is a separate, currently inactive mechanism",
                "level": carryover_selection.get("level"),
                "parameter_source": carryover_selection.get("parameter_source", parameters.get("parameter_source", "user_configured")),
                "residual_volume_ul_range": self.tip_residual_volume_range_ul,
                "sampling_distribution": "truncated_gaussian" if self.tip_model == "truncated_gaussian_residual_volume" else ("bounded_uniform_legacy" if self.tip_residual_volume_range_ul else "fixed"),
                "residual_volume_ul_prior_mean": self.tip_residual_volume_prior_mean_ul,
                "residual_volume_ul_prior_sd": self.tip_residual_volume_prior_sd_ul,
                "sampled_residual_volume_ul": self.tip_residual_l * 1_000_000.0 if self.enabled and carryover_profiled else None,
                "residual_fraction_range": self.tip_residual_fraction_range,
                "sampled_residual_fraction": self.sampled_tip_residual_fraction if self.enabled and carryover_profiled else None,
                "cross_sample_contamination_status": "not_modeled",
            },
            "mixing": {
                "status": "parameterized" if mixing_profiled else "not_parameterized",
                "model": self.mixing_model,
                "mechanism": "first-order homogenization: eta(t)=1-exp(-k*t); k is drawn once per run from a bounded Gaussian prior and the same draw is evaluated at the configured mixing duration",
                "level": mixing_selection.get("level"),
                "parameter_source": mixing_selection.get("parameter_source", parameters.get("parameter_source", "user_configured")),
                "efficiency_range": self.mixing_efficiency_range,
                "sampling_distribution": ("truncated_gaussian_rate_per_second" if self.mixing_model == "truncated_gaussian_rate_per_second" else "truncated_gaussian" if self.mixing_model == "truncated_gaussian_efficiency_per_action" else ("bounded_uniform_legacy" if self.mixing_efficiency_range else "fixed")),
                "efficiency_prior_mean": self.mixing_efficiency_prior_mean,
                "efficiency_prior_sd": self.mixing_efficiency_prior_sd,
                "sampled_efficiency_per_action": (self.mixing_efficiency if self.enabled and mixing_profiled
                                                  and self.mixing_model != "truncated_gaussian_rate_per_second" else None),
                "reference_efficiency_at_10_s": (
                    1.0 - math.exp(-float(self.mixing_rate_prior_mean) * 10.0)
                    if mixing_profiled and self.mixing_model == "truncated_gaussian_rate_per_second"
                    and self.mixing_rate_prior_mean is not None else None
                ),
                "sampled_duration_s": None,
                "rate_prior_model": "truncated_gaussian_rate_per_second" if self.mixing_model == "truncated_gaussian_rate_per_second" else None,
                "rate_range_s_inv": self.mixing_rate_range,
                "rate_prior_mean_s_inv": self.mixing_rate_prior_mean,
                "rate_prior_sd_s_inv": self.mixing_rate_prior_sd,
                "sampled_rate_s_inv": self.mixing_rate_s_inv if self.enabled and mixing_profiled else None,
                "paired_cell_correlation": self.paired_mixing_correlation,
                "paired_cell_sampling": "shared_latent_plus_independent_cell_deviation",
                "paired_cell_correlation_status": "provisional_uncalibrated_assumption",
                "sampled_paired_efficiencies": None,
            },
            "factor_evidence": parameters.get("factor_evidence", {}),
        }

    @staticmethod
    def _validate_range(bounds: list | None, label: str, minimum: float, maximum: float) -> None:
        if bounds is None:
            return
        if len(bounds) != 2:
            raise ValueError(f"{label}范围必须包含上下限")
        low, high = map(float, bounds)
        if not all(math.isfinite(value) for value in (low, high)) or low > high or low < minimum or high > maximum:
            raise ValueError(f"{label}范围无效")

    @classmethod
    def _sample_range(cls, bounds: list, rng: random.Random, label: str) -> float:
        cls._validate_range(bounds, label, -1e9, 1e9)
        low, high = map(float, bounds)
        return rng.uniform(low, high)

    @classmethod
    def _sample_truncated_gaussian(
        cls,
        bounds: list,
        mean: float | None,
        standard_deviation: float | None,
        rng: random.Random,
        label: str,
    ) -> float:
        """Draw from a factor-specific normal prior, rejecting out-of-range draws."""
        cls._validate_truncated_gaussian_parameters(bounds, mean, standard_deviation, label)
        low, high = map(float, bounds)
        mean = float(mean)
        standard_deviation = float(standard_deviation)
        if low == high or standard_deviation == 0:
            return mean
        for _ in range(100_000):
            sample = rng.gauss(mean, standard_deviation)
            if low <= sample <= high:
                return sample
        raise ValueError(f"{label}截断高斯抽样未能落入设定范围；请检查均值和标准差")

    @classmethod
    def _validate_truncated_gaussian_parameters(
        cls,
        bounds: list,
        mean: float | None,
        standard_deviation: float | None,
        label: str,
    ) -> None:
        cls._validate_range(bounds, label, -1e9, 1e9)
        low, high = map(float, bounds)
        if mean is None or standard_deviation is None:
            raise ValueError(f"{label}截断高斯模型必须配置先验均值和标准差")
        mean = float(mean)
        standard_deviation = float(standard_deviation)
        if not math.isfinite(mean) or not low <= mean <= high:
            raise ValueError(f"{label}先验均值必须位于设定范围内")
        if not math.isfinite(standard_deviation) or standard_deviation < 0:
            raise ValueError(f"{label}先验标准差必须有限且非负")

    def snapshot(self) -> dict:
        """本次运行实际采用的档位与抽取参数，写入结果便于追溯。"""
        return self._realized_profile

    def realize(self, command: Action, rng: random.Random) -> RealizedAction:
        if not self.enabled:
            if command.kind == "aspirate" and self.pipetting_model == CONTINUOUS_PIPETTE_MODEL_ID:
                assert command.volume_l is not None
                reference = reference_at_volume(
                    {"pipette_profiles": self.pipette_profiles,
                     "scenario_multiplier": self.pipette_scenario_multiplier},
                    command.volume_l * 1_000_000.0,
                )
                if command.pipette_id and command.pipette_id != reference["pipette_id"]:
                    raise ValueError("步骤指定的移液器与设定体积对应的量程不匹配")
                return RealizedAction(
                    command, command.volume_l, error_status="disabled",
                    pipette_id=reference["pipette_id"],
                    pipette_nominal_range_ul=tuple(reference["nominal_volume_range_ul"]),
                    pipette_evidence_status=reference["evidence_status"],
                    pipette_systematic_limit_ul=reference["systematic_error_limit_ul"],
                    pipette_repeatability_sd_ul=reference["repeatability_sd_limit_ul"],
                )
            return RealizedAction(command, command.volume_l, error_status="disabled")
        if command.kind == "mix":
            if not self._realized_profile["mixing"]["status"] == "parameterized":
                return RealizedAction(command, command.volume_l, mixing_efficiency=self.mixing_efficiency, error_status="not_parameterized")
            if self.mixing_model == "truncated_gaussian_rate_per_second" and self.mixing_rate_s_inv is None:
                self.mixing_rate_s_inv = self._sample_truncated_gaussian(
                    self.mixing_rate_range, self.mixing_rate_prior_mean,
                    self.mixing_rate_prior_sd, rng, "混匀速率 k"
                )
                self.mixing_efficiency = 1.0 - math.exp(-self.mixing_rate_s_inv * 10.0)
                self._realized_profile["mixing"]["sampled_rate_s_inv"] = self.mixing_rate_s_inv
                self._realized_profile["mixing"]["reference_efficiency_at_10_s"] = self.mixing_efficiency
            elapsed = command.duration_s if command.duration_s is not None else 10.0
            efficiency = (1.0 - math.exp(-self.mixing_rate_s_inv * elapsed)
                          if self.mixing_rate_s_inv is not None else self.mixing_efficiency)
            self._realized_profile["mixing"]["sampled_duration_s"] = elapsed
            self._realized_profile["mixing"]["sampled_efficiency_per_action"] = efficiency
            return RealizedAction(command, command.volume_l, mixing_efficiency=efficiency, error_status="applied")
        if command.kind == "dispense":
            if not self._realized_profile["tip_carryover"]["status"] == "parameterized":
                return RealizedAction(command, command.volume_l, error_status="not_parameterized")
            return RealizedAction(command, command.volume_l, tip_residual_l=self.tip_residual_l, tip_residual_fraction=self.sampled_tip_residual_fraction, error_status="applied")
        if command.kind != "aspirate":
            return RealizedAction(command, command.volume_l)
        assert command.volume_l is not None
        if not self._realized_profile["pipetting"]["status"] == "parameterized":
            return RealizedAction(command, command.volume_l, error_status="not_parameterized")
        if self.pipetting_model == CONTINUOUS_PIPETTE_MODEL_ID:
            return self._realize_continuous_pipetting(command, rng)
        volume_parameters = None
        volume_band_id = None
        if self.volume_profiles:
            requested_ul = command.volume_l * 1_000_000.0
            for candidate in self.volume_profiles:
                bounds = candidate.get("applicable_volume_ul_range")
                if bounds and float(bounds[0]) <= requested_ul <= float(bounds[1]):
                    volume_parameters = candidate
                    volume_band_id = str(candidate.get("volume_band_id", bounds))
                    break
            if volume_parameters is None:
                return RealizedAction(command, command.volume_l, error_status="outside_profile_scope")
            if volume_parameters.get("status") != "parameterized":
                return RealizedAction(command, command.volume_l, error_status="not_parameterized")
        if self.pipetting_volume_ul_range is not None:
            min_ul, max_ul = map(float, self.pipetting_volume_ul_range)
            requested_ul = command.volume_l * 1_000_000.0
            if not min_ul <= requested_ul <= max_ul:
                return RealizedAction(command, command.volume_l, error_status="outside_profile_scope")
        if volume_parameters:
            assert volume_band_id is not None
            if volume_band_id not in self._volume_samples:
                band_bias_range = volume_parameters.get("systematic_relative_bias_range")
                band_cv_range = volume_parameters.get("repeatability_cv_range")
                band_bias = self._sample_range(band_bias_range, rng, "分体积移液系统偏差") if band_bias_range else 0.0
                band_cv = self._sample_range(band_cv_range, rng, "分体积移液重复性 CV") if band_cv_range else None
                self._volume_samples[volume_band_id] = (band_bias, band_cv)
            band_bias, band_cv = self._volume_samples[volume_band_id]
        else:
            band_bias, band_cv = self.proportional_bias, self.repeatability_cv
        bias = band_bias + self.source_biases.get(command.source, 0.0)
        systematic = command.volume_l * bias + self.absolute_bias_l
        cv = band_cv if volume_parameters else self.repeatability_cv
        repeatability_sd_l = command.volume_l * cv if cv is not None else self.repeatability_sd_l
        # Repeatability is the only Gaussian term here. Reject physically
        # impossible draws instead of silently clipping the distribution.
        noise = 0.0
        volume = command.volume_l + systematic
        if repeatability_sd_l:
            for _ in range(10_000):
                noise = rng.gauss(0.0, repeatability_sd_l)
                volume = command.volume_l + systematic + noise
                if volume > 0:
                    break
            else:
                raise ValueError("移液重复性抽样无法得到正体积")
        if not math.isfinite(volume) or volume <= 0:
            raise ValueError("模拟吸液量无效；未自动截断误差分布")
        return RealizedAction(command, volume, systematic, noise, error_status="applied")

    def _realize_continuous_pipetting(self, command: Action, rng: random.Random) -> RealizedAction:
        """Use manufacturer reference anchors continuously within each tool range.

        The manufacturer systematic-error limit is represented by one shared
        signed latent fraction per pipette/run. The repeatability limit is used
        as a per-aspiration Gaussian scale approximation, not a measured draw
        distribution.
        """
        assert command.volume_l is not None
        requested_ul = command.volume_l * 1_000_000.0
        parameters = {
            "pipette_profiles": self.pipette_profiles,
            "scenario_multiplier": self.pipette_scenario_multiplier,
        }
        try:
            reference = reference_at_volume(parameters, requested_ul)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"移液误差模型不支持 {requested_ul:g} μL：{exc}") from exc
        if command.pipette_id and command.pipette_id != reference["pipette_id"]:
            raise ValueError(
                f"步骤指定的移液器 {command.pipette_id} 与 {requested_ul:g} μL 所选量程"
                f" {reference['pipette_id']} 不匹配"
            )

        pipette_id = reference["pipette_id"]
        if pipette_id not in self._pipette_bias_fractions:
            self._pipette_bias_fractions[pipette_id] = rng.uniform(-1.0, 1.0)
        bias_fraction = self._pipette_bias_fractions[pipette_id]
        systematic_ul = bias_fraction * reference["systematic_error_limit_ul"]
        systematic_ul += requested_ul * self.source_biases.get(command.source, 0.0)
        repeatability_sd_ul = reference["repeatability_sd_limit_ul"]
        noise_ul = rng.gauss(0.0, repeatability_sd_ul) if repeatability_sd_ul else 0.0
        realized_ul = requested_ul + systematic_ul + noise_ul
        if not math.isfinite(realized_ul) or realized_ul <= 0:
            raise ValueError("连续量程移液抽样得到非正体积；未截断误差分布")

        self._continuous_action_samples[command.step_id] = {
            "pipette_id": pipette_id,
            "requested_volume_ul": requested_ul,
            "nominal_volume_range_ul": reference["nominal_volume_range_ul"],
            "systematic_error_limit_ul": reference["systematic_error_limit_ul"],
            "sampled_systematic_offset_ul": systematic_ul,
            "repeatability_sd_limit_ul": repeatability_sd_ul,
            "sampled_random_offset_ul": noise_ul,
            "evidence_status": reference["evidence_status"],
            "parameter_source_status": reference["parameter_source_status"],
        }
        return RealizedAction(
            command,
            realized_ul / 1_000_000.0,
            systematic_ul / 1_000_000.0,
            noise_ul / 1_000_000.0,
            error_status="applied",
            pipette_id=pipette_id,
            pipette_nominal_range_ul=tuple(reference["nominal_volume_range_ul"]),
            pipette_evidence_status=reference["evidence_status"],
            pipette_systematic_limit_ul=reference["systematic_error_limit_ul"],
            pipette_repeatability_sd_ul=repeatability_sd_ul,
        )

    def realize_paired_mixing(self, rng: random.Random, duration_s: float | None = None) -> dict[str, float]:
        """Draw close, non-identical mixing efficiencies for two simultaneous cells.

        The selected mixing profile supplies the marginal mean, spread and
        bounds. A shared Gaussian latent term models the common plate/run
        conditions; separate Gaussian terms model each cell's local variation.
        This affects only two-cell simultaneous mixing. Single-cell mixing
        continues to use ``realize`` and its existing per-run efficiency.
        """
        if not isinstance(rng, random.Random):
            raise ValueError("双池混匀需要可复现的随机数生成器")
        mixing = self._realized_profile["mixing"]
        is_parameterized = (
            self.enabled and mixing.get("status") == "parameterized"
        )
        if not is_parameterized:
            pair = {"left_half_cell": self.mixing_efficiency,
                    "right_half_cell": self.mixing_efficiency}
            mixing["sampled_paired_efficiencies"] = dict(pair)
            mixing["sampled_duration_s"] = duration_s if duration_s is not None else 10.0
            return pair

        if self.mixing_model == "truncated_gaussian_rate_per_second":
            bounds = self.mixing_rate_range
            low, high = map(float, bounds)
            mean, sd = float(self.mixing_rate_prior_mean), float(self.mixing_rate_prior_sd)
            if sd == 0 or low == high:
                rates = {"left_half_cell": mean, "right_half_cell": mean}
            else:
                common_weight = math.sqrt(self.paired_mixing_correlation)
                local_weight = math.sqrt(1.0 - self.paired_mixing_correlation)
                for _ in range(100_000):
                    shared = rng.gauss(0.0, 1.0)
                    left = mean + sd * (common_weight * shared + local_weight * rng.gauss(0.0, 1.0))
                    right = mean + sd * (common_weight * shared + local_weight * rng.gauss(0.0, 1.0))
                    if low <= left <= high and low <= right <= high:
                        rates = {"left_half_cell": left, "right_half_cell": right}
                        break
                else:
                    raise ValueError("双池混匀速率抽样未能落入先验范围")
            mixing["sampled_paired_rates_s_inv"] = dict(rates)
            mixing["sampled_rate_s_inv"] = None
            elapsed = duration_s if duration_s is not None else 10.0
            mixing["sampled_duration_s"] = elapsed
            pair = {key: 1.0 - math.exp(-rate * elapsed) for key, rate in rates.items()}
            mixing["sampled_paired_efficiencies"] = dict(pair)
            return pair

        bounds = self.mixing_efficiency_range
        low, high = map(float, bounds) if bounds else (self.mixing_efficiency, self.mixing_efficiency)
        if self.mixing_model == "truncated_gaussian_efficiency_per_action":
            mean = float(self.mixing_efficiency_prior_mean)
            sd = float(self.mixing_efficiency_prior_sd)
            if sd == 0 or low == high:
                pair = {"left_half_cell": mean, "right_half_cell": mean}
            else:
                common_weight = math.sqrt(self.paired_mixing_correlation)
                local_weight = math.sqrt(1.0 - self.paired_mixing_correlation)
                for _ in range(100_000):
                    shared = rng.gauss(0.0, 1.0)
                    left_z = common_weight * shared + local_weight * rng.gauss(0.0, 1.0)
                    right_z = common_weight * shared + local_weight * rng.gauss(0.0, 1.0)
                    left = mean + sd * left_z
                    right = mean + sd * right_z
                    if low <= left <= high and low <= right <= high:
                        pair = {"left_half_cell": left, "right_half_cell": right}
                        break
                else:
                    raise ValueError("双池混匀效率抽样未能落入先验范围")
        elif self.mixing_model == "uniform_efficiency" or self.mixing_model == "bounded_efficiency_per_action":
            # Gaussian-copula sampling preserves the selected uniform marginal
            # while introducing only the configured shared-cell dependence.
            common_weight = math.sqrt(self.paired_mixing_correlation)
            local_weight = math.sqrt(1.0 - self.paired_mixing_correlation)
            shared = rng.gauss(0.0, 1.0)
            left_z = common_weight * shared + local_weight * rng.gauss(0.0, 1.0)
            right_z = common_weight * shared + local_weight * rng.gauss(0.0, 1.0)
            normal = NormalDist()
            pair = {
                "left_half_cell": low + (high - low) * normal.cdf(left_z),
                "right_half_cell": low + (high - low) * normal.cdf(right_z),
            }
        else:
            raise ValueError(f"当前误差模型不支持双池混匀: {self.mixing_model}")

        mixing["sampled_efficiency_per_action"] = None
        mixing["sampled_paired_efficiencies"] = dict(pair)
        return pair
