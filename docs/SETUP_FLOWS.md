# 集群接入与脚本适配流程（Phase 9A／9B）

setup_flow 管理接入／适配的持久记录、步骤门禁、工作区和恢复，不替代计算任务 workflow。MCP 不调用模型：agent 改造代码、启动全新上下文 reviewer，并转交用户确认。日常计算直接使用已激活的应用工具，不重复执行学习流程。

## 集群接入

```text
create(cluster_setup) → cluster_inputs → cluster_probe
  → awaiting_inputs：补充后重新 probe
  → awaiting_confirmation：展示精确配置与检查范围 → 用户确认 → confirm
```

cluster_inputs 至少提供集群名称和 ssh_host；scheduler／work_root 缺失时探测返回需要补充的信息。输入变化清除旧确认计划，配置只有在 confirm 后才保存。探测检查登录环境、调度命令、远程目录权限、队列和可选 module avail／用户指定路径；不扫盘、不 source 新配置中的 init_scripts、不加载模块或执行软件。登录失败、调度器歧义、命令缺失、目录权限或队列查询失败不能进入确认。

确认绑定完整默认设置、探测报告哈希与原配置摘要。默认探测有效期 3600 秒，可设置 30..86400 秒；配置变化／证据过期要求重新输入并探测。实际用户提交权限、共享存储和计算节点软件仍未验证。确认后保存 TOML、更新本地设置并完成流程，不提交测试作业。

集群配置确认保存后、首次计算前，调用 cluster_registry_probe 建立 work_root 下的共享登记并核对实际登录节点；该动作会写管理元数据，不能混入上述只读 probe。任务默认保留，跨节点冲突及释放规则见 [保留登记](RETENTION.md)。

## 应用适配

```text
create(application_adaptation) → application_source → agent 改造
  → application_install → application_review_request → 独立 reviewer
  → application_review_submit
      revise → agent 修订 → 安装新版本 → 新的独立审阅
      pass → 展示冻结计划 → 用户确认 → confirm 激活
```

application_source 接收应用名和用户明确提供的 1..32 个原脚本路径，总量不超过 2 MiB，仅复制并记录哈希，不执行源码。MCP 将原脚本存档到 workspace/source；bundle/original 必须逐文件匹配冻结来源，改变来源需新建流程。普通提交脚本变成生成器，批处理器保留生成逻辑并剥离直接提交；参数和环境以原脚本为准。

bundle 必须在本流程 workspace 内，manifest 应用与来源注册一致，集群已配置且调度器匹配。安装后返回精确 version 和 review_token；独立 reviewer 的固定材料位于 flow.business.review_request。审阅失败保留意见，必须安装新版本，不能将旧失败版本直接改为通过。

confirm 重新核对版本、文件哈希、独立报告和安装时的集群设置；配置变化后安装新版本并重新审阅。成功激活后提示重启 MCP 刷新能力信息，不证明计算成功。默认通过 application_get 查看注册参数、batch_plan 计算；专属 prepare 工具只在显式 advanced 模式重启后注册。来源、版本和报告绑定持久流程；受管版本的普通 application_review_request/submit/activate 拒绝绕过步骤，活动流程引用阻止应用卸载和版本清理。

独立 reviewer 的 fresh-context 身份仍由客户端声明；不能伪造报告或用户确认。未具备独立 reviewer 能力则等待。没有进入受管流程的旧版独立工具仍保留兼容行为，不能称其为已完成的 setup_flow；不是整个服务的统一提交授权门禁，后者在 Phase 10B。

## 标准动作参数

setup_flow_step(flow_id, expected_revision, action, payload)：

| action | payload |
| --- | --- |
| cluster_inputs | `{cluster,settings,probe_options?}`；settings 至少 ssh_host，键与 settings_get 一致 |
| cluster_probe | `{}` |
| application_source | `{application,source_paths:["/absolute/script"]}` |
| application_install | `{bundle_dir:"返回工作区内的绝对包路径"}` |
| application_review_request | `{author_session:"改造会话 ID"}` |
| application_review_submit | `{report:独立 reviewer 的原样报告}`；格式见 APPLICATIONS.md |
| reset_failed_step | `{}`；仅重置确认未产生持久副作用的失败操作；已安装／已存报告／已应用确认必须 resume |

probe_options：paths、module_avail、max_module_bytes、timeout、queue_details（与 cluster_probe 同范围），以及 max_probe_age_seconds。未知键拒绝。源码存档失败可恢复或放弃，不更换冻结来源。

setup_flow_confirm(flow_id, expected_revision, confirmation_token, confirmation_note)：token 来自 flow.business.confirmation_token；用户必须先审查 confirmation_plan。旧 token、旧 revision、未通过审阅或不在确认步骤都会失败。重复确认已完成的同一计划幂等。

## 工作区和关闭／清理

```text
state/onboarding/<flow_id>/
├── owner.json     # 目录所有权标记，清理后保留
└── workspace/
    ├── source/    # MCP 存档原脚本
    ├── bundle/    # agent 改造包
    └── review.json
```

只在 MCP 返回的 workspace 写临时材料，不写工作目录、用户项目或 checkout，默认不生成 GAUSSIAN.md。MCP 不限制 agent 在工具外写文件；客户端必须遵守目录约束。request_key 是一次意图的幂等键，结束后重复使用不重新分配，新适配用新 key。

受管业务流程只能通过 confirm 完成，不能手动 close(completed) 跳步。用户放弃时 close(outcome=abandoned)，不取消作业或卸载已安装版本。cleanup 默认预览；应用删除仅限这个已关闭流程的 workspace，保留 SQLite 历史、报告引用、安装版本、配置与用户数据。

Phase 9A 历史记录若没有 business 字段，保留原关闭语义；旧 open 记录重新 create 时接入业务阶段，不将既有材料猜成已确认配置。

## 恢复与接口

底层状态为 allocating → open → completed/abandoned → cleaning → cleaned，旧记录的 closing 可恢复；business.stage 表达上面的步骤。意图先持久化，操作失败保留 last_error 与 operation。get 在等待确认时只读检查有效性，失效返回 confirmation_status.valid=false 和下一步提示；读取不执行 SSH。

resume 只重放已记录动作：探测是只读重试，安装根据操作身份找回版本，报告根据操作身份去重，确认检查配置基线并恢复既有授权。已完成配置写入可恢复收尾；尚未写入且探测过期不能继续应用。没有后台自动推进或清理。可信同账户进程仍可能修改文件；流程检查本身不隔离同账户进程；handler 运行隔离另见 [Phase 10A 执行器](HANDLER_RUNNER.md)。

MCP：setup_flow_create/get/list/events/resume/step/confirm/close/cleanup。list 支持 limit／offset，events 支持 limit／after_id。CLI 对应 setup-flow-*：

```bash
hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  setup-flow-create application_adaptation --request-key gaussian-lab-001 --label gaussian
# payload.json 在返回 workspace 中；每次使用最新返回的 revision。
hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  setup-flow-step FLOW_ID --revision REVISION --action application_source \
  --payload-file /ABS/WORKSPACE/source-request.json
# 完成安装与独立审阅后，用户核对计划，再确认：
hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  setup-flow-confirm FLOW_ID CONFIRMATION_TOKEN --revision REVISION --note '用户确认了精确计划'
hpc-mcp --state-dir /ABS/HPC_MCP/state setup-flow-cleanup FLOW_ID --revision CLOSED_REVISION
hpc-mcp --state-dir /ABS/HPC_MCP/state setup-flow-cleanup FLOW_ID --revision CLOSED_REVISION --apply
```
