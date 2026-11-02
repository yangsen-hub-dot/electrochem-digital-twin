# 公网首页与视觉资源

2026-11-02 发布的首页沿用用户确认的深色科研门户：自动化实验室概念图融入首屏，银河星图融入模型选择区域。首页标题与英文副标题不带句号；右侧流程演示面板已移除。浓差电池和电催化氧化入口仍进入既有工作台，节点设置使用原连接逻辑。

## 文件与用途

- `frontend/index.html`：正式首页结构与工作台入口。
- `frontend/assets/home.css`：页面布局、背景融合与响应式样式。
- `frontend/assets/home.js`：原有浏览器节点管理逻辑。
- `frontend/assets/lab-digital-twin.jpg`：自动化实验室背景概念图。
- `frontend/assets/galaxy-field.webp`：模型区域银河背景装饰图。
- `frontend/assets/concentration-cell.svg`、`hmf-pathway.svg`：两种实验的原理/反应路径示意。
- `frontend/assets/fonts/`：本地托管的字体子集与对应 OFL 许可；页面不依赖 Google 字体服务器。

图片使用内置 imagegen 生成。实验室图不是项目现有实体实验室或已验收仿真场景的照片；银河图不作为天文测量数据。两者都是网页装饰性概念素材。

## 背景生成提示

实验室图：

> Futuristic automated scientific laboratory for a digital-twin research website, panoramic 2.2:1. Two physically coherent white-and-silver collaborative robotic arms at separate stations; sample vial transfer, precision pipetting, a multichannel liquid handling gantry, organized sample carriers and compact electrochemical testing modules. Bright silver-blue daylight in a spacious research facility. A translucent cyan computational wireframe aligned with one robot and part of the work surface suggests physical/digital correspondence. Calm blue space at the left for a website title. Premium realistic materials and depth. No text, branding, people or floating dashboards.

银河图：

> Photographic Milky Way-style panoramic background, roughly 2.2:1. A luminous finely detailed galactic band sweeps from lower left to upper right through a midnight navy-blue field. Tiny stars, realistic dust lanes and pearly silver-blue light, with restrained desaturated violet. Detail remains visible at the outer edges and between two translucent model-entry panels. No planets, diagrams, labels, grids, network lines, foreground landscape or watermarks. Decorative conceptual image, not measured scientific data.

## 维护

首页通过既有 `scripts/build_site.py` 构建到 `dist/`，由 Pages 工作流发布。字体采用 Google Fonts 上的 Inter、Noto Sans SC、Space Grotesk 的子集，许可文件随字体保留。后续新增静态中文时，可以更新字体子集；缺少的字形会使用系统字体回退。

本次发布范围为首页和静态素材。工作台、后端接口与仿真引擎使用仓库已有实现；本地测试服务与云端仿真进程不属于此次发布操作。
