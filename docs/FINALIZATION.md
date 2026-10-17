# 正常收尾与自动释放

释放规则与实际脚本、输入、集群和同步范围一起进入批次／任务计划，经用户一次确认后由 MCP 执行。规则不授权删除；默认协调器仍需显式启用，没有规则的请求和旧任务继续手动保留。

## 批次中的规则

在 batch_plan 的 automation 中指定；也可在某一 tasks 项的 automation 覆盖。以下示例要求已注册的 Gaussian 应用带有结果检查，且同步选择包含这些文件：

```json
{
  "release_policy": {
    "strategy": "after_verified_sync",
    "required_outputs": ["water.log", "water.chk"],
    "optional_outputs": ["stderr.log"],
    "allow_unverified_application": false
  }
}
```

compact 预览的 policies 同时展示 release_policy 与 release_contract，后者绑定应用版本、manifest 哈希、实际检查规则和检查能力限制。用户确认 batch_start 的精确摘要后，不需要逐任务确认释放。高级入口通过 job_submission_plan 的 automation 设置同一契约，workflow 计划也支持它。

没有应用检查规则时，自动策略必须明确设置 allow_unverified_application=true，否则计划失败。此时应用状态仍为 unverified，只证明调度成功、必要文件已取回，不证明科学结果正确；不能为了提交方便静默开启。没有明确必要输出时使用 manual，不从宽泛的输出列表猜测哪些文件必需。

## 三种策略

| strategy | 行为 |
| --- | --- |
| manual | 不自动释放本阶段声明；有明确输出契约时仍可核验正常收尾，为依赖阶段提供证据 |
| after_verified_sync | 满足正常收尾条件后释放本安装的 submission 声明；其他保留／依赖声明独立阻止删除 |
| after_consumers_complete | 先满足正常收尾，再等待已登记的远程文件依赖声明结束，最后释放 submission |

必要输出可写相对文件路径，也可写 `{ "pattern": "results/**/*.chk", "min_matches": 1 }`。默认至少一个匹配；零匹配绝不视为成功。模式从任务根目录匹配，`*` 不跨目录，`**`／`***` 可跨目录。字面路径只能要求一个文件，条目最多 256 个。可选输出为模式数组，计算未生成它们不阻止收尾。

所有契约文件必须已包含在批准的同步选择中，不会自动扩大下载范围。排除必要文件的规则被拒绝；无法静态证明安全的“通配契约＋排除规则”组合也拒绝，请改为明确路径。已经同步的清单仍需整体校验，用户之后改动／删除已下载文件会阻止自动收尾和清理。

## 正常收尾的证据

同时要求：新查询确认 succeeded 且退出码 0；同步 complete、所选本地文件 SHA-256 正确；必要文件达到匹配数量；没有待恢复安装或活动同步操作；配置的应用检查通过，或计划明确批准无检查的限制。失败、取消、未知状态、缺文件、权限／审批／配置变化都不触发自动释放。

后台监控与生命周期执行这些固定规则，记录批准摘要、调度结果、输出摘要、应用状态和时间。重复评估不重复释放；本地只保存一份当前详细记录，审计保存精简状态变化，重复轮询不复制完整输出清单。

## 后续阶段与其他 agent

已确认 workflow 的文件交接和 application_succeeded 依赖会建立独立的远程保留声明；父任务未提交时在其登记时建立，外部已提交父任务在 workflow 启动前建立。保留建立失败则不放行后续提交。派生快照仍需原有 review／确认，声明随新的已确认执行身份更新。

后续任务按自己的已批准输出契约正常收尾后，可以结束对应依赖声明；其自己的 submission 或其他手动声明不必先释放，避免循环等待。没有后续任务的正常收尾契约时保持依赖，可由用户通过 retention_release 明确解除；标签可从 retention_status 读取。暂停 workflow 不自动撤销依赖。

其他安装的声明不被顺带释放。同安装多个 agent 应使用独立 holder 标签；显式 retention_hold 即使使用 submission 标签，也成为手动保留，自动规则不能覆盖它。用户再次明确释放后才结束保护。

## 反馈、恢复与清理

- job_get／job_lifecycle_get 提供 finalization；batch_status 返回精简的缓存结果、时间和 blocker，保留完整计数。生命周期可处于 awaiting_release，计算已结束但仍等待收尾或其他声明。
- retention_status 在远程锁下返回当前持有者及 cleanup_ready。这里表示共享保留门禁已放行，实际删除仍要验证终态、必要证据、已下载结果、节点冲突、保留时间和清理批准。
- retention_finalize(run_ids) 可批量重新评估已有授权，通常仅用于诊断／恢复；不增加权限、不删除数据、不要求额外手动释放批准。CLI：`hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state retention-finalize --run-ids r_...`。
- 自动释放后发现输出或批准失效，会重新保留自动声明；手动释放不会被偷偷改写。清理入口也复核证据，不能仅信任缓存的 cleanup_ready。新增保留使旧清理摘要失效。

升级不追溯批准旧任务。若要为已有任务启用规则，先生成包含规则的新计划并经用户确认；已有手动重新保留仍需手动释放。所有参与客户端／协调器都应升级重启，旧版本不遵守新增协议。
