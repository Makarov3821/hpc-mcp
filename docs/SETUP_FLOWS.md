# 接入／适配流程与受管工作区（Phase 9A）

setup_flow 管理集群接入和应用适配的持久记录、工作区和恢复，不替代计算任务的 workflow。当前仅实现流程基础设施：关闭为 completed 表示所引用材料已持久化，不证明集群配置完成、应用通过审阅或已激活。完整业务步骤门禁在 Phase 9B。

## 使用顺序

1. 调用 setup_flow_create(kind, request_key, label)，kind 为 cluster_setup 或 application_adaptation。request_key 是同一意图的幂等键；重复参数返回已有流程，不创建第二个目录，改变参数则冲突。
2. 只在返回的绝对 workspace 中写改造包、审阅 JSON 和辅助材料。原输入留在用户项目，原脚本复制到包的 original/；默认不生成 GAUSSIAN.md 等说明。MCP 不会约束 agent 的外部文件写入，客户端必须遵守此路径。
3. 按现有探测、配置或应用安装／独立审阅／确认接口操作。9A 不自动调用这些操作，也不执行脚本、提交计算或启动监控。
4. 关闭前获取最新 revision。completed 必须引用已持久化的探测报告或已安装应用版本；MCP 验证来源并保存哈希。没有结果或用户停止适配时，用 abandoned 显式放弃，不取消作业、不卸载应用。
5. 先 setup_flow_cleanup(..., dry_run=true) 查看范围，再按用户清理意图 apply。仅删除这个已关闭流程的 workspace，保留 SQLite 事件、材料引用、目录所有权标记和已安装应用，不删除状态根目录、checkout 或用户输入／输出。

```text
state/onboarding/<flow_id>/
├── owner.json     # 受管目录身份标记，清理后保留
└── workspace/     # agent 改造包与审阅暂存；关闭后可清理
```

## 状态与恢复

| 状态 | 含义／允许动作 |
| --- | --- |
| allocating | 已记录创建意图，目录分配未完成；resume 或用原 key 重试 |
| open | 等待 agent 收集或准备材料；禁止清理 |
| closing | 已验证并保存材料及关闭意图；resume 完成关闭 |
| completed / abandoned | 已关闭；允许预览／清理，不允许重新开放 |
| cleaning | 已明确应用清理，但删除未完成；resume 恢复原清理 |
| cleaned | 暂存已删除；历史可读，重复清理返回 already_cleaned |

修订号、SQLite 事务和 OS 锁防止并发覆盖；过期 revision 返回 setup_flow_conflict。操作失败保存 last_error 和事件；resume 不授权新业务动作，只恢复已记录的分配、关闭或清理。所有恢复由显式调用触发，没有后台自动清理。结束后再次使用同一个 request_key 不重新创建工作区，新适配必须使用新 key。

拒绝目录越界、所有权标记不符、符号链接和非普通文件。当前假定同一账户下的可信客户端；目录检查不隔离客户端，也不阻止其他进程在检查后改变文件。

## 接口和 CLI

MCP：setup_flow_create/get/list/events/resume/close/cleanup。list 支持 limit（1..500）和 offset；events 支持 limit 和 after_id。close、cleanup 使用 expected_revision；cleanup 默认预览，CLI --apply 才删除。

```bash
hpc-mcp --state-dir /ABS/HPC_MCP/state setup-flow-create application_adaptation \
  --request-key gaussian-lab-001 --label gaussian
# 将改造包写入返回的 workspace，持久化应用版本及审阅报告后，再关闭。
hpc-mcp --state-dir /ABS/HPC_MCP/state setup-flow-close FLOW_ID \
  --revision REVISION --outcome completed --artifacts-file /ABS/WORKSPACE/artifacts.json
hpc-mcp --state-dir /ABS/HPC_MCP/state setup-flow-cleanup FLOW_ID --revision CLOSED_REVISION
hpc-mcp --state-dir /ABS/HPC_MCP/state setup-flow-cleanup FLOW_ID --revision CLOSED_REVISION --apply
```

artifacts.json 示例：`[{"kind":"application_version","application":"gaussian","version":1}]`，或 `[{"kind":"onboarding_report","report_id":"o_…"}]`。不接受任意文件路径作为持久材料引用。应用引用保存安装版本与哈希；它本身不要求激活，不能替代独立审阅门禁。
