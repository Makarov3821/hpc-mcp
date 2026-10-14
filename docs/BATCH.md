# 批量计算：一次计划、一次确认

日常计算默认使用 batch_plan → 用户确认 → batch_start → 按需 batch_status；单个输入也用一项 tasks。同一请求、集群与项目内的多个输入归入同一批次，不为每张卡分别建批次。批量入口在 MCP 内部复用注册 handler、独立 run、workflow、生命周期和同步 worker；agent 不必逐个 prepare／authorize／submit／check。客户端是否审批每次调用仍由客户端控制，批量入口减少调用次数，不取消 Phase 10 的权限检查。

默认 serve 的 batch 模式只暴露统一计算路线，隐藏逐项 prepare／authorize／submit／sync、模板提交和高级 workflow 工具；隐藏工具也不能直接 tools/call。安装／适配管理、参数发现、历史、取消和诊断继续可用。单任务后端与 CLI 保留；用户明确需要仅准备、手动提交、已有 run 恢复或高级依赖时，可用 CLI 或显式启动 serve --tool-profile advanced 恢复完整 MCP 工具集。已存在 run 不重新准备以切换路线。

settings_get.tool_catalog 返回模式、隐藏列表和高级启动选项。模式变更需要重启客户端连接，不删除配置或历史，不改变后台任务；advanced 模式也不放宽提交／同步／清理权限。工具模式控制 MCP 接口曝光，不是本地进程安全隔离，也不取消客户端审批。

## 明确提供任务清单

同一批次使用一个 cluster 和项目母目录 project_root，最多 500 个任务。每项指定已注册应用和输入卡／任务目录，或已有提交脚本；不扫描目录、不猜软件、不改写输入卡。未知软件返回需要用户模板的错误。

```json
{
  "request_key": "gaussian-A-20261014-001",
  "name": "项目 A 的两张输入卡",
  "cluster": "lab",
  "project_root": "/ABS/A",
  "tasks": [
    {"task_id":"water", "application":"gaussian", "input_path":"/ABS/A/B/water.gjf",
     "parameters":{"cpus":28}},
    {"task_id":"methane", "application":"gaussian", "input_path":"/ABS/A/D/methane.gjf",
     "parameters":{"cpus":28}}
  ],
  "limits":{"max_in_flight":2,"max_submissions_per_tick":2},
  "compact":true
}
```

parameters 必须使用 application_get 返回的真实 schema；没有内置 Gaussian 默认调用。version 可固定应用版本，省略时首次准备固定当前绑定，后续重试不换新版本。普通脚本任务示例：

```json
{"task_id":"hello","input_dir":"/ABS/A/B","script":"job.sh",
 "outputs":["result.txt"],"input_files":["job.sh","input.txt"]}
```

脚本任务还可提供 output_exclude、input_exclude、max_input_bytes。所有任务可提供 automation 覆盖批次同名顶层设置；sync_options／cleanup_policy 等嵌套对象整体替换，不猜测深层合并。批量生命周期要求 auto_submit=true、auto_sync=true，默认二者开启；删除默认关闭。其他策略、参数和限制沿用 [提交授权](SUBMISSION_AUTHORIZATION.md) 与 [生命周期](LIFECYCLE.md)。

## 计划与一次启动

batch_plan 只写本地快照、历史和禁用 workflow，不上传、不提交、不启动后台。每项保存独立 run_id／远程目录；任何准备失败都阻止整批确认，成功子集不会先提交。一个 request_key 只对应一份明确请求；重复相同请求恢复未完成准备并复用已保存快照／workflow，改变请求则拒绝，新的计算意图用新 key。

返回 batch_id、review_token、任务映射、输入／脚本 SHA、参数／资源、回传目标、策略分组、workflow 限额及协调器设置。compact=false 同时返回所有冻结脚本和完整 manifest；重复读取不重新生成。摘要绑定所有任务和启动策略，准备后输入原文件变化不改变快照。

用户确认这份完整计划后，batch_start(batch_id,review_token,confirmation_note) 内部逐项批准并登记生命周期，最后放行 workflow。start_monitor 默认 true，按计划中冻结的监控设置启动／复用协调器；false 不新启进程，已有协调器仍会推进启用的 workflow。monitor_settings 省略时固定创建批次时的既有设置。运行中的协调器设置不匹配会停止启动步骤，不能偷偷改全局设置。

启动不等待计算结束。调用中断可检查错误后重复同一 batch_start 收尾；重复成功启动不会重交或重置失败。暂停使用 batch_pause；已有作业和 worker 继续。修复并检查后 batch_resume(batch_id,task_ids=["water"]) 只恢复指定任务的失败步骤；暂停时未选中的任务继续保持暂停。只有恢复整批显式暂停流程时才能省略 task_ids。新计算仍需新 run 和新授权。

配置变化或批准撤销后，用 batch_review 暂停并重新冻结现有快照，再经用户确认 batch_start。更换 SSH 目标／远程根等准备身份可能需要新批次；新增任务、变更参数或扩大回传／清理策略使用新请求，不原地扩展已批准批次。

## 输出冲突与状态

每项回原任务目录，保留独立远程执行目录。同目录不同输入卡必须输出不同名字，例如 water.log／methane.log。批量计划拒绝已发现的相同输出、覆盖任何批次输入，以及重叠目录下无法证明互斥的通配输出；不自动改名、不通过 merge 掩盖冲突。复杂通配范围检查偏保守，可改成精确文件名或分开回传目录。

batch_status 只读本地持久记录，不主动 SSH 查询。counts 覆盖全部任务；默认不展开已完成项，优先失败／待处理项，limit=50、offset=0，include_completed=true 可读取完成任务与归位位置。返回 next_offset、stage／control_stage、job_id、独立调度／应用／同步状态、failed_step、错误和 pending_action；没有判据的完成任务仍为 unverified。读状态不会补批准或重交；避免高频轮询。

## CLI

```bash
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state batch-plan REQUEST_KEY NAME lab \
  --project-root /ABS/A --tasks-file /ABS/tasks.json --no-compact
# 用户确认返回的精确 token：
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state batch-start BATCH_ID TOKEN \
  --confirmation-note '用户确认整批任务、回传和自动动作范围'
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state batch-status BATCH_ID --include-completed
```

batch-plan 还支持 --automation-file、--limits、--monitor-settings、--[no-]start-monitor、--[no-]compact；控制命令为 batch-pause、batch-resume --task-ids、batch-review。所有入口共用核心，历史在 agent 状态目录，计划请求不应包含凭据。
