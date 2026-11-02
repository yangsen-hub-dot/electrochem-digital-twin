# 行星弧光：公网全站主题

本次主题采用用户选择的第二张“行星弧光”，覆盖首页上半部和实验工作台。首页中的自动化实验室素材继续融合显示，下面的模型区域保留此前认可的银河星带。

工作台、校验与修复、结果分析、批量实验、历史记录共用 `frontend/assets/workbench-space.css`。两种反应模型共用同一主题：表单、导航、状态区、数据面板与比较表统一调整。后台计算、节点请求、实验条件与运行记录的逻辑继续使用原控制器。

## 文件

- `frontend/assets/planet-horizon.webp`：行星弧光背景；由内置 imagegen 生成的装饰性概念素材，不作为天文观测资料。
- `frontend/assets/home.css`：首页行星背景与实验室层的融合。
- `frontend/assets/workbench-space.css`：所有工作台视图共用的主题覆盖。
- `frontend/assets/fonts/home-fonts.css` 及对应字体子集：首页和内页的中文、英文、数字与单位字形。
- `scripts/build_site.py`：在公网工作台文档末尾加载新主题，不修改本地权威工作台源文件。

曲线画布继续使用浅色绘图区，以保留原有科学曲线、范围线与坐标文字的对比度；实验录像与同帧预览不叠加装饰图层。

## 生成提示概要

> A panoramic blue planet enters from the far right, with a fine silver-blue atmospheric rim, subtle cloud texture and a sparse indigo star field. Calm left side for typography, luminous upper-right edge, refined photographic space aesthetic. No continents, text, spacecraft, galaxy band or watermark. Decorative artwork for the hero of a scientific digital-twin website.

发布源使用独立公网副本。本地正在测试的工程、SSH 隧道、云端实验服务和运行流程不属于此次改动。
