# 电化学实验流程仿真基础模板

版本：0.2.0。当前提供铜、锌两种金属离子浓差电池示例；核心能斯特计算按配置反应商工作。Python 3.10 及以上，只使用标准库，不需要 GPU、MATTERIX、密钥或额外安装包。

本项目是仿真核心，HTML 是读取节点数据的轻量展示页面。上级目录的 `copper_cell_node.py` 是部署适配器；每次本地更新后，需将对应代码、配置和页面文件同步到 Bohrium 才会影响节点端。旧固定基线脚本已归档到上级 `legacy` 目录。

## 固定流程误差拆解检查（2026-10-20）

`electrochem_sim/error_chain_validation.py` 是本地数值核验工具，复用条件规划、移液/混匀执行和电化学计算。它在诊断副本中隔离理想、仅系统偏差、仅重复性、仅残液、仅混匀和全部误差，保留当前同头连续转移策略；不改生产参数、不补液、不调用云端或机器人。

```powershell
python -m electrochem_sim.error_chain_validation --output 'D:\codex\误差拆解核验与云端连接恢复_20261020\数值验证\误差链审计.json' --repeats 200 --first-seed 20000
```

报告保存每类情景一份完整动作/状态实例，以及重复模拟的精简样本和统计量。经验百分位是当前假设下的模型抽样范围；混匀局部电压范围是概念分区端点情景，二者含义不同。主电压使用总体平均浓度。相同种子编号不保证不同误差情景之间是配对抽样，不能从各组方差直接相减推断真实独立误差贡献。活度/液接对照另行核对相同实际动作事件，液接沿用已授权的参考估算策略。

## 模块边界（已按讨论拆分）

- `config/copper_concentration_cell.json`：实验条件——物种/电解质、母液/溶剂、容器、温度、电池两侧映射、电极/盐桥信息、误差参数，并引用流程文件。它不再内嵌操作步骤。
- `config/protocols/copper_concentration_cell_stock_dilution.json`：有序操作步骤——配液、混匀、电极就位、盐桥连接。流程只描述做什么，不保存实验条件或公式。
- `electrochem_sim/state.py`：过程状态账本——各容器/枪头/废液的体积和物质的量、概念液体分区及其浓度差、电池组装状态。浓度由物质的量和体积推导。
- `electrochem_sim/state_ledger.py`：跨域统一账本元数据——流程阶段/步骤、设备回执观测、实体位置/连接关系、确定性和来源、化学模型版本及局部修复谱系。化学库存仍只保存在 `ExperimentState`，本模块不维护第二份化学库存。
- `electrochem_sim/error_model.py`：通用误差入口。错误类别为移液体积误差、枪头残液/带液误差、不完全混匀误差；模型描述各类别的产生机制；参数档案记录适用条件和来源；低/中/高是档案内可选择的强度级别。
- `config/error_profiles/README.md`：误差档案库的分类、命名和新增规则。档案管理参数及适用条件，核心代码实现模型。
- `config/error_profiles/gilson_pipetman_p200_p1000_continuous_v1.json`：当前连续量程移液参考档案。P200 覆盖 20–200 μL，P1000 覆盖 >200–1000 μL；厂家规格锚点之间按绝对 μL 误差线性插值，低/中/高倍率分别为 1.0/1.5/2.0。保留原 100/500/900 μL 档案中的枪头残留与混匀工程先验；旧移液模型档案不覆盖，供复现历史结果。
- `electrochem_sim/pipette_reference_model.py`：连续量程选择、锚点插值及总量分次规划；量程外不外推，少于 20 μL 明确拒绝，更大总量拆为最少次数的合法吸液。
- `electrochem_sim/liquid_transfer.py`：可复用液体转移动作（装枪头、吸液、分液、弃枪头），不计算电压；`pipetting.py` 仅保留旧导入兼容。
- `electrochem_sim/mixing.py`：混匀动作及混匀程度，与移液转移分开。
- `electrochem_sim/cell_assembly.py`：记录电极就位和盐桥连接，不模拟机器人轨迹，也不计算盐桥电势。
- `electrochem_sim/conservation.py`：独立守恒核验，只读比较操作前后总库存并报告结果，不改变状态。
- `electrochem_sim/nernst.py`：能斯特公式组件，支持输入浓度（理想基线）或无量纲活度。
- `electrochem_sim/activity.py`：离子活度估算，含离子强度计算和 Davies 近似；可从状态中已追踪的离子组成计算离子强度，参数不足时明确报错，不从不完整配方猜参数。
- `config/activity/davies_debye_huckel_A_water_neA_v1.json` 与 `electrochem_sim/activity_temperature.py`：水溶液 Debye–Hückel 常数 A 的温度参考值（0–50 °C，1 bar），仅在表格范围内插值，不向外推算。
- `config/ion_transport/aqueous_ion_diffusion_infinite_dilution_25c_v1.json`：独立的离子迁移参数参考库，记录 Cu²⁺、SO₄²⁻、K⁺、NO₃⁻ 在水溶液 25 °C 无限稀释极限下的扩散系数，以及由 Einstein/Nernst–Einstein 关系换算的迁移率；包含来源和适用范围，不等同于当前有限浓度条件的精确参数。
- `config/ion_transport/aqueous_ion_conductivity_temperature_reference_v1.json` 与 `electrochem_sim/ion_transport_temperature.py`：整理 Cu²⁺、SO₄²⁻、K⁺、NO₃⁻ 的 25 °C 无限稀释当量电导锚点；用水黏度比例方法生成 0–95 °C 的非条件匹配温度参考估算，显式允许后才使用，范围外拒绝、不外推。
- `config/ion_transport/aqueous_electrolyte_transport_finite_concentration_v1.json`：有限浓度电解质文献数据集，按参数类型分别保存多种盐的互扩散系数、KNO₃ 阳离子迁移数和 CuSO₄ 表观阳离子迁移数，并注明来源、浓度/温度条件及不可直接套用的限制。电解质互扩散系数不是单离子扩散系数或迁移率；该库当前仅做可追溯的数据归档，不自动参与液接电势计算。
- `electrochem_sim/ion_transport.py`：读取和校验离子迁移参数库及电解质文献数据库，并提供扩散系数到电迁移率的通用换算；不做浓度插值或外推。
- `electrochem_sim/liquid_junction.py`：可选择 Henderson 线性浓度梯度近似，或沿预设线性浓度剖面对一维 Nernst–Planck 零电流电场关系做数值积分；分别计算左溶液—盐桥、盐桥—右溶液两个界面，并输出各项及总液接电势。当前数值路线也假设局部电中性和线性浓度剖面，因此在相同参数下理论上与 Henderson 结果一致；它不求完整稳态各离子通量守恒，也不是完整 N–P–Poisson 求解器。条件不匹配的参数默认拒绝用于计算；只有显式选择参考值估算策略时才计算，并标记为非条件匹配估算。
- `electrochem_sim/chemical_models.py`：可注册的化学模型契约，提供模型状态初始化、按已确认步骤推进、模型状态转移校验及结构化观测接口。当前仅注册已有浓差电池能斯特平衡模型；接口为后续反应动力学、扩散传质和电极界面动力学模型预留，尚未实现这些动力学方程。
- `electrochem_sim/electrochemistry.py`：经模型注册表调用配置的电化学模型。当前能斯特模型读取同一半反应和反应商，计算左右两侧浓差电池电压，再组合活度修正和液接电势。添加新模型必须提供适用范围、模型状态转移校验和可观测输出，不能假设所有实验都用当前浓差电池模型。
- `electrochem_sim/simulator.py`：流程总控，组合条件和步骤、调用操作/误差/核验/计算模块；不内置具体物理公式，也不负责写结果文件。
- `electrochem_sim/result_recorder.py`：独立保存单次运行完整 JSON 记录。
- `electrochem_sim/batch.py`：按基础配置中的活性物种执行浓度扫描；默认批量配置仍对应铜示例。
- `electrochem_sim/temperature_concentration_batch.py`：本地扫描温度与左右浓度组合；右侧浓度列表为可选项，未配置时沿用基础配置。
- 上级目录 `copper_cell_node.py`：可替换的 HTTP 部署适配器，不是仿真核心；查询网页不触发重复仿真。

`__init__.py` 和 `__main__.py` 是 Python 包支持文件，不是业务模块。

## 目录约定

```text
electrochemistry_simulation/
  electrochem_sim/                  核心代码
  config/copper_concentration_cell.json  铜示例条件和误差参数
  config/zinc_concentration_cell.json   锌示例条件
    config/protocols/*.json           独立的操作步骤
    config/ion_transport/*.json       离子与电解质输运性质文献参考数据
  tests/test_copper_concentration_cell.py  核验测试
  results/                         自动生成的运行记录
  README.md                        本说明
```

文件名采用清晰的英文小写与下划线，不使用“最终版”“新建文件”等模糊名称。业务代码不混入配置和结果目录。用户参数放在 JSON 中，避免为了改变配方修改核心代码。

## 默认案例

假设电解质为 CuSO₄ 水溶液，母液 0.1000 M，并假设完全解离、暂不考虑离子缔合。低侧：100 μL 母液 + 900 μL 水，混匀得到 1.00 mL、0.0100 M；状态同时追踪 Cu²⁺ 和 SO₄²⁻。

高侧：分两次各加入 500 μL 同批母液，混匀得到 1.00 mL、0.1000 M。暂定盐桥为 0.100 M KNO₃ 水溶液。

配置中的源液量和操作体积是纯仿真示例，不是对真实工作站量程、容器几何或精度的声明。

18 个步骤包括装枪头、吸液、排液、弃枪头、混匀、电极就位和盐桥连接。最后两项仅记录组装状态。默认在 298.15 K 下启用 Davies 活度近似：按完全解离假设，左右侧离子强度约为 0.04 和 0.40 mol/kg，含活度修正的理论电压约 22.7888 mV；关闭活度修正时，理想能斯特基线约 29.5797 mV。两者都是仿真值，不是实测，也不含液接电势。

## 电化学计算与校正

当前浓差电池的极性定义为“右侧减左侧”。能斯特计算入口使用

```text
E = RT/(nF) × ln(a_right/a_left)
a_i = γ_i × (c_i/c°)
```

其中 `a` 是无量纲活度；活度修正关闭时 `γ=1`，保留原来的理想结果。IUPAC 对能斯特方程以活度表述，并指出活度系数是溶液非理想性的修正量（[IUPAC 能斯特方程](https://goldbook.iupac.org/terms/view/09068)、[IUPAC 活度系数](https://goldbook.iupac.org/terms/view/A00116)）。

活度修正当前可选 Davies 近似：

```text
log10(γ_i) = -A z_i² × (√I/(1+√I) - 0.3I)
I = 1/2 × Σ(m_i z_i²)
```

默认 `A=0.509` 仅对应约 25 °C 的水溶液；当前实现把离子强度限制在 0–0.5 mol/kg，并将单离子活度系数视为约定性近似。当前暂定 CuSO₄ 情景由状态追踪 Cu²⁺ 与 SO₄²⁻，用其浓度近似 molality 来动态计算两侧离子强度；该状态账本仍未追踪溶剂质量。未知真实配方时，这些必须视为可替换的仿真假设，而非实验事实。Debye–Hückel/Davies 活度系数的适用性和单离子约定见 [IUPAC Debye–Hückel 条目](https://goldbook.iupac.org/terms/view/D01534)。

液接电势单独作为第三项校正。模型从实验条件读取左右溶液的离子组成/状态浓度、盐桥电解质浓度与化学计量数，再从迁移参数库查各离子的迁移率；可选择 Henderson 近似或沿预设线性浓度剖面对一维 Nernst–Planck 零电流电场关系做数值积分，分别计算两处液接界面，并将其电势差加到活度修正后的能斯特电压上。各界面电势正号定义为从左向右电势升高。Henderson 适合快速估算连续/自由扩散液接，但依赖近似线性浓度梯度、近似恒定迁移率和近似理想溶液等条件；完整 Nernst–Planck 可在给定输运参数、剖面和边界条件后处理更一般的通量/浓度剖面。本项目当前数值路线只对预设线性浓度剖面做零电流积分，不求完整稳态各离子通量或 Poisson 空间电荷，所以不能据此宣称比 Henderson 更准确。两种理论方法都没有通用的单一浓度阈值；需要根据体系组成、参数来源和液接结构判断。

当液接参数可用并成功计算时，结果会同时记录 Henderson 与简化 Nernst–Planck 两个方法的总修正和分界面值，并在页面展示同条件差值。两者共用相同离子参数与线性浓度剖面，因此该差值用于检查数值积分是否与闭式近似一致，不是精度排名，也不构成实验验证；电压实际采用配置中选定的方法。

离子迁移参数库目前收录 Cu²⁺、SO₄²⁻、K⁺、NO₃⁻ 的 25 °C 水溶液无限稀释极限扩散系数，迁移率由 `uᵢ=|zᵢ|FDᵢ/(RT)` 换算。当前参数温度参考估算覆盖 0–95 °C，但不是各离子全温区直接测量，且不匹配有限浓度混合液接；盐桥为 KCl 或 NaNO₃ 时还因 Cl⁻、Na⁺缺少迁移参数而不能计算。因此默认策略仍是“不用于数值修正”。若在配置中显式将 `parameter_use_policy` 设为 `allow_reference_estimate`，程序可使用这些值进行探索性估算，结果会标记 `estimated_from_reference_parameters`、`condition_matched=false`，不能视为精确预测或实验验证。温度超范围、参数库缺少某个离子、组成不电中性或电荷定义不一致时会拒绝计算。参考：[CRC Handbook 无限稀释离子参数表](https://is.muni.cz/el/sci/podzim2016/C4020/um/pom/Ionic_Conductivity_and_Diffusion_at_Infinite_Dilution.pdf)、[White《Geochemistry》表 5.3](https://www.eps.mcgill.ca/~courses/c220/WMWhiteTextBook/Chapter05.pdf)。

另有有限浓度电解质文献库，收录多种二元水溶液的互扩散系数表点、KNO₃ 的有限浓度阳离子迁移数，以及 CuSO₄ 的表观阳离子迁移数；CuSO₄ 的经典 25 °C 输运数据也登记了来源和待提取状态。不同参数类型严格分开：互扩散系数描述盐整体的浓度驱动扩散，迁移数描述电流分担比例，二者都不能直接冒充单离子电迁移率。当前液接电势计算器仍只接受单离子迁移率，所以这批数据暂不自动改变仿真电压；尤其没有与当前浓度、温度、pH 和混合液接区条件匹配的数据时，不插值、不外推、不宣称已验证。

离子组成按物种 ID 配置，能斯特计算不再限定为铜离子。简单的金属离子/金属电极浓差电池可使用 `cell.active_species`；更一般的同一氧化还原对可使用 `cell.redox_reaction` 提供半反应、电子数和反应商项。

### 通用半反应配置

在 `reaction_quotient_terms` 中，溶液产物的指数填正数、反应物的指数填负数；电子、纯固体和纯液体不写入。程序按

```text
E_cell = E_right - E_left = -RT/(nF) × ln(Q_right / Q_left)
Q = Π a_i ^ ν_i
```

计算。左右两侧使用同一半反应，因此标准电势相消。配置示意：

```json
"redox_reaction": {
  "name_zh": "Fe³⁺/Fe²⁺",
  "reduction_reaction_zh": "Fe³⁺ + e⁻ → Fe²⁺",
  "display_species": "Fe3",
  "electrons_transferred": 1,
  "reaction_quotient_terms": {"Fe3": -1, "Fe2": 1}
}
```

对 `Zn²⁺ + 2e⁻ → Zn(s)`，反应商只包含溶液中的 `Zn²⁺`，可设 `reaction_quotient_terms: {"Zn": -1}`、`electrons_transferred: 2`。同时要在 `species`、母液/容器组成、两侧映射、电极和盐桥配置中提供一致的物种和实验条件；若启用活度修正，还要列出组成离子与电荷。页面会读取节点返回的活性物种名称，不再固定显示 Cu²⁺。

这不是“任何电化学实验都自动可算”：当前求解范围是左右两侧采用相同半反应的浓差电池。不同左右半反应、复杂络合/沉淀/酸碱平衡、真实电极动力学仍需相应模型。Davies 活度系数及 Henderson 液接电势还受浓度、温度和参数库覆盖限制；缺数据时不能把理想能斯特值说成已验证的真实电压。

## 本地运行

在终端进入该目录后：

```powershell
cd 'D:\电化学数字孪生\electrochemistry_simulation'
python -m electrochem_sim
```

运行测试：

```powershell
python -m unittest discover -s tests -v
```

更换配置或随机种子：

```powershell
python -m electrochem_sim --config config/copper_concentration_cell.json --seed 42
```

运行非铜锌体系示例：

```powershell
python -m electrochem_sim --config config/zinc_concentration_cell.json --seed 42
```

如果系统没有可用的 `python`，需使用实际安装的 Python 可执行文件；不要把这些本地路径直接当成 Bohrium 节点路径。以后部署节点时可以复制整个目录，本次未执行远端部署。

## 操作与误差规则

- JSON 中液体体积用 mL、操作体积用 μL；内部用 L、mol、K、V。
- `dispense` 未写 `volume_ul` 表示排尽当前枪头；填写体积时支持一次吸液多次分液。
- 吸液需要装枪头且源液已混匀；加液后保留各次加入液体的概念分区。混匀按效率缩小各分区与总体积加权平均浓度的差异，默认效率为 100%。
- `error_model.enabled=false` 为理想操作。当前档案给移液提供来源型参考范围，给枪头残液和混匀提供可编辑的工程先验；后二者不是当前节点/实验设备的校准值。
- 当前移液模型按单次目标体积自动选型：20–200 μL 选择 P200（100–200 μL 重叠区也优先 P200），>200–1000 μL 选择 P1000。区间内支持任意有限精度设定值，不限于规格锚点；相邻厂家锚点间对系统误差限和重复性尺度按绝对 μL 分段线性插值，不在锚点外推。1000 μL 以上总剂量拆成最少次数、等量的量程内移液；单次剂量小于 20 μL 拒绝，不虚构扩大量程。
- 新模型的系统误差以厂家最大允许误差幅度为上界，每种移液器每轮共享一个带符号潜变量；逐次重复性以厂家随机误差规格作为高斯尺度近似。两者都不是实测概率分布。低档使用厂家参考边界，中/高档将两项误差尺度分别乘 1.5/2.0，这是项目情景设定，不是文献分档。误差关闭时仍记录所选枪型与规格来源，但不注入偏差。
- `tip_carryover.residual_volume_ul` 表示排液后留在枪头中的正体积；液体及其溶质按物料守恒记账，若复用枪头才可能将前一液体带入下一容器。它不是电压噪声。当前从配置范围内按该档位自己的先验均值/标准差作截断高斯抽样，每轮共享并记入运行记录；参数是工程先验。
- 混匀效率随动作持续时间计算：`η(t)=1−exp(−k·t)`。启用时间速率模型时，每轮从所选档位的有界截断高斯先验抽取一次速率 `k`，并在该轮混匀动作持续时间 `t` 下计算效率；默认 `t=10 s` 以兼容旧条件，也可在单次或批量条件中设置扫描。运行记录同时保存设定时长、抽样速率和由此得到的效率。现有 10 秒效率情景只是转换成 `k` 的工程先验，不代表当前装置实测或校准；不完全混匀不会阻止平均浓度理论电压计算。
- 误差类别、模型、档案和档位是不同概念：类别指误差来源；模型指产生机制；档案记录适用条件、参数和证据来源；档位是在同一模型下选择的误差强度。配置通过 `profile_file` 选择档案，并为有参数依据的类别选择 `model` 与 `level`。
- 当前误差曲线引用 Gilson PIPETMAN P200/P1000 厂家规格和推荐 D200/D1200 枪头。该曲线只是在厂家离散性能点之间构造的可追溯参考情景，不是机器人、虚拟枪头、当前抓取/排液动作或液体配方的实测校准。D1200ST 枪头架容量不扩大 P1000 的 100–1000 μL 移液量程；P200 使用 D200 枪头。当前仿真动作会记录 P200/P1000 的语义选择，但 Matterix 场景仍需后续核实真实枪型模型、枪头切换、枪头架与抓取动作；本轮未做云端视频验收。
- 约500 µL的重复性参考范围为低档CV 0.20–0.40%、中档0.40–1.00%、高档1.00–2.00%。证据锚点包括Tecan Freedom EVO 200水液体类别/PBS实测CV 0.31%、0.62%、0.89%；Opentrons OT-2厂家CV≤0.20%；Eppendorf epMotion CV≤0.30%；Accuris AutoMATE 96 CV≤0.40%；自动分装应用研究实测CV约1.1–1.2%；Biotage Extrahera HV-5000厂家CV为2.0%。这些值来自不同设备、通道/液路、校准和应用，不是同一条件下的头对头比较，也不是“多数仪器”的市场分位数。档位边界是为仿真定义的参考分层，来源的最大规格不是概率分布。仿真在每次运行开始时从选定CV区间均匀抽取一个CV，再以该CV作为单次吸液高斯重复性误差的标准差比例；均匀抽取和高斯误差形状均为模型约定，不是文献证明的分布。系统偏差按各档来源给出的界限单独建模，不把精密度CV误当准确度。
- 档案库把误差类别、因子、模型、参数档案、档位分层。当前枪头残液低/中/高范围分别为0.05–0.20、0.20–0.60、0.60–1.00 µL；混匀效率范围分别为0.98–0.995、0.90–0.98、0.70–0.90。它们是针对原型的工程暂定情景，不是已知设备性能；可在确认枪头、容器尺寸、混匀方式和循环数后调整。文献条件性参考另存档案，避免和当前暂定范围混淆。
- 当前残液模型采用枪头残液体积；混匀模型把不同次加入的液体视作概念分区，按效率逐步拉近各自浓度。结果同时报告总体平均浓度的均匀等效理论电压，以及由分区浓度极值组合得到的局部情景电压范围。局部范围不是空间地图、电极表面预测或实测值；概念分区也不是 CFD 网格，这不是流体力学模拟。
- 非法操作返回失败记录，不越过失败步骤。参数或初始配置不合法时在运行前报错。

## 目前核验什么

理论基线、等浓度零电势差、极性反转、独立守恒核验、空容器无有效电压、混匀/枪头/组装前提、源液不足、容量超限、失败不修改旧状态、弃置液体去向、残液记账、混匀误差、一次吸液多次分液、误差共享关系、种子复现和结果不覆盖。

其中一个关键对照：母液和水同比例少吸时，体积改变但浓度比可能不变；仅母液少吸时，稀释浓度及电压会改变。

## 模型边界与后续

现阶段假设体积可加、混匀前浓度差仅存在于各次加入液体形成的概念分区、混匀按单一效率同比缩小分区相对总体平均的偏差；铜示例还假设 CuSO₄ 完全解离且总铜浓度近似自由 Cu²⁺ 浓度。活度系数使用 Davies 近似、两侧等温；盐桥液接电势默认不计。无放电、蒸发、电极动力学或仪表误差。局部电压范围是由分区浓度端点构成的情景边界，不表示真实空间梯度。没有真实实验验证，不把程序测试通过称为真实预测准确。

单次结果 JSON 包含运行编号/时间/状态、实验条件快照、每一步操作和状态变化、最终浓度/体积/理论电压及守恒核验；结果标记为 simulated，不是实测。结果按北京时间日期存入 `results/YYYY-MM-DD/`，文件名使用“单/批_实验简称_时间_短编号”。单次简称在配置的 `experiment_name_short_zh` 中维护。不要为了视觉效果自动让开路浓度随时间衰减。

本次模块拆分后，节点部署时需把新的 `config/protocols/` 和新增的模块文件一起上传。为避免漏文件，建议更新整个 `electrochemistry_simulation` 文件夹及 `copper_cell_node.py`，然后重启节点服务。

## 批量浓度扫描

`config/copper_concentration_sweep.json` 定义要比较的左侧目标浓度。批处理入口 `electrochem_sim/batch.py` 复用同一套单次仿真逻辑，固定右侧条件、温度及左侧总体积，自动计算各情景需要加入的母液和水体积；母液浓度情景会自动省略加水动作。

运行：

```bash
python -m electrochem_sim.batch
```

默认输出 CSV 汇总和每组完整 JSON 运行记录到 `results/YYYY-MM-DD/`。批量简称在 `config/copper_concentration_sweep.json` 的 `batch_name_short_zh` 中设置；CSV 的 `detail_file` 列给出对应 JSON 明细相对路径。也可通过 `--base-config`、`--batch-config`、`--output-dir` 指定路径。该命令是独立批处理，不会重启或改变节点上的 HTTP 展示服务。

若原仿真模块已经上传到波尔节点，首次增加批量功能时只需上传新增的 `electrochem_sim/batch.py` 和 `config/copper_concentration_sweep.json`，无需再次上传整个项目。后续更改浓度列表只改批量配置，再运行 `python3 -m electrochem_sim.batch` 即可。

## 温度与浓度组合扫描

`config/copper_temperature_concentration_sweep.json` 定义扫描温度和左侧目标浓度。`temperatures_k` 接受任意有限正温度（单位 K），20、25、30、35、40 °C 只是默认扫描点；例如 25.1 °C 填 `298.25`。右侧默认沿用基础实验配置；若同时扫描右侧浓度，可在批量配置中增加 `right_concentrations_m` 列表和 `right_total_volume_ml`。右侧配液由母液和水按目标体积重建。每个温度/浓度组合都从同一基础配置副本生成，随机种子按情景递增。

运行：

```bash
python -m electrochem_sim.temperature_concentration_batch
```

批处理为每组保存完整 JSON 和汇总 CSV 到 `results/YYYY-MM-DD/`。CSV 记录目标/实际浓度、理想能斯特电压、活度与液接电势修正及状态、最终理论电压、步骤完成情况、守恒结果、适用范围说明和 JSON 明细路径。液接启用且可计算时，还记录本轮模型、Henderson 与简化 Nernst–Planck 的各自结果、两者差值及其适用范围说明；关闭或参数不可用时这些模型数值留空并保留状态，避免把未计算当成零。差值仅核对共享假设下的数值积分，不是精度比较。现有 `python -m electrochem_sim.batch` 和 `config/copper_concentration_sweep.json` 的接口与行为保持不变。

更新后的网页在“批量仿真曲线”中可切换“浓度扫描”“温度 × 浓度”和“自选条件组合”。自选模式可直接在网页填写扫描条件、查看组合总数并启动后台批量仿真；完成后自动刷新曲线和条件表。节点通过对应的 `/sweep?type=...` 接口读取最近生成的批量 CSV；网页和节点使用同一 `results` 目录即可查看。

温度×浓度批处理会在每个情景配置副本中，按 OECD/NEA 热力学数据库的水溶液 Debye–Hückel A 温度表更新 Davies 参数。数据覆盖 0–50 °C、1 bar；表格点之间使用线性插值，并在汇总和情景明细中记录来源、方法及参数值，范围外不外推。25 °C 的表值 0.509 与基础配置一致。默认温度网格为 20、25、30、35、40 °C；批量 JSON 可在 0–50 °C 范围内指定其他温度。基础配置文件及现有浓度扫描行为不变。

液接修正默认关闭。若显式设置 `parameter_use_policy` 为 `allow_reference_estimate`，25 °C 无限稀释迁移率锚点可在 0–95 °C 内按理想 Walden 关系 `lambda·η≈常数` 进行水黏度比例估算；纯水黏度由 IAPWS SR6-08(2011) 的 0.1 MPa 关联式计算，并归一化到 25 °C 锚点。输出明确标记为“非条件匹配参考估算”：温度修正不是 Cu²⁺、SO₄²⁻、K⁺、NO₃⁻ 各自全温区逐点实测，也不匹配有限浓度混合液接条件；真实离子的 Walden 乘积可能随温度变化。0–95 °C 范围外拒绝液接估算、不外推；未显式允许参考估算时仍按现有策略等待条件匹配参数。Davies 活度参数的 0–50 °C 覆盖范围独立于液接迁移参数范围，因此 50–95 °C 可计算液接温度参考估算，但活度参数仍需单独匹配或关闭活度修正。

## 节点服务的作用与部署目录

新版节点脚本只负责调用总控、保存结果和提供 HTTP 数据，不再自行写一套电压公式或固定浓度。

启动时执行配置中的流程一次，随后只读提供终态：

- `/health`：服务及仿真状态。
- `/state`：兼容旧网页的两侧浓度、体积、温度和电压，附运行编号及步骤数。
- `/steps`：每个步骤的操作事件与前后状态。
- `/run`：完整运行记录。
- `/comparison`：当前运行的误差抽样/逐步移液明细，以及同条件关闭误差后的理论基准；网页连接时读取一次，不会重复执行当前协议。
- `/sweep`：只读读取 results 中最近一次批量浓度扫描 CSV，供网页表格和曲线显示；没有结果时返回空状态。
- `/sweep?type=temperature_concentration`：只读读取最近一次温度×浓度批量 CSV；不传 `type` 时保持读取原浓度扫描结果。
- `/sweep?type=experimental_design`：只读读取最近一次自选条件组合 CSV，包含每组温度、左右浓度、盐桥、修正开关、误差档位与重复编号。
- `POST /batch/start`、`GET /batch/status`：本地网页启动并查询自选条件组合任务；后台运行期间同一节点只接受一项批量任务。
- `/history`：只读读取最近保存的最多 200 条运行记录摘要，网页可勾选 2–5 条进行多轮条件与结果比较；不会重新运行仿真。对液接修正，还会列出本轮所选模型、Henderson 与简化 Nernst–Planck 的结果及差值，并附带两种模型的适用范围说明。较早运行记录没有保存双模型对照时，页面会标明无该记录数据，不会事后补算。

网页重复查询不会重复移液。默认协议很快完成，目前网页显示最终状态，不是逐步动画同步。`elapsed_s` 表示服务运行时间，不是电化学稳定或放电时间；失败仿真的 `/state` 返回 HTTP 409，不能当成成功的 0 mV。

## 自选实验条件组合批量仿真

若不只扫描温度和浓度，可在网页表单或 `config/copper_experimental_design_sweep.json` 里选择扫描轴：温度、左/右侧目标浓度、盐桥种类与浓度、活度修正开关、液接修正开关与模型、操作误差开关，以及移液/枪头残液/混匀各自的低/中/高档位。表单中每格填一个值表示固定，填多个值表示做全组合；留空沿用当前节点条件。网页温度单位为 °C，浓度单位为 M。右侧体积扫描时可填写 `right_total_volume_ml`，留空沿用当前体积；当前不支持体积本身作为扫描轴。

运行：

```bash
python -m electrochem_sim.experimental_design_batch
```

每个条件组合可指定重复次数。默认最大组数为 500（最高可设 5000），超过上限会在开始仿真前拒绝运行。CSV 与每轮详细 JSON 保存到 `results/YYYY-MM-DD/`；重复结果在曲线中按同一条件与浓度汇总为平均值，竖线显示重复间最小—最大值，不是置信区间。网页最多绘制 12 组曲线，完整数据见表格。命令行批处理保留作为备用入口。

本地测试服务（先确认没有旧进程占用端口）：

```powershell
python '..\copper_cell_node.py' --host 127.0.0.1
```

波尔节点需要一并上传文件，目录保持为：

```text
/personal/
  copper_cell_node.py
  electrochemistry_simulation/
    electrochem_sim/
    config/copper_concentration_cell.json
    config/protocols/copper_concentration_cell_stock_dilution.json
    results/
```

上传整个 `electrochemistry_simulation` 文件夹最简单；`tests` 也可保留。只上传 `copper_cell_node.py` 不够。确认旧服务已停止后，在节点运行：

```bash
python3 -u /personal/copper_cell_node.py
```

要在节点运行锌示例，启动时显式传入其配置（并确保锌配置及对应 protocol 已上传）：

```bash
python3 -u /personal/copper_cell_node.py --config /personal/电化学数字孪生/electrochemistry_simulation/config/zinc_concentration_cell.json
```

此命令在前台运行，可先查看启动信息。需要后台运行时再使用：

```bash
nohup python3 -u /personal/copper_cell_node.py >/tmp/copper-cell-node.log 2>&1 </dev/null &
```

两种启动方式任选一种，不能同时占用同一个端口。页面地址仍填写原节点地址与 50005 端口；不要在本地浏览器把节点的 `127.0.0.1` 当成远端地址。更改 JSON 配方后，需要重启脚本生成新的一次运行。
