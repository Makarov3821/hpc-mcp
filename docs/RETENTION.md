# 跨 agent 保留登记

远程清理以集群工作目录中的共享登记表为依据。单凭超过保留时间、调度结束或同步完成不会释放数据；只有预先批准的正常收尾规则或明确释放才会结束声明。所有 MCP 客户端必须使用支持本协议的版本；旧版本或手动 rm 无法受它约束。

升级前停止旧协调器和 MCP 连接，所有相关客户端升级重启后再启用清理。清空本地状态前先处理自己的远程声明；客户端身份也在状态库中，丢失后其他安装不会自动接管。共享登记目录不能作为某一安装的缓存随卸载删除。

## 接入与存储

配置 work_root 后调用 `cluster_registry_probe(cluster)`。它只创建私有管理目录，不提交计算、不扫描软件：

```text
work_root/
├── .hpc-mcp-registry/       # 不属于任务清理范围，权限 0700
│   ├── workspace.json      # workspace_id、首次访问的实际登录节点
│   ├── registry.lock       # 远程互斥锁
│   ├── registry.sqlite3    # 任务、持有者、操作与同步回执记录
│   └── conflict-*.json     # 其他实际节点的冲突记录
└── r_<任务ID>/
```

远程新增依赖为 Python 3.8+ 及标准库 sqlite3、fcntl，无需安装 hpc-mcp 包或常驻服务。通过 SSH 发送固定程序，使用回滚日志、完整同步和独占锁，不用 WAL。本地历史保存客户端身份，重启沿用；不同状态目录拥有不同身份。

数据库只允许首次登记的 owner_host 打开。其他节点访问同一 workspace_id 时报告冲突；显式 probe／写操作留下冲突记录，原节点也停止清理。第二节点的提交在上传前被阻止，应统一 SSH 目标后重试。同一配置中 work_root 路径相同会提前提示；路径相同不证明存储相同，workspace_id 用于核对共享目录。

cluster_check 只读检查，不创建标记；只读探测发现另一节点时提示，但不写冲突文件。已有冲突不自动过期。**节点嗅探不能证明 NFS 等网络文件系统的锁和持久化可靠**；即使只有一个登录节点，也需站点提供可靠存储语义，条件不满足时不要启用删除。此协议不隔离拥有同一 SSH 身份的恶意进程。

## 保留与释放

- 提交前自动登记 `submission` 保留声明；登记失败不上传、不调用调度器。提交／同步完成后记录回执；没有预先批准的释放规则时仍保持声明。
- 其他阶段或 agent 使用 `retention_hold(cluster, run_ids, holder, reason)`，例如 holder=agent-a-stage2。实际身份包含本地 MCP 安装的命名空间；新增和重新保留都会更新版本。
- `retention_status(cluster, run_ids)` 查看所有声明、原因、版本、工作区身份与远程清理资格，无需导入其他客户端的本地历史。
- 阶段结束后，`retention_release(cluster, run_ids, holder="submission", reason=...)` 默认只预览。用户确认后以 `dry_run=false, review_token=..., confirmation_note=...` 执行。计划有效 1 小时，绑定配置、工作区、任务版本与明确持有者。
- 只释放本安装命名空间下的指定声明。同一 MCP 的多个 agent 应使用不同标签；标签不是客户端认证机制。严格 external_signed 模式另需 action=retention_release、subject=集群名、token=该摘要的签名。
- 全部持有者释放后才具备远程清理资格；仍需独立清理批准、终态、结果校验和保留时间。释放不删除文件，也不代表已经同步完整输出。

无记录的旧任务默认保护。对本地已知且已提交的旧任务，显式 retention_hold 可登记为保留，再正常释放；未知目录不会收养或删除。丢失原客户端状态后不会自动接管其声明，需保留原状态或人工检查恢复，当前没有强制释放工具。

## 清理与中断

批量远程清理 token 绑定声明版本；预览后增加保留，即使随后释放，也需重新预览。逐任务／生命周期远程清理同样受共享登记表约束。锁覆盖最终声明检查、标记 deleting 和远程校验／删除，期间不授予新的声明。

成功登记 deleted 并保留审计。进程中断或超时可能留下 deleting；此时不自动删除或接受保留，先核对目录与诊断，不能靠过期时间恢复。普通校验失败保留目录，后续重新预览。

live 表示登记仍可操作，不代表调度器正在运行；调度状态仍通过任务查询获取。远程已记录 deleted 但本地丢失回执时，重试只补记删除结果，不再次删除目录。

本地批量维护还要求本机已观察到释放。本地原始输入、最新结果和历史保留；跨机器声明协调以远程目录为范围，各客户端独立本地缓存不作为其他客户端的结果副本。成功同步后的暂存搬移仍正常执行，不是持久远程数据的释放。

## 解除节点冲突

先统一所有客户端使用 owner_host，停止其他节点访问。在原节点调用 `cluster_registry_reconcile(cluster)` 预览，确认精确冲突列表，再传 token、dry_run=false 和 confirmation_note 执行。新冲突使旧计划失效；严格模式另需对应 action 的签名。此操作只解除冲突记录，不释放声明、不删除任务、不迁移数据库；再次跨节点访问仍会触发保护。

CLI：`cluster-registry-probe CLUSTER`、`retention-status --cluster CLUSTER --run-ids ID ...`、`retention-hold --cluster CLUSTER --run-ids ID ... --holder LABEL --reason TEXT`。`retention-release` 和 `cluster-registry-reconcile` 默认预览，用 `--apply --review-token TOKEN --confirmation-note TEXT` 执行。始终传相同的绝对 --config、--state-dir。

## 正常收尾与自动释放

Phase 15 已实现：提交批次时一次批准必要／可选输出、应用检查限制和释放策略，由 MCP 核验后自动结束本阶段声明。支持 manual、after_verified_sync、after_consumers_complete；没有规则的旧任务不自动迁移。所有声明结束后才具备共享清理资格，实际删除仍独立批准和复核。参数、依赖处理与恢复见 [正常收尾指南](FINALIZATION.md)。
