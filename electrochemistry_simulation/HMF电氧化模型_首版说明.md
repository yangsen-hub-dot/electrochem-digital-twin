# HMF 电氧化终点模型（本地）

模型编号 `hmf_tempo_fdca_endpoint_dargo2025_v1`，通过 `chemical_models.get_chemical_model` 获取。输入为已有 `ExperimentState` 中指定反应容器的权威液量和 HMF、TEMPO、KOH 物质的量，以及条件字典：`reaction_container`、`current_a`、`duration_s`、`anode`、`cathode`、`electrolyte`、`mode`、`cell_configuration`。调用 `model.observe({}, state, cell, {})` 返回独立的估计结果，不写入物料库存，不声称进行电解、取样或 HPLC 检测。

数据来源：Dargó 等，ChemSusChem，doi:10.1002/cssc.202401190，表 4；25、31.25、37.5、43.75、50 mM 对应 96%、96%、84%、74%、52%。点间线性插值是工程假设。只接受论文单瓶、石墨阳极、不锈钢阴极、5 mL、0.1 M KOH、5 mol% TEMPO、10 mA、6 F/mol HMF；使用数值容差只为浮点误差，不代表已验证的实验容差。温度、混匀、母液差异等尚未模型化，预测未经过本平台校准，无可信预测区间。

实际液量、投料、KOH 或电解参数偏离所列参考条件时，仍返回可算出的实际浓度及电量，但不提供 FDCA 数值。这个严格限制意味着一般随机移液误差会使预测不可用；需要额外实验数据支持多变量模型，不能将缺失的数据用臆造的影响函数替代。预测不自动推定电解已完成，电流与时间只是调用方提供的条件。电量是电流乘时间，理想 6 电子电量只作为计量参考，不证明 FDCA 实际生成。

现已新增独立的单池数值流程编排，说明见 `通用流程编排_本地说明.md`；原有 `run_experiment` 仍固定双容器和电压结果。实际电极机器人、电解设备反馈、反应物库存更新、HPLC 及真实硬件均未接入。要开展真实端到端闭环须确认设备回执、具备可独立核验的物料/电子平衡和实验验证；不能让预测结果冒充观测值或实体实验。

验证：在 `electrochemistry_simulation` 目录下运行 `python -m unittest tests.test_hmf_fdca_model -v`。
