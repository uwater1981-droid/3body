---
name: 3body
description: |
  3body 多 agent 工作框架的管理技能。当用户提到以下任何意图时触发：
  - "3body" / "三体" / "检查系统" / "系统状态" / "系统诊断"
  - "检查效率" / "工作效率" / "agent 效率" / "运行报告"
  - "新建项目" / "添加项目" / "init project"
  - "检查任务" / "任务状态" / "task board"
  - "增加 backlog" / "添加任务" / "扩展任务"
  - "审查系统" / "审计" / "audit system"
  - "优化框架" / "优化系统"
  适用于管理 3body 多项目多 agent 协作系统的所有操作。
argument-hint: <command> [args]
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
---

# 3body — 多 Agent 工作框架

## 使用原则

默认先做只读诊断，再决定是否修改系统。

- 对 `/3body status`、`/3body list`、`/3body logs`、`/3body efficiency`、`/3body audit`：
  只读执行，不修改文件、不重启服务。
- 对 `/3body init`、`/3body restart`、`/3body backlog ...`：
  这是写操作。只有用户明确要求时才执行，并且先做 preflight、备份、验证。
- 输出时默认脱敏：
  不回显 token、chat id、cookie、密码、完整 webhook、完整日志敏感字段。

## 事实来源优先级

遇到文档和现场不一致时，一律以现场为准，按这个顺序取证：

1. 项目注册表：`/Users/vincent/.local/lib/3body/projects.json`
2. 各项目根目录下的 `project.json`
3. 文件系统实际结构：
   `.ai/tasks` 是真实目录还是 symlink，`web/` 是本地目录还是共享 symlink，`scripts/` 里实际有什么
4. launchd plist 与运行状态
5. engine 日志和项目日志

不要把旧文档里的单一路径结构当成系统不变量。

## 当前机器上的关键路径

| 组件 | 路径 |
|------|------|
| Engine（权威副本） | `/Users/vincent/.local/lib/3body/engine.py` |
| 项目注册表 | `/Users/vincent/.local/lib/3body/projects.json` |
| filelock | `/Users/vincent/.local/lib/3body/filelock.py` |
| telegram | `/Users/vincent/.local/lib/3body/telegram.py` |
| Engine wrapper | `/Users/vincent/.local/bin/3body-engine.sh` |
| Engine launchd | `~/Library/LaunchAgents/com.3body.engine.plist` |
| Engine stdout | `/Users/vincent/.openclaw/workspace/main/runtime/3body-engine.stdout.log` |
| Engine stderr | `/Users/vincent/.openclaw/workspace/main/runtime/3body-engine.stderr.log` |

## 拓扑说明

3body 在这台机器上已经是混合结构，不能假定所有项目都完全同构。

- 项目根目录以 `projects.json` 中的 `root` 为准。
- `.ai/tasks` 可能是：
  - 项目内真实目录
  - 指向 `~/.local/lib/3body-<name>/tasks` 的 symlink
- `web/` 可能是：
  - 项目私有目录
  - 指向共享 dashboard 的 symlink
- dashboard 启动入口可能是：
  - 项目内 `scripts/serve_task_board.sh`
  - 项目内 `scripts/task_board_server.py`
  - 共享根项目中的 `scripts/task_board_server.py`，通过 `THREEBODY_ROOT` / `THREEBODY_PORT` 指向项目
- dashboard 自启方式可能是：
  - 独立 launchd plist
  - 手动 wrapper
  - 前台临时进程

不要再假定“每个项目都必须自带一份 `scripts/task_board_server.py`”。

## 角色与命名

- 调度 owner 名称是 `openclaw`
- 执行 owner 名称是 `codex`
- 审核 owner 名称是 `claude_code`

注意：

- 可执行文件通常检查 `codex` 和 `claude`
- `claude_code` 是任务 owner，不一定存在同名二进制

## 命令参考

用户输入 `/3body` 后跟以下命令。

### `/3body status` — 系统诊断

目标：给出当前系统健康概览，不做修改。

输出应包括：

1. Engine launchd 是否已加载
2. Engine 最近日志是否有错误
3. 所有注册项目的 dashboard 可用性
4. 每个项目任务数：`active / awaiting_audit / awaiting_human / blocked / done`
5. 运行中 agent 数量与 pid 存活状态
6. 异常摘要与建议

执行方式：

```bash
launchctl list | grep 'com.3body.engine'
cat /Users/vincent/.local/lib/3body/projects.json
tail -10 /Users/vincent/.openclaw/workspace/main/runtime/3body-engine.stdout.log
tail -10 /Users/vincent/.openclaw/workspace/main/runtime/3body-engine.stderr.log
```

然后对每个项目动态探测：

- 读取 `root`、`port`、`enabled`
- 统计 `<root>/.ai/tasks/*/*.json`
- 检查 `runtime.pid` 是否仍存活
- 请求 `http://127.0.0.1:<port>/api/tasks`

推荐输出模板：

```text
3body 状态总览

Engine
- launchd: loaded / not loaded
- 最近日志: 正常 / 有错误

Dashboards
- <project>: 8765 OK
- <project>: 8768 FAIL（超时）

Tasks
- <project>: active X / audit Y / human Z / blocked A / done B

Agents
- codex: N running
- claude_code: N running

Findings
- HIGH: ...
- MEDIUM: ...

Next Actions
- ...
```

成功标准：

- 已覆盖 engine、dashboard、tasks、agents 四个面向
- 每个已注册项目都有状态结论
- findings 不为空时给出下一步建议
- 不泄露敏感字段

### `/3body list` — 列出所有项目

读取 `projects.json`，显示：

- `id`
- `name`
- `enabled`
- `root`
- `port`
- `.ai/tasks` 是目录还是 symlink
- `web/` 是目录还是 symlink
- dashboard 当前是否可访问

推荐输出模板：

```text
Projects
- social | enabled | 8765 | tasks=symlink | web=dir | dashboard=OK
- bri | enabled | 8766 | tasks=symlink | web=dir | dashboard=OK
- xxx | disabled | 8770 | tasks=dir | web=symlink | dashboard=DOWN
```

### `/3body logs [project]` — 查看日志

默认只读。

- 不带项目名：
  看 engine stdout/stderr 最近日志
- 带项目名：
  先通过 `projects.json` 找到项目根目录，再看 `<root>/.ai/logs/` 最新日志

不要假定日志目录一定存在；不存在时说明“该项目尚无 agent 日志”。

推荐输出模板：

```text
Logs: <scope>

Latest Entries
- ...
- ...

Error Signals
- none

Interpretation
- ...
```

### `/3body efficiency [1h|3h|12h|all]` — 效率报告

遍历所有注册项目的任务 JSON，统计指定时段内 checkpoint：

- 每个 agent 的 checkpoint 数
- 实质更新数
- 涉及任务数
- 吞吐率
- 空档时间

如果某项目未注册，不纳入全局效率统计，除非用户明确指定按路径扫描。

推荐输出模板：

```text
Efficiency: last 3h

By Agent
- codex: checkpoints X | substantive Y | tasks Z | rate R/hr
- claude_code: checkpoints X | substantive Y | tasks Z | rate R/hr

Idle Gaps
- codex: ...
- claude_code: ...

Assessment
- 效率高 / 一般 / 偏低

Actions
- ...
```

### `/3body audit` — 系统审查

这是深度只读审查，不自动修复。

至少检查：

1. `engine.py` 语法与启动入口
2. `projects.json` 是否存在坏路径、重复端口、禁用项目
3. 每个项目的 `project.json` 是否可解析
4. `.ai/tasks` 目录或 symlink 是否有效
5. dashboard 是否可访问
6. launchd plist 是否存在并与现网匹配
7. 日志中是否有持续错误、idle spam、死进程
8. `which codex`、`which claude` 是否可用

输出分级：

- `CRITICAL`
- `HIGH`
- `MEDIUM`
- `LOW`

如果没有发现问题，要明确说明“未发现 findings”，但仍可补充残余风险。

执行节奏：

1. 快扫：
   注册表、端口、JSON、launchd、二进制路径
2. 深扫：
   只有快扫发现异常时，再继续看日志、代码和死进程

推荐输出模板：

```text
Audit Summary
- scope: all registered projects
- result: findings found / no findings

Findings
- CRITICAL: ...
- HIGH: ...
- MEDIUM: ...

Residual Risks
- ...

Suggested Fix Order
1. ...
2. ...
```

成功标准：

- findings 按严重级别排序
- 每条 finding 指向明确文件、端口、label 或项目
- 若无 findings，明确写“未发现 findings”
- 不自动修复，除非用户继续授权

### `/3body restart` — 重启系统

这是写操作，只有用户明确要求时才执行。

重启原则：

1. 先识别受谁管理，再决定怎么重启
2. 优先使用 launchd 原生方式
3. 只有在没有 launchd 管理时，才考虑 wrapper 或前台命令
4. 不要默认用“kill 端口进程”代替服务重启

Engine 推荐方式：

```bash
launchctl kickstart -k gui/$(id -u)/com.3body.engine
```

Dashboard 推荐流程：

1. 先在 `~/Library/LaunchAgents/` 中查找相关 plist
2. 如果找到对应 label，优先 `launchctl kickstart -k`
3. 如果没有 label 但项目有 `scripts/serve_task_board.sh`，用该 wrapper 重启
4. 如果两者都没有，再查实际启动方式

重启后必须验证：

- engine 日志有新输出
- dashboard `GET /api/tasks` 返回成功
- 项目页面可打开

推荐执行框架：

1. preflight
   - 确认目标项目或目标服务
   - 确认当前管理方式：launchd / wrapper / 前台
   - 备份将要修改的 plist 或配置
2. restart
   - 按管理方式执行重启
3. verify
   - 检查 launchd / pid / `/api/tasks` / 页面可达性
4. rollback
   - 若重启后比重启前更差，优先恢复到上一个可工作状态

成功标准：

- 对应服务成功重新拉起
- 目标 dashboard 或 engine 比重启前更健康
- 没有引入新的端口冲突或 label 异常

失败标准：

- `kickstart` 或 wrapper 返回失败
- `/api/tasks` 仍不可访问
- 页面仍不可打开
- 日志出现新的启动错误

失败时必须说明：

- 卡在哪一步
- 当前更像 launchd 问题、端口问题还是项目路径问题
- 下一步最小修复动作

### `/3body init <name>` — 创建新项目

这是写操作，只有用户明确要求时才执行。

不要再把单一结构写死成模板。先做 preflight：

1. 检查 `projects.json` 里是否已存在同名 id 或 root
2. 检查候选端口是否被占用
3. 检查用户是否需要独立 dashboard，还是复用共享 dashboard
4. 检查用户是否要项目内 `.ai/tasks`，还是本地库 symlink 方案

默认推荐“兼容模板”：

1. 创建项目根目录
2. 创建 `project.json`
3. 创建 `.ai/tasks/`，默认直接放在项目内
4. `web/` 可复用共享 dashboard；若复用，则允许建立 symlink
5. dashboard 启动优先提供 `scripts/serve_task_board.sh`
6. 注册到 `/Users/vincent/.local/lib/3body/projects.json`
7. 只有用户要求自启时，才创建 launchd plist

只有当用户明确要求本地任务仓库时，才创建：

- `/Users/vincent/.local/lib/3body-<name>/tasks/...`
- 并把 `<project_root>/.ai/tasks` 指向该目录

写入前先备份：

```bash
cp /Users/vincent/.local/lib/3body/projects.json /Users/vincent/.local/lib/3body/projects.json.bak.$(date +%s)
```

创建后必须验证：

- `project.json` 能解析
- `.ai/tasks` 结构存在
- dashboard 入口可启动
- 新项目已出现在 `projects.json`

推荐输出模板：

```text
Init Result
- project: <name>
- root: <path>
- port: <port>
- tasks layout: dir / symlink
- dashboard mode: shared / private

Verification
- project.json: OK
- tasks: OK
- registry: OK
- dashboard: OK / pending

Follow-up
- ...
```

成功标准：

- 注册表写入成功且 JSON 合法
- 项目根目录与任务目录真实存在
- dashboard 有明确启动入口
- 输出中清楚说明采用了哪种项目模板

### `/3body backlog <project> add <task_id> <goal>` — 添加 backlog

这是写操作。

步骤：

1. 通过 `projects.json` 找到项目根目录
2. 读取 `<root>/project.json`
3. 备份原文件
4. 向 `backlog` 末尾追加任务
5. 用 JSON 校验写回结果

验证方式：

```bash
python3 -m json.tool <project.json>
```

不要在写坏 JSON 后离开现场。

成功标准：

- 找到正确项目根目录
- backlog 已追加且未破坏原有结构
- `project.json` 校验通过
- 输出中说明新增 task_id 和目标项目

## 共享资产与项目私有资产

审查和修改前先识别资产归属：

- 共享资产：
  - `/Users/vincent/.local/lib/3body/engine.py`
  - 共享 dashboard 前端
  - 共享 server 脚本
- 项目私有资产：
  - `<root>/project.json`
  - `<root>/.ai/tasks/`
  - `<root>/.ai/logs/`
  - 项目级 wrapper 与项目数据文件

修改共享资产前，要先确认影响范围不是单一项目。

## 修改后的验证要求

只要改了系统，就至少做对应的验证：

- 改 `engine.py`：
  - `python3 -m py_compile /Users/vincent/.local/lib/3body/engine.py`
- 改 `projects.json`：
  - `python3 -m json.tool /Users/vincent/.local/lib/3body/projects.json`
- 改 `project.json`：
  - `python3 -m json.tool <project.json>`
- 改 dashboard：
  - 访问 `/api/tasks`
  - 打开 `task-board.html`
- 改 launchd：
  - `launchctl list | grep 3body`

建议把所有写操作都按统一节奏执行：

```text
Preflight -> Change -> Verify -> Rollback(if needed)
```

如果无法完成 `Verify`，不要把任务表述成“已完成”，只能表述成“已修改，待验证”。

## 回滚要求

对任何写操作，都要给自己留回滚点：

- 改 JSON 前先备份
- 改 plist 前先备份
- 改共享脚本前先确认旧版本路径
- 如果启动失败，优先恢复到上一个可工作的配置，而不是继续叠加修复

## 故障排除

| 症状 | 常见原因 | 检查点 |
|------|----------|--------|
| dashboard 打不开 | launchd 未启动、wrapper 退出、端口冲突 | 先看 `projects.json` 端口，再看 plist 和进程 |
| dashboard 串到别的项目 | `projects.json` 的 `root` 或 `port` 指错 | 先核注册表，不要先改前端 |
| 任务板空白 | `.ai/tasks` 无效或 API 失败 | 检查目录是否存在、是目录还是 symlink、`/api/tasks` 是否返回 |
| engine 空闲但 backlog 有内容 | `project.json` 未被 engine 读到，或 backlog 太短 | 检查项目是否注册、`project.json` 是否可解析 |
| 重启后服务仍异常 | 用错了重启方式 | 先确认是不是 launchd 管理，不要直接 kill 端口 |
| 审核 agent 无法启动 | 命令名和 owner 名混淆 | 检查 `which claude`，不要去找 `claude_code` 二进制 |

## 关键提醒

1. 本机 3body 结构已经演化为混合拓扑，必须动态探测，不要套旧模板。
2. `projects.json` 是跨项目操作的第一入口。
3. 默认只读，写操作先 preflight、再备份、最后验证。
4. 不泄露敏感信息。
