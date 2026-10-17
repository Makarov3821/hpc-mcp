# 精确任务计划与执行授权（Phase 10B）

日常默认走 [batch](BATCH.md)，batch_start 内部执行本页的逐项授权；单个输入也同样处理。以下低层 MCP 接口属于 serve --tool-profile advanced，CLI 和核心方法始终保留；默认模式仍可读取历史或显式撤销授权，不放宽执行门禁。

普通脚本、生成模板、注册 handler 和 workflow 的提交共用 JobService 门禁。prepare 仅生成本地快照；应用激活、独立 review、安装确认、启用监控均不授权提交计算任务。MCP 不调用 LLM，也不认证聊天用户身份。默认 client_attested 模式由可信 agent 转交确认说明；可选 [外部签名审批](EXTERNAL_APPROVALS.md) 使用独立审批者/reviewer 的 Ed25519 背书，保持精确计划与角色绑定，不能伪造用户同意。

## 单任务流程

1. `job_submission_plan(run_id, automation=null)`：冻结完整计划、返回 review_token，检查本地快照，不上传、不连接集群。展示 script_text、manifest、集群设置、remote_dir、command、输出目标／覆盖与自动动作。
2. 用户确认这一具体计划后，`job_authorize(run_id, review_token, confirmation_note)` 保存批准。错误 token、另一任务的 token、旧输入／配置／策略拒绝；确认不执行计算。
3. `job_submit(run_id)` 在上传前以及上传／远程校验后重新检查授权，才调用 bsub/sbatch。别名、环境路径、所有集群设置、脚本／输入 SHA、生成来源、项目路径和输出策略均参与摘要。
4. 状态查询、历史和 `job_recover` 无需写权限。提交响应丢失继续读既有远程回执，绝不因旧授权失效或缺失而重新提交。已提交任务的重复 submit 只返回既有状态。

计划在 history.sqlite3 的 run 与事件中保存。创建不同计划撤销旧批准；同一计划重复读取保持批准。`job_revoke_authorization` 撤销未来上传／同步／清理，不取消已在集群运行的任务。正在持有作业锁的动作可能在撤销返回前完成。未授权旧版任务也必须展示新计划并确认；不自动补批准。

CLI：

```bash
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state submission-plan RUN_ID
# 读完整计划，用户确认后使用返回的精确 token：
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state authorize RUN_ID TOKEN \
  --confirmation-note '用户确认了此任务的脚本、输入、目标及输出范围'
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state submit RUN_ID
```

`submission-plan --automation-file /ABS/policy.json` 读取下面的权限对象；`revoke-authorization RUN_ID` 撤销。MCP 启动、CLI、协调器和 worker 保存同一配置文件的绝对路径，动作前重读有效配置，禁止后台进程用陈旧捕获值绕过变化检查。配置文件缺失／无效明确失败。

## 同步与清理范围

automation 是完整权限描述，未知键拒绝；默认自动动作与删除均关闭：

```json
{
  "auto_submit": false,
  "auto_sync": true,
  "sync_options": {
    "mode": "filtered", "includes": ["water.log", "water.chk"],
    "destination": "/ABS/A/B", "overwrite": "merge", "stable_only": true
  },
  "cleanup_policy": {
    "sync_cache_age_seconds": 86400,
    "storage_categories": ["snapshot", "sync_history"],
    "storage_age_seconds": 604800
  },
  "remote_cleanup": false,
  "extra_sync": []
}
```

sync_options 接受全部公开 job_sync 参数。最终目标在计划中解析：项目任务回到 input_dir；历史 snapshot 模式展示 `<attempt_id>` 模板。计划最多 4 MiB，脚本文本最多 1 MiB。允许在批准范围内只取具体文件、等待终态、降低字节／时间预算或提高空闲空间保留；扩大通配选择、更换目录／布局／覆盖及其他设置须重新计划并确认。extra_sync 最多 16 项额外明确同步范围，主要由 workflow 为依赖文件生成。

自动回传须批准 auto_sync=true，再以相同 sync_options／cleanup_policy 调用 monitor_watch；自动同步强制稳定终态。只读 watch(auto_sync=false) 且关闭清理不需要新写权限。操作队列固定授权 token；worker 在写入前复核，token 变化／配置变化拒绝。授权撤销或策略失配使监控进入 needs_attention 并通知，修正及重新登记后才能恢复。

清理预览不删除；实际清理必须在批准类别与范围内。缓存／存储清理允许更长保留年龄，禁止缩短到批准年龄以下；远程删除须 remote_cleanup=true，并继续满足已有终态、回执、路径与已下载结果校验。授权不会取消这些保护。全局 input_cache_cleanup 为独立显式维护设置，单个任务授权不自动启用它。

## Workflow 与依赖任务

workflow_plan 冻结同一 per-run 计划、资源限制、依赖条件和依赖同步范围。workflow_start(token,note) 为这些具体任务批准自动提交，不启动 daemon；coordinator 仅推进当前确认且启用的 workflow。直接 job_submit／job_authorize 不能绕过任务所有权；失效或旧版启用记录停止，先 workflow_review 再确认。

依赖输出尚未产生时没有它的 SHA。获得父任务结果后，MCP 校验并生成独立新 run，workflow 暂停，任务为 awaiting_authorization。调用 workflow_review(workflow_id) 展示实际新脚本、输入与 checkpoint 哈希、目标和输出范围；用户确认新 token 后 workflow_start，继续 tick。旧 token 不能授权派生快照。其他任务已在运行时继续运行；暂停不取消它们。默认不自动继承批准到未来内容，也不推测改写是否等价。

本地隔离见 [Handler runner](HANDLER_RUNNER.md)。本门禁不约束远程账户本来的权限，不冻结远程软件／初始化文件内容；SSH 身份、主机密钥与别名解析由用户的 OpenSSH 配置管理。科学正确性和客户端确认真实性仍需相应验证。
