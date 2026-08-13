# Weather Agent

基于 OpenClaw 的单 Agent 天气助手。当前基线固定为 OpenClaw `2026.7.1-2`，这是 2026-08-12 npm `latest` 对应的稳定版本；不采用 `2026.8.1-beta.1`。

这个仓库不再实现独立的 Agent 服务、融合服务或自建记忆/调度系统：

- OpenClaw 负责唯一 Agent、大模型规划、长期记忆、定时任务、主动消息、飞书渠道和 `web_search`。
- 本仓库只实现两个平级工具：`qweather_query` 与 `openmeteo_query`。
- Agent 根据问题选择一个或多个来源，并直接理解各来源的原生数据。
- 不做标准化、加权、平均或隐藏式融合；来源冲突时由 Agent 明确说明。
- 后续气象源继续以新的平级工具接入。
- 用户明确要求图片时，Agent 可以调用隔离在本机的 `image_generate`；它不参与天气数据判断。

记忆增强完全使用 OpenClaw 原生能力：本地 llama.cpp Embeddings 建立语义索引，`Active Memory` 仅在私聊回复前召回相关偏好，`Dreaming` 每天 `03:00`（`Asia/Shanghai`）整理证据并将稳定事实晋升到长期记忆。它不会自动修改代码、工具或运行配置。

架构说明见 [docs/architecture.md](docs/architecture.md)。

## 目录

```text
config/                         OpenClaw 配置参考
config/automations/             OpenClaw cron 声明与任务提示词
config/workflows/               固定的本地图片生成工作流
plugins/weather-query-tools/    两个天气查询工具
workspace/                      唯一 Agent 的工作区、回答模板与记忆
scripts/setup.ps1               本地 profile 配置脚本
scripts/setup-automations.ps1   校验并幂等同步天气定时任务
scripts/setup-image-generation.ps1  安装本地图片运行时、模型与后台任务
scripts/run-comfyui.ps1         后台图片服务入口
```

## 环境要求

- Node.js `24.15+`
- OpenClaw `2026.7.1-2`
- 一个可用的大模型账号
- 和风天气专属 API Host 与 API Key
- Tavily API Key
- 一个飞书自建应用
- 可选：NVIDIA RTX 40/50 系列显卡；当前图片基线按 8 GB 显存配置

确认版本：

```powershell
node --version
openclaw.cmd --version
```

## 首次搭建

1. 配置独立的 OpenClaw profile 与模型：

```powershell
openclaw.cmd --profile weather-agent onboard --workspace "$PWD\workspace"
```

2. 把 `.env.example` 中需要的变量写入 profile 的全局 `.env`。使用 `--profile weather-agent` 时，Windows 默认位置是：

```text
C:\Users\<you>\.openclaw-weather-agent\.env
```

不要把真实密钥放进仓库或 `workspace/`。

3. 安装官方飞书、Tavily 与本地 llama.cpp Embeddings 适配器，构建并链接天气插件，同时写入最小工具权限和原生记忆配置：

```powershell
.\scripts\setup.ps1
```

首次执行记忆状态检查时，OpenClaw 会下载默认的本地 EmbeddingGemma GGUF 模型；之后索引和查询均在本机运行。

如果需要本机图片生成，先安装 ComfyUI `0.32.0` 与 Z-Image-Turbo NVFP4/FP4 权重，并注册登录启动任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-image-generation.ps1
```

下载量约 10 GB（运行时压缩包约 2 GB，模型约 7.8 GB）。安装脚本会逐个校验文件大小与 SHA-256；服务只监听 `127.0.0.1:8188`，禁用第三方节点、云端 API 节点与提示词元数据。首次安装后再执行 `scripts/setup.ps1`，即可把 OpenClaw 的 `image_generate` 指向仓库中的固定工作流。

4. 参考 `config/openclaw.example.json5` 写入模型与飞书配置。飞书保持私聊 `pairing`、群聊 `allowlist`，只填写审核过的目标群；公开搜索固定使用 Tavily。模板中的密钥全部使用环境变量或 SecretRef。

5. 启动并检查：

```powershell
openclaw.cmd --profile weather-agent gateway run
openclaw.cmd --profile weather-agent doctor
openclaw.cmd --profile weather-agent channels status
```

6. 配置并同步定时简报：

在 profile 的 `.env` 中填写已审核的飞书群目标和报告范围。目标顺序固定且会去重；第一项使用 OpenClaw cron 原生投递，其余项由同一次 Agent 运行通过 OpenClaw 原生 `message` 工具同步相同报告，不会重复生成三份内容：

```dotenv
WEATHER_SCHEDULE_FEISHU_TARGETS=chat:replace-with-reviewed-group-1,chat:replace-with-reviewed-group-2
WEATHER_SCHEDULE_LOCATION=全国
```

Gateway 运行后执行：

```powershell
.\scripts\setup-automations.ps1
openclaw.cmd --profile weather-agent cron list --all
```

脚本使用稳定的 `declarationKey` 幂等更新三条全国电力交易气象任务：09:00 全国晨报、16:30 变化复核、17:00 次日预报。全国任务先筛查七大区域，再精查 6～10 个重点省份，将天气翻译为负荷、光伏、风电和净负荷的方向性影响；不预测电价或 MW。16:30 只有相较晨报出现实质变化时才向所有目标群发送，没有变化时返回 `NO_REPLY`。如果没有配置发送目标，任务会被创建为禁用状态且不投递，防止误发。旧的单目标变量 `WEATHER_SCHEDULE_FEISHU_TARGET` 仍可兼容读取，但新配置统一使用复数变量。

只校验声明、不修改 OpenClaw 时：

```powershell
.\scripts\setup-automations.ps1 -ValidateOnly
```

图片服务状态可以这样检查：

```powershell
Get-ScheduledTask -TaskName "OpenClaw ComfyUI (weather-agent)"
Invoke-RestMethod http://127.0.0.1:8188/system_stats
```

图片仅在用户明确要求时生成。它属于视觉创作，不是气象观测、雷达图、卫星云图或预警证据。
当前固定工作流输出 `1024×1024` 单图；OpenClaw 工具中的其他尺寸或宽高比参数不会改变这个工作流。

## 开发验证

```powershell
npm.cmd ci --prefix plugins/weather-query-tools
npm.cmd test --prefix plugins/weather-query-tools
npm.cmd run plugin:validate --prefix plugins/weather-query-tools
.\scripts\setup-automations.ps1 -ValidateOnly
```

测试不会请求真实气象 API，也不需要密钥。

## 参考资料

- [OpenClaw Getting Started](https://docs.openclaw.ai/getting-started)
- [OpenClaw Tool Plugins](https://docs.openclaw.ai/plugins/tool-plugins)
- [OpenClaw Feishu channel](https://docs.openclaw.ai/channels/feishu)
- [QWeather API Host](https://dev.qweather.com/en/docs/configuration/api-host/)
- [QWeather Weather API](https://dev.qweather.com/en/docs/api/weather/)
- [Open-Meteo Forecast API](https://open-meteo.com/en/docs)
- [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api)
- [ComfyUI](https://github.com/Comfy-Org/ComfyUI)
- [ComfyUI Z-Image-Turbo workflow](https://docs.comfy.org/tutorials/image/z-image/z-image-turbo)
- [Z-Image](https://github.com/Tongyi-MAI/Z-Image)
