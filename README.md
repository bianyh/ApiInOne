# ApiInOne

`ApiInOne` 是一个面向个人或小团队自托管的 OpenAI HTTP API 中转站。

后端使用 `Flask`，前端管理台直接由后端提供 `HTML + CSS + JavaScript` 页面，不依赖 React，也不需要 Node、Vite 或打包流程。服务启动后，同一个进程同时提供：

- `/v1/**`：OpenAI 风格 HTTP API 中转入口
- `/admin`：带登录保护的管理后台

你只需要把客户端里的 `base_url` 和 `api_key` 改成这个中转站，即可统一接入多个上游模型节点。

## 特性

- 兼容 OpenAI 风格 `/v1/**` HTTP 接口
- 支持普通 JSON 请求、SSE 流式响应、文件上传和文件下载
- 支持多个上游 `base_url + api_key`
- 支持按优先级有序故障切换
- 支持按测速优选可用节点
- 支持高优先级节点超时后自动切换到下一个节点
- 支持每 60 秒自动探活超时节点，恢复后自动切回高优先级节点
- 支持上游模型自动探查，并把模型列表存储到本地配置中
- 支持代理 Key 管理，客户端永远只使用代理 Key，不直接暴露上游 Key
- 支持请求明细、调用次数、调用速率、Token 数据、延迟数据和健康状态图表
- 不使用数据库，全部使用本地文件持久化
- 前端直接依赖 CDN 的 `Vue 3 + Element Plus + ECharts`

## 管理台功能

管理台默认包含以下页面：

- `总览`
  - 调用总数、成功失败数、平均耗时、平均首字耗时
  - 请求趋势、状态分布、热门模型、节点延迟图表
- `上游节点`
  - 新增、编辑、删除上游节点
  - 自动探查模型
  - 展示已探查模型列表
  - 拖拽调整节点优先级
- `代理 Key`
  - 创建客户端使用的中转 Key
  - 启用 / 停用 Key
- `调用明细`
  - 查看模型、Key、节点、输入 Tokens、缓存 Tokens、输出 Tokens、推理 Tokens、总 Tokens、耗时、首字耗时、缓存比率等字段
- `健康测速`
  - 查看主动探活结果
  - 查看回复摘要
  - 查看失败原因
  - 查看超时切换状态和恢复情况

## 路由与故障切换逻辑

支持两种路由模式：

- `ordered_failover`
  - 按节点顺序依次尝试
  - 高优先级节点超时或首字节前失败时，自动切到下一个节点
- `fastest_first`
  - 优先使用健康且被动首字耗时更低的节点

当高优先级节点因为超时被切走后：

1. 后续请求会优先走下一个可用节点
2. 后台会每 60 秒对被切走节点自动探活
3. 探活成功后，该节点会自动恢复为可用
4. 如果它本来优先级更高，后续请求会重新优先走它

## 前端技术栈

前端不需要单独构建。页面由 Flask 直接返回，浏览器运行时加载：

- `Vue 3`
- `Element Plus`
- `ECharts`

这意味着部署时只需要运行 Python 服务即可访问完整后台。

## 目录结构

```text
.
├─ app.py
├─ relay/                 # Flask 后端核心逻辑
├─ templates/             # 管理台 HTML 模板
├─ static/                # 管理台 JS / CSS
├─ tests/                 # pytest 测试
├─ deploy/systemd/        # Linux systemd 示例
├─ requirements.txt
└─ config.example.yaml    # 开源示例配置
```

## 快速开始

### 1. 创建虚拟环境并安装依赖

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 配置管理员账号和密钥

出于开源安全考虑，仓库默认只提供占位配置。部署前请至少设置以下环境变量：

```bash
export RELAY_ADMIN_USERNAME=admin
export RELAY_ADMIN_PASSWORD='请改成你自己的强密码'
export RELAY_ADMIN_SECRET_KEY='请改成你自己的随机长字符串'
```

Windows PowerShell:

```powershell
$env:RELAY_ADMIN_USERNAME="admin"
$env:RELAY_ADMIN_PASSWORD="请改成你自己的强密码"
$env:RELAY_ADMIN_SECRET_KEY="请改成你自己的随机长字符串"
```

如果没有设置这些环境变量，程序会使用 `relay/settings.py` 里的占位默认值，不适合直接公网部署。

### 3. 启动服务

```bash
python app.py
```

默认监听：

- `http://0.0.0.0:8000`
- 管理后台：`http://<你的服务器IP>:8000/admin`

### 4. 首次使用

1. 登录 `/admin`
2. 添加一个或多个上游节点
3. 点击自动探查模型
4. 拖拽调整优先级
5. 创建代理 Key
6. 让你的其他设备改用本中转站地址

## 客户端接入方式

把原本客户端中的：

- `base_url`
- `api_key`

改成：

```python
base_url = "http://<你的服务器IP>:8000/v1"
api_key = "管理台创建出来的 sk-relay-..."
```

请求体本身保持不变。

也就是说，客户端依旧按 OpenAI 风格去请求，只是把入口地址换成你的中转站。

## 配置文件与数据文件

运行时会自动生成本地文件：

- `config.yaml`
  - 上游节点配置
  - 代理 Key 元数据
  - 全局设置
- `data/requests-YYYY-MM-DD.jsonl`
  - 请求明细
- `data/metrics-YYYY-MM-DD.json`
  - 聚合统计
- `data/object-index.json`
  - 对象归属索引
- `data/health.json`
  - 健康状态与探活结果

这些文件都已经在 `.gitignore` 中忽略，不应该提交到公开仓库。

仓库里提供了一个不含任何真实密钥的示例配置文件：

- `config.example.yaml`

## 上游节点说明

每个上游节点支持的字段包括：

- `name`
- `base_url`
- `api_key`
- `enabled`
- `order`
- `allowed_models`
- `timeout_ms`
- `probe_enabled`
- `endpoint_capabilities`

推荐优先在管理台中创建和修改，而不是手动编辑 `config.yaml`。

## 探活说明

探活逻辑分两步：

1. 请求上游 `/v1/models`，确认模型列表结构有效
2. 用一个 `hi` 的流式 `/v1/chat/completions` 请求确认节点能正常返回有效内容

默认探活模型为 `gpt-5.4`，也可以在前端选择该节点已探测到的其他模型进行探活。

## 已实现兼容范围

当前版本重点覆盖 OpenAI HTTP 风格中转场景，包括：

- `chat.completions`
- `responses`
- `files`
- 常见 `/v1/**` JSON / SSE / 上传 / 下载请求透传

当前版本不包含：

- Realtime
- 自动费用计算
- 限流
- 历史日志自动清理
- 模型名映射

## 测试

运行测试：

```bash
python -m pytest -q
```

## Linux systemd 部署

仓库提供了示例服务文件：

- `deploy/systemd/openai-relay.service`

使用前请根据你的服务器实际路径修改：

- `WorkingDirectory`
- `ExecStart`
- `User`

典型流程：

```bash
sudo cp deploy/systemd/openai-relay.service /etc/systemd/system/api-in-one.service
sudo systemctl daemon-reload
sudo systemctl enable --now api-in-one.service
sudo systemctl status api-in-one.service
```

## 开发说明

- 后端：Flask
- 前端：原生 HTML + CSS + JS，运行时加载 CDN
- 测试：pytest
- 持久化：本地 YAML / JSON / JSONL 文件

## 安全提示

- 不要把真实 `config.yaml` 提交到 Git
- 不要把 `data/` 目录提交到 Git
- 公网部署前必须修改管理员密码和 `SECRET_KEY`
- 建议只在受信网络环境中暴露管理台，或者再额外加一层反向代理认证
