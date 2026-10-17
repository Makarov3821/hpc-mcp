# 本地缓存与集群工作目录清理

默认 MCP 提供 local_cache_cleanup、cluster_work_cleanup。两者默认只预览；按保留时间筛选，返回路径、字节数、跳过原因和精确 review_token。用户确认后执行，不必改写逐任务授权，也不授予计算／上传／同步权限。

## 本地缓存

新任务默认保留，不能仅按年龄清理。阶段结束先 retention_release 预览／确认；远程目录要求共享登记表中的全部持有者释放。本地批量维护要求本机已观察到释放。接入、跨 agent 声明、旧任务迁移与节点冲突处理见 [保留登记](RETENTION.md)。

默认保留 7 天，类别 input_cache、sync_cache、sync_history、old_outputs。最新归位输出、项目输入、数据库历史、配置和注册应用不删除；输入快照只有明确 categories=["snapshot"] 才可选。删除快照后不能直接重放它，来源和输入哈希仍保留。

cluster、project_root、run_ids 筛选已登记任务。limit 默认 100、最大 500；offset 分页，next_offset 指出下一页。计算须结束、同步 complete，非空保留输出仍通过 SHA-256；活动／未知／未提交任务、活动 worker 和 pending 安装恢复日志均跳过。失败／取消任务默认保留，include_failed=true 才可明确选择。

input_cache 是独立复制的全局共享 blob，删除不影响已准备快照。带任务筛选时默认去掉此类别；显式选它不能带任务筛选。max_input_cache_bytes 可指定容量目标，可能选择未达到保留年龄的 blob，实际范围在预览中列出。每次最多选 limit 个，剩余数量见 omitted_shared_cache_entries，后续重新预览逐批处理。

执行前复核任务事实、保留输出和候选目录／文件的身份与元数据。变化则拒绝该项；不跟随符号链接，不删除未知目录、特殊文件或跨挂载树。

## 集群临时工作目录

共享登记表缺失、仍有保留声明、存在登录节点冲突或处于 deleting 时均保护目录。远程锁覆盖声明检查与删除，预览绑定声明版本；重新保留再释放也使旧清理计划失效。

预览必须指定已配置 cluster，默认保留 7 天，也支持 project_root、run_ids、include_failed、limit、offset。只处理本地历史登记的 work_root/run_id，不扫根目录、不删除 work_root 本身或未知目录。

复用原有远程保护：配置／路径匹配，最新调度查询确认终态，提交回执匹配，已选远程结果 SHA 和下载清单一致。整个目录及内容须早于冻结的保留时间；删除前在同一 SSH 命令中复核。新文件、变化结果、状态无法确认或不安全路径均不删除。

**清理删除整个任务目录，未选择下载的文件也会消失。** 确认前取回所有需要的 log、chk 等。目录校验不保证抵御其他远程进程在最终检查后继续写入，仍需生产者已停止写入。

## 预览、批准与执行

向 cluster_work_cleanup 传以下参数；本地工具可省略 cluster：

```json
{"cluster":"lab","older_than_seconds":604800,"dry_run":true}
```

用户检查 plan.candidates 和 skipped 后，向同一工具传：

```json
{"dry_run":false,"review_token":"返回的精确摘要",
 "confirmation_note":"用户确认删除预览中的过期任务数据"}
```

执行只使用 token 绑定的原计划，其他筛选参数不会扩大范围。计划有效 1 小时，不纳入新任务／缓存；变化或过期须重新预览确认。部分成功逐项记录，重试不会重删已完成项。维护计划与结果保留在历史中，不清空数据库。

external_signed 模式另需对应 action=local_cache_cleanup 或 cluster_work_cleanup 的独立签名，subject 来自 plan.subject，token 为 review_token。确认说明不能代替签名，见 [外部审批](EXTERNAL_APPROVALS.md)。计算授权仍保持原样。

## CLI 与周期调用

```bash
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state local-cache-cleanup
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state cluster-work-cleanup --cluster lab
# 用户确认预览后：
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state cluster-work-cleanup \
  --apply --review-token TOKEN --confirmation-note '用户确认这份目录清理计划'
```

CLI 支持 --project-root、--run-ids、--older-than-seconds、--include-failed、--limit、--offset；本地另有 --categories、--max-input-cache-bytes。限制由 settings_get.maintenance 发现。

可按周期调用入口检查保留策略；当前不安装定时器、不自动批准未来删除。后台定期删除需要另行明确保留规则，不能复用过期 token 覆盖未来未知任务。已有逐任务生命周期清理仍按原授权运行。
