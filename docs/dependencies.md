# 第三方环境与资产

数值引擎不依赖 GPU。Matterix、Isaac Lab、Isaac Sim 和 ChemLab 资产属于独立依赖，不将本地第三方参考目录、大型资产和缓存整体上传本仓库。

## 原工程的两套兼容配置

- `isaacsim5_isaaclab2`：Isaac Sim 5.0、Python 3.11、Isaac Lab 2.2.0；Matterix 固定提交 `51f2070c3f54a64e974dbc8e6fd3b309019c1b94`，对应 `integration/reference/MatterixIsaacLab2`。
- `isaacsim6_isaaclab3`：Isaac Sim 6.0.1.0、Python 3.12、Isaac Lab 3.0.0b2.post1；Matterix 提交 `687e2cef294f00e86dbb32173fefd93b50e5107f`，对应 `integration/reference/Matterix`。

这些是本地既有工程所记录的配置，不代表任意 GPU/驱动组合均已验证。选择与自己部署环境一致的一套，遵循上游安装说明，避免混装。

上游：

- Matterix：<https://github.com/ac-rad/Matterix>
- Isaac Lab：<https://github.com/isaac-sim/IsaacLab>
- ChemLab-Assets：<https://github.com/mbzuai-nlp/ChemLab-Assets>
- ChemLab 数据：<https://huggingface.co/datasets/MBZUAI/ChemLab-Assets>

既有 `integration/deployment/build_runtime_bundle.py` 根据所选配置，从已经安装/准备好的参考目录打包运行依赖。GPU 工作流仍需原工程相应的资产转换、尺寸/坐标配置与设备环境；仅克隆本仓库不会自动取得这些大型依赖。

第三方内容遵守各自许可证、署名和分发要求。本仓库的首页图为自行绘制的原理示意，未复制参考网站的图片或代码。
