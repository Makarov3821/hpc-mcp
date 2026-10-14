# 下一步计划

## Phase 11：批量任务入口与一次确认

目标：agent 生成一批输入卡后，通过 batch_plan → 用户确认 → batch_start → batch_status 完成整批计算，减少逐任务 MCP 调用及客户端 review。MCP 不修改客户端审批设置，不承诺绕过 Codex review。

### 11A：批量准备与冻结计划

- 接收明确任务清单，不扫描目录、不猜软件；支持已注册应用的输入卡／任务目录，以及用户明确提供的提交脚本。
- 每项独立 run、输入快照、远程目录和回传范围；复用 handler 隔离与 JobService。
- request_key 幂等，预留任务身份，固定应用版本；中断后找回已生成快照和 workflow，不重复生成或登记。
- 准备失败不提交成功子集；整批准备完成才返回统一摘要。检查回传输出冲突及覆盖其他任务输入的风险。

### 11B：一次确认与后台执行

- 批次摘要绑定全部任务的实际脚本／输入哈希、应用版本、集群设置、输出／覆盖、自动同步／清理权限、workflow 限额和协调器启动策略。
- batch_start 经用户确认一次后，内部逐项授权、启用生命周期和已有 workflow／coordinator；保留 Phase 10 的所有门禁，不绕过依赖、回执防重或配置变化检查。
- 重复启动不重交、不自动重置失败；部分启动中断可显式重放收尾。配置变化先 batch_review，再确认新摘要；新增任务或扩大权限创建新批次。

### 11C：汇总反馈与定向恢复

- batch_status 只读本地历史，返回完整计数、任务映射、待处理项及分页详情；默认优先异常与待办，避免展开所有日志和 manifest。
- 批次暂停不取消作业；显式选择任务恢复，只重试失败步骤，已确认 job_id 不重交。
- MCP／CLI 同步提供批量入口，README 指导 agent 优先使用批量流程。
- 为真正只读的查询声明 readOnlyHint；生成、授权、执行及清理保持准确副作用注解。注解只供客户端分类，不削弱审批或服务器权限检查。

### 验收

验证批量注册／原脚本准备、未知应用、混合失败、幂等与请求冲突、进程中断恢复、输出冲突、整批授权失效、跨批次 token、后台执行、多目录归位、暂停／定向重试、防重交、汇总分页、CLI 与真实 MCP stdio。沿用离线调度器和真实 rsync，不连接用户集群。

状态：已实现并通过回归；本计划单独保存，不改写前面 Phase 9／10 的计划。

### 实施记录（2026-10-14）

- batch.py 通过独立 SQLite 批次记录与 request_key 管理幂等。预留 run／workflow 身份；成功快照、固定应用版本、已登记 workflow 和中断的启动收尾均可复用。仅重建可验证归属的未登记快照，未知目录／链接不删除。
- batch_plan 支持注册应用与原提交脚本、逐项 automation 覆盖、明确输入清单及 500 项上限；成功子集不先提交。整批摘要绑定实际脚本／输入、每项策略与协调器启动；共享策略分组减少重复输出，完整脚本／manifest 一次读取。
- batch_start 内部批准各任务、登记生命周期，然后才放行 workflow，并按冻结计划启动／复用 daemon；低层提交门禁、配置重读、隔离、回执和结果判据不变。
- batch_status 只读本地记录，提供全量计数、异常优先分页、映射与独立结果。pause／resume／review 支持部分恢复和失效后重新确认；未选中的暂停任务保持暂停，不重交确认过的作业。
- 六个 MCP／CLI 批量入口、settings_get 元数据、只读查询注解与 README／BATCH／REFERENCE／PLUGIN_TESTING 已更新。客户端审批设置没有修改，减少调用次数而不承诺免 review。
- 目前未 commit／push；没有连接真实集群。最终验收结果见后续记录。

### 最终验收

- 全量 **297 项测试通过**（2026-10-14）：23 项批量回归，以及新增 SDK modern／legacy 批量工具与注解集成场景；保留全部既有授权、生命周期、真实 Linux 隔离和 stdio 检查。
- 真实 detached coordinator／rsync worker 验证整批确认后无需客户端逐项 prepare／submit／check；LSF／Slurm 使用离线调度命令，没有访问真实集群。
- compileall 与 git diff --check 通过。尚未 commit／push；真机验收时可先使用已有注册应用的小批输入卡，关闭删除，核对每项输出位置和汇总状态。

### 默认调用路线

- 按用户要求，日常计算统一默认 batch，单个输入也使用一项批次。同一请求、集群与项目内的输入合并为一个明确清单，不逐卡建批次，不循环调用 prepare／submit／check。
- MCP instructions、通用／专属 prepare 描述、settings_get.batch 与 README／应用／生命周期说明对齐；参数通过 application_get 发现。
- 低层接口用于明确仅准备、手动／诊断、已有 run 恢复和高级依赖；已有任务不为了切换路线重新准备。客户端仍决定实际工具选择，默认指引不取消用户确认或客户端审批。

## Phase 12：统一计算入口与工具模式

目标：默认 MCP 只提供 batch 计算路线，单个和多个输入统一处理；减少 agent 选择逐项计算管线的机会。内部单任务模块和 CLI 保留，不重写执行、授权或历史机制。

- Phase 11 已提交为 b738ea5（Add batch planning and confirmed background execution），本轮未 push。
- `serve` 默认 batch 模式，隐藏单任务准备／授权／提交／同步、模板提交、高级 workflow、旧 profile 读取和手动作业清理；隐藏工具不仅不展示，tools/call 也不可调用。
- `serve --tool-profile advanced` 显式恢复完整 MCP 接口与专属应用 prepare。模式在启动时固定，变更需重启；不自动切换，不修改客户端配置，CLI 和内部服务不受目录曝光影响。
- 安装／接入／适配、注册参数发现、历史、取消、日志、回执恢复、记账及全局监控默认保留。应用是否注册与 MCP 工具模式独立，默认通过 batch_plan 内部使用注册 handler。
- settings_get.tool_catalog 暴露当前模式、精确隐藏列表和高级启动选项。README、BATCH、REFERENCE、APPLICATIONS 与 SETUP_FLOWS 对齐，不再声称默认注册专属 prepare 工具。
- 验收：默认目录及直接调用拒绝，单张注册输入通过 batch，同目录多任务沿用批量流程；advanced 完整接口与授权门禁保留；真实 SDK modern／legacy 和重启跨模式共享历史。

状态：已实现并通过验收；按用户要求作为独立 commit 提交，未 push。

### Phase 12 验收记录

- 全量 **298 项测试通过**，包含真实 SDK modern／legacy 默认目录、隐藏工具直接调用拒绝、已注册单卡通过 batch，以及跨连接 advanced 模式恢复专属 prepare／完整工具且保留 workflow 所有权门禁。
- 既有批量后台执行、CLI、隔离、授权和恢复测试均通过；compileall、git diff --check 通过。未连接真实集群。
- 升级后重启 MCP 连接刷新目录；默认不修改客户端配置，不停后台作业，不迁移或删除记录。本记录随 Phase 12 的独立实现 commit 提交，未 push。
