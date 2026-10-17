# 持久计算生命周期（Phase 9C）

共享保留登记新增独立门禁：新提交默认保留，完成同步不释放。已有批准的 release_policy 时按规则自动核验并释放，否则需用户明确 retention_release；保留期间计算与归位正常完成，清理标为 deferred。自动策略等待条件时显示 awaiting_release，见 [正常收尾](FINALIZATION.md)。远程删除还核对其他客户端的声明与登录节点冲突，见 [保留登记](RETENTION.md)。

生命周期复用 JobService、monitor、同步 worker 和授权门禁，按独立 run 记录进度，不重新生成脚本、不调用 LLM。默认关闭；准备、授权、启用与启动后台进程分开。历史保存在 agent 的 state/history.sqlite3，agent 退出后可读取和恢复。

日常默认使用 [batch 入口](BATCH.md)，包括仅一个输入的一项批次；由批次内部登记并推进生命周期。下面的低层工具用于明确的手动操作、仅准备、诊断、已有 run 恢复或高级依赖流程，CLI 始终可用，MCP 需显式启动 serve --tool-profile advanced。

## 低层单任务操作

1. 用注册 application_prepare 或 job_prepare 创建独立输入快照，指定 project_root 和 outputs。
2. job_submission_plan 的 automation 设置 auto_submit=true、auto_sync=true，核对实际脚本、输入 SHA、集群、输出目标／覆盖和清理范围；用户确认后 job_authorize。已手动提交的任务只需批准 auto_sync。
3. job_lifecycle_start(run_id) 显式启用，不提交、不启动 daemon。monitor_start 启动独立协调器；也可 job_lifecycle_tick 推进一个任务，监控／下载仍由 monitor 执行。
4. job_lifecycle_get 返回 lifecycle、scheduler、application_result、sync、cleanup 和 pending_action；job_lifecycle_list 分页列举。结果归位使用批准的目录与覆盖规则：A/B 的输出回 B，A/D 的输出回 D，失败暂存在 A/.hpc-mcp-sync。

```bash
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state submission-plan RUN_ID \
  --automation-file /ABS/policy.json
# policy.json 至少包含 {"auto_submit":true,"auto_sync":true}；用户确认精确 token 后：
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state authorize RUN_ID TOKEN \
  --confirmation-note '用户确认此任务及自动提交、回传范围'
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state lifecycle-start RUN_ID
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state monitor-start
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state lifecycle-get RUN_ID
```

## 三种结果分别反馈

- scheduler：调度状态及查询时间。未知／查询失败不能解释为成功。
- application_result：注册版本的明确日志判据；passed、failed、pending 或 unverified。无判据为 unverified，终态日志缺失／没有成功标记为 pending，需要检查。只检查已归位、SHA 校验的日志，读取上限 2 MiB，不再次下载替换整份输出记录；marker 通过不证明科学正确性。
- sync：同步是否 complete、最终 output_dir、output_manifest 和错误。失败／取消任务也回传批准的诊断输出，但不自动清理。没有判据的任务可完成归位，清理标为 deferred。

只有调度成功、应用判据 passed 且保留输出校验通过，才按已批准的类别、年龄清理本地缓存／快照；remote_cleanup=true 才允许自动远程清理，继续满足原有回执／路径／结果保护。未通过任务保留数据。直接手动清理仍受独立授权与原有校验约束。

## 暂停、失败与恢复

job_lifecycle_pause 停止未来推进并停用本任务 monitor；已在集群运行的计算和正在同步的 worker 继续。monitor_stop 停整个协调器，不取消计算。重新启动协调器沿用持久记录，不自动补权限。

needs_attention 提供 failed_step 和 last_error。修复连接／冲突并检查后 job_lifecycle_resume：上传失败沿用同一快照；SSH 丢响应先恢复回执；已确认的 job_id 永不重交；活跃同步 worker 被接管，已校验输出复用；清理失败只重试未完成清理。应用失败不会重新计算，重算要准备新 run 并重新授权。输出被本地修改时停止，不悄悄覆盖；如需重新下载，明确新目标／覆盖范围并重新批准。

旧授权／配置／同步策略变化先重新展示计划、用户确认，再 resume。新增任务单独授权。重复 start 不重启已完成或需要检查的流程；resume 是明确重试入口。

## Workflow 与调度范围

批量／依赖用 workflow_plan(..., automation={"auto_sync":true,...}) 冻结每个任务的回传／清理权限，经用户确认 workflow_start 后对需要完整生命周期的具体 run 调用 job_lifecycle_start。提交仍由 workflow 管理，生命周期不会绕过依赖／并发／速率限制。派生 run 的实际输入必须 workflow_review 后确认，并为新 run 显式启用生命周期。

协调器每个轮询周期最多推进 monitor.max_jobs_per_cycle 个到期生命周期，其中最多尝试提交一个独立任务；更高吞吐使用 workflow 的提交限额。状态查询仍共享既有预算，同步共享 max_sync_operations。启动参数和发现入口见 settings_get；本阶段不引入自动重算或客户端身份认证。
