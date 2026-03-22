---
triggers:
  - 定时
  - 提醒
  - 闹钟
  - 定时任务
  - cron
  - 每天
  - 每小时
  - 分钟后
  - reminder
  - schedule
max_tokens: 500
---

# 定时任务与提醒

## 触发条件

用户请求设置定时任务、提醒、闹钟，或查看/管理已有定时任务。

## 指令

你有两个工具可用：

### cron — 管理定时任务

- `cron(action="list")` — 列出所有定时任务
- `cron(action="add", name="任务名", schedule="分 时 日 月 周", message="执行内容")` — 添加定时任务
  - schedule 格式为标准 cron 表达式: `分钟 小时 日 月 星期几`
  - `*` 表示所有值，例如 `0 8 * * *` = 每天 8:00
- `cron(action="remove", job_id="xxx")` — 删除任务
- `cron(action="trigger", job_id="xxx")` — 立即触发任务

### reminder — 一次性提醒

- `reminder(message="提醒内容", minutes=30)` — N 分钟后发一条提醒消息（默认）
- `reminder(message="完整任务描述", minutes=N, action="agent_task")` — N 分钟后由 Agent 执行该任务（生图、发消息、跑脚本等）

**铁律（必须遵守）**：当用户说「N 分钟后」再做某任务（例如「5 分钟后用香蕉生图画一只龙」「10 分钟后提醒我开会」）时：
1. **必须先**在本轮**仅**调用 `reminder(message="用户说的完整任务内容", minutes=N, action="agent_task")`（若只是提醒一句话则用默认 `action="message"` 即可）。
2. **不得在本轮**执行该任务本身（不要在本轮调用生图、发消息、bash 等工具去完成该任务）。定时到点后系统会自动把「任务内容」当作新消息让 Agent 执行。

## 示例

用户: "每天早上 9 点提醒我看邮件"
→ `cron(action="add", name="查看邮件", schedule="0 9 * * *", message="该查看邮件了")`

用户: "30 分钟后提醒我开会"
→ `reminder(message="该开会了", minutes=30)`

用户: "5 分钟后用香蕉生图，画一只喷火龙，比例 3:4"
→ 只调用 `reminder(message="使用香蕉生图，画一只喷火龙，比例3:4", minutes=5, action="agent_task")`，不要在本轮生图。

## 工具

- cron
- reminder
