# 自动化电化学实验数字孪生

连接实验设计、虚拟/真实工作流、实验状态、电化学响应、仪器观测与误差感知反馈。

**网站：<https://yangsen-hub-dot.github.io/electrochem-digital-twin/>**

本仓库从已有本地工程整理，复用浓差电池计算引擎、状态账本、工作台控制器和 Matterix 集成。首页使用深色科研风格；网站访问者可以保存并选择自己部署的实验节点。

## 代码分类

```text
frontend/                       平台首页、公网连接适配与工作台主题
电化学浓差电池_轻量可视化原型.html  既有工作台源文件；保留原元素 ID 与控制器
copper_cell_node.py              浓差电池数值 HTTP 适配器
electrochemistry_simulation/
  electrochem_sim/               化学、误差、状态账本与数值流程
  config/                       条件、模型与参考参数配置
  tests/                        原有数值测试
integration/
  web/                          HMF 服务、条件/历史与既有网页控制器
  coordinator/                  云端任务、批次、运行协调
  first_transfer/               Matterix 执行、场景和物理约束
  media_service/                认证录像回放
  live_preview/                 同帧状态与预览
  process_validation/           过程检查及受约束局部修复
  device_adapters/              设备适配契约
scripts/                        前端构建与本地启动
deployment/                     通用部署配置
docs/                           部署、模型与结构说明
.github/workflows/              仅发布前端的 Pages 工作流
```

第三方仿真框架和大型资产单独安装，见 [依赖说明](docs/dependencies.md)。运行录像、历史档案、密钥、个人服务器配置不包含在仓库中。

## 本地使用

数值核心与本地网页服务使用 Python 标准库，推荐 Python 3.11；GPU 仿真另有依赖。

```bash
git clone https://github.com/yangsen-hub-dot/electrochem-digital-twin.git
cd electrochem-digital-twin
python scripts/start_local.py frontend
```

手动访问 `http://127.0.0.1:8080`。程序不会自动打开浏览器。

另一个终端启动浓差电池数值节点：

```bash
python scripts/start_local.py concentration
```

在工作台连接设置中填 `http://127.0.0.1:50005`，点击连接。该模式计算数值结果，不生成机器人录像。

HMF 数值模式：

```bash
python scripts/start_local.py hmf
```

访问 `http://127.0.0.1:8766/workbench.html?model=oxidation`，沿用原 HMF 数值流程。记录保存在 `var/hmf-history`。

## 公网使用与复用

GitHub Pages 仅发布 `scripts/build_site.py` 生成的前端。使用者在自己的服务器运行后端，通过 HTTPS 暴露受认证 API，将 `https://yangsen-hub-dot.github.io` 加入允许来源，然后在网站填写节点地址。

详细步骤见 [部署](docs/deployment.md)、[模型适用范围](docs/models.md) 和 [节点接口](docs/node-contract.md)。自定义域名尚未购买或绑定；默认 Pages 地址可直接使用。

## 当前研究状态

- 浓差电池：有数值核心、操作误差、状态/回执、批量、历史与 Matterix 原型。
- HMF → FDCA：已有独立数值流程与文献条件下的终点预测，机器人三维流程未完成。
- 真实仪器测量、实体机器人校准和完整误差感知闭环仍需后续研究。

项目中的数值预测、模拟执行和真实仪器观测分别标明来源。网页上线不代表物理模型或真实实验已经验收。

## 源码使用

本仓库自有代码使用 [MIT License](LICENSE)，便于研究复用与自部署。第三方框架/资产遵循各自原始许可证。
