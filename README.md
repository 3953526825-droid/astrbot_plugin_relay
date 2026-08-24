# astrbot_plugin_relay

AstrBot QQ 传话插件：支持即时传话、固定文本定时传话、私聊/群聊、智能分段，以及 AstrBot WebUI 任务面板。

## 功能

- QQ 私聊和群聊传话
- 立即发送固定消息
- 每日/每周固定时间发送
- 基于 AstrBot 原生 Basic Cron 持久化
- 自动防止完全相同的周期任务重复创建
- 插件重载后恢复 Basic Cron handler
- AstrBot WebUI 插件 Pages：查看、创建、启停、删除、立即测试任务
- 智能分段、分段间隔和消息撤回
- 目标别名与白名单
- `{time}`、`{date}`、`{datetime}` 占位符

## WebUI 页面

在 AstrBot WebUI 中打开：

```text
插件 → astrbot_plugin_relay → 传话任务
```

页面支持：

- 查看任务总数、启用数、停用数
- 搜索任务
- 创建每日/每周固定消息任务
- 启用/停用任务
- 立即测试发送一次
- 删除任务
- 查看规则、目标、下次执行、上次执行和最近错误

页面使用 AstrBot 官方 Plugin Pages 机制：

```text
pages/relay/index.html
```

后端接口通过 `context.register_web_api()` 注册，页面通过 `window.AstrBotPluginPage` bridge 调用。

## LLM 工具

- `relay_message`：立即传话
- `set_periodic_relay`：创建每日/每周固定文本任务
- `list_pending_relays`：查看本插件创建的任务
- `cancel_relay_task`：取消本插件任务
- `recall_last_message`：撤回最近发送消息

固定文本定时发送应使用 `set_periodic_relay`，不会调用 Agent 二次判断。

## 安装

将插件目录复制到 AstrBot 的插件目录：

```text
astrbot_plugin_relay/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ README.md
└─ pages/
   └─ relay/
      └─ index.html
```

安装或更新后重载插件；如果 WebUI 插件详情中暂时看不到页面，刷新 WebUI 或重启 AstrBot 实例。

## 说明

- 页面任务当前专门管理本插件创建的 AstrBot `basic` Cron，不会修改 SelfEvolution 等其他插件任务。
- 所有页面操作都会再次校验任务的 `relay_plugin` 标记，不能通过页面操作其他插件的 Cron。
- 任务目标平台默认为 `aiocqhttp`，可在插件配置中设置 `default_platform`。
- 页面“立即测试”会真实向目标发送一条消息，请谨慎使用。
- 不要把插件目录中的备份文件、`__pycache__` 或运行日志上传到 GitHub。
