# 部署与访问

## 1. 发布前端

本仓库的 `.github/workflows/pages.yml` 从首页源文件和既有工作台构建 `dist/`，仅上传该目录到 GitHub Pages。

仓库 Settings → Pages → Source 选择 GitHub Actions。推送 main 后执行工作流，网站地址：

<https://yangsen-hub-dot.github.io/electrochem-digital-twin/>

个人电脑关机后，已发布的前端仍可访问。前端未配置任何个人服务器、真实访问码或运行录像。

域名尚未购买或绑定。候选 `electrochem-twin.science` 仅为命名建议，未确认可注册。注册并完成 DNS 配置后，再在 Pages 设置 Custom domain；请勿提前添加未持有域名的 CNAME。

## 2. 使用自己的实验节点

首页「节点设置」保存名称、API 地址、实验类型和可选录像地址。节点地址只保存在访问者自己的浏览器；访问码仅在该浏览器会话内保存。

公网网站使用 HTTPS，因此对接的节点也应提供 HTTPS。`127.0.0.1` 始终指访问者自己的电脑；本地 HTTP/SSH 转发节点请配合本地前端使用。

允许来源填写网站的 **origin**，例如 `https://yangsen-hub-dot.github.io`，不要包含 `/electrochem-digital-twin/` 路径。

## 3. 数值节点：普通 Python 环境

推荐 Python 3.11。数值核心、HTTP 服务与前端构建使用标准库，无需 GPU。

先在部署机器生成访问码文件：

```bash
python -c "from pathlib import Path; import secrets; p=Path('var/access-token.txt'); p.parent.mkdir(exist_ok=True); p.write_text(secrets.token_urlsafe(32))"
```

该文件保留在运行环境，不提交 GitHub。

浓差电池节点：

```bash
python copper_cell_node.py --host 127.0.0.1 --port 50005 --output-dir var/concentration --token-file var/access-token.txt --allowed-origin https://yangsen-hub-dot.github.io
```

HMF 数值节点：

```bash
python integration/web/hmf_local_server.py --host 127.0.0.1 --port 8766 --history-dir var/hmf-history --token-file var/access-token.txt --allowed-origin https://yangsen-hub-dot.github.io
```

通过自己的 HTTPS 反向代理发布服务；`deployment/Caddyfile.example` 使用两个示例子域名分别代理上述端口。域名、证书和云平台的端口配置由部署者完成。在前端填写 HTTPS 地址与访问码。

可选容器部署见 `deployment/compose.yaml`。它运行相同 Python 数值服务，GPU/Matterix 不包含在该镜像内。

## 4. Matterix 云端仿真节点

安装与原工程匹配的仿真环境和资产，见 [依赖](dependencies.md)。原协调器、媒体服务、过程检查、状态账本与执行器继续复用。

```bash
python integration/coordinator/cloud_experiment_server.py --engine-root electrochemistry_simulation --runner integration/first_transfer/matterix_first_transfer.py --worker-python /path/to/isaac/python --runs-root var/runs --jobs-root var/jobs --token-file var/access-token.txt --host 127.0.0.1 --port 50007 --allowed-origin https://yangsen-hub-dot.github.io
python integration/media_service/cloud_video_server.py --root var/runs --token-file var/access-token.txt --host 127.0.0.1 --port 50006 --allowed-origin https://yangsen-hub-dot.github.io
```

实验 API 与录像服务分别配置 HTTPS 入口。实际 Matterix 路径及运行环境应按部署者的安装核对；当前发布不会访问、重启或更新原有云端仿真实例，也不会自动提交实验。

## 5. 多节点行为

当前前端按用户选择连接一个节点；各节点独立运行、保存历史和录像。切换节点后重新读取其结果。当前没有跨服务器自动分配批次、迁移中途任务或统一汇总全节点历史；这些需要后续在既有协调逻辑上继续扩展。

网站可公开浏览，计算必须由使用者配置后端并显式启动。HMF 数值结果不显示为三维机器人实验或仪器实测。
