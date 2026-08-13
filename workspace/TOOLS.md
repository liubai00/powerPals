# Tool Notes

## qweather_query

需要 Gateway 环境变量 `QWEATHER_API_HOST` 与 `QWEATHER_API_KEY`。API Host 必须使用和风天气控制台分配的专属域名。

支持 `current`、`daily`、`hourly`、`warning`。返回值中的 `locationLookup` 和 `data` 均保留和风天气原生字段。

## openmeteo_query

公共非商业接口默认不需要密钥。支持地点名称或经纬度，以及 `current`、`daily`、`hourly`、`combined`。返回值中的 `locationLookup` 和 `data` 均保留 Open-Meteo 原生字段。

## web_search

由 OpenClaw 原生提供。用于获取官方公告和公开背景；选择搜索提供方时使用 `openclaw configure --section web`。

## image_generate

使用仅监听本机回环地址的 ComfyUI 与固定 Z-Image-Turbo 工作流。仅在用户明确要求图片时调用；当前固定输出一张 `1024×1024` PNG。生成内容是视觉创作，不能作为气象观测或预警证据。

## 飞书呈现

飞书官方插件使用自动渲染模式。普通短回答保持文本；包含围栏代码块或表格的结构化简报自动使用 Markdown 卡片。回答只需要遵循 `formats/weather-report.md`，不调用自建前端或自定义卡片服务。

## 定时任务

每日天气简报由 OpenClaw 原生 cron 执行。声明位于 `../config/automations/weather-schedules.json`，任务提示词在 `../config/automations/prompts/`；不要在 Agent 内自行维护第二套调度循环。09:00 晨报与 16:30 复核只通过 `runtime/weather-briefings/morning-latest.md` 交换当天基线，这个文件是短期运行数据，不进入长期记忆。
