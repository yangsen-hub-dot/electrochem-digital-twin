# 实验模型与当前能力

## 浓差电池

源代码：`electrochemistry_simulation/electrochem_sim`。读取条件、协议动作与权威状态账本，组合操作误差、活度修正和可选液接估算。

铜体系可接既有 Matterix 原型；锌体系已有数值配置，不代表相同机器人动作、器皿和误差参数已验证。

数值 HTTP 适配器为 `copper_cell_node.py`；带动作、录像和局部校验的协调服务为 `integration/coordinator/cloud_experiment_server.py`。两者使用同一数值核心，但运行方式和可用接口不同。

## TEMPO–HMF → FDCA

沿用 `integration/web/hmf_conditions.py`、`hmf_local_server.py` 和 `electrochem_sim/workflow_runner.py`。

文献终点预测只支持所记录参考装置下 HMF 25–50 mM、TEMPO 5 mol%、KOH 0.1 M、10 mA 等匹配条件。偏离适用条件时保留数值流程记录，明确不给出有效收率预测。详见 `electrochemistry_simulation/HMF电氧化模型_首版说明.md`。

HMF 三维机器人动作、真实 HPLC 测量和完整反应后化学库存尚未实现。前端配图是原理示意。

## 新模型扩展

先定义模型和具体体系的条件、状态、观测类型、适用范围、动作与校验规则，再注册到现有模型/流程接口。前端选择模型后加载匹配的条件和数据，不套用其他体系的电压、视频或历史。
