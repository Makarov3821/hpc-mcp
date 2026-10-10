# 用户脚本应用插件契约（接口版本 1）

MCP 不内置应用执行方法。外部 agent 在首次设置时把用户脚本改造成标准 Python handler；MCP 负责固定的安装、独立审阅门禁、确认、调用与清理。所有目录默认位于 `state-dir/applications`，使用绝对 `--state-dir` 可显式改变整个数据位置。注册方法不自动调用 LLM。

## 文件与来源

```text
bundle/
├── manifest.json
├── handler.py
└── original/用户原脚本
```

安装后位于 `applications/<application>/versions/<version>/`，增加受管理的 `record.json`，保存来源文件 SHA、状态、参数定义、独立审阅报告与真实验证证据。原脚本只存档，不由 MCP 执行。包最多 2 MiB、128 个普通文件，不接受符号链接或保留文件名。插件 ID 为小写字母开头，最多 48 个小写字母／数字／下划线；核心工具命名空间不可占用。

agent 先通过 settings_get 获取 applications_root，在该目录分配唯一 `.install-draft-*` 暂存目录，再写 bundle 与审阅 JSON。不要使用当前工作目录、用户项目或 checkout 存放学习资料；默认不生成 GAUSSIAN.md 等说明文档。Markdown 不参与运行时分发或配置，持久结果是 handler.py、manifest.json、original/ 和 record.json。用户要求的附加说明可放在应用包内。安装和报告提交成功后，agent 清理自己创建的确切暂存目录；失败时可暂留诊断，之后通过 application_cleanup 的预览／应用流程清理。MCP 不会自动删除用户提供的外部源目录。

## Manifest

参考 [Gaussian](../examples/applications/gaussian/manifest.json)、[VASP](../examples/applications/vasp/manifest.json)、[LSF](../examples/applications/hello_lsf/manifest.json) 和 [Slurm](../examples/applications/hello_slurm/manifest.json)。样例不会自动安装，clusters、队列和路径必须按用户来源修改。

必填字段：`interface_version: 1`、`application`、`scheduler: lsf|slurm`、非空 `clusters`、`parameters`。可选：`description`、`timeout_seconds`（1..60，默认 30）、`max_output_bytes`（1024..1048576，默认 262144）、`dependencies`、`validation`。

参数采用有界 JSON Schema 子集：object／string／integer／boolean／array，以及 properties、required、additionalProperties=false、default、description、enum、minimum、maximum、pattern、items、maxLength、maxItems。未知关键字拒绝；整数默认非负，上限 2147483647，字符串默认最长 4096，数组默认最多 1024 项。输入覆盖只接受声明参数。没有未知参数透传、表达式求值或跨调度器转换。

`dependencies` 是当前 MCP Python 环境中的顶层模块名。缺少模块返回 application_dependency_missing，由用户／agent 明确安装到 MCP 虚拟环境；MCP 不自动执行 pip，不创建插件专属环境，不卸载共享依赖。因此本版卸载不遗留专属依赖环境，也不会破坏其他插件。

可选验证定义：`{"log":"{input_stem}.log","success_marker":"Normal termination","failure_marker":"Error termination"}`。只支持明确日志路径与字面终止标记，不求值；失败标记优先，成功标记要求位于日志最后 8192 字符。没有判据返回 unverified。此判据不验证科学正确性。

## Handler 请求与响应

MCP 使用当前 Python 的 `-I -B` 在独立子进程中执行已审阅 handler；stdin 为一个 JSON 对象，stdout 必须只输出一个 JSON 对象，诊断写 stderr。运行暂存由 MCP 创建并在成功／失败后清除，超时与输出超预算会终止进程组。子进程不是强安全沙箱，代码审阅和用户确认必需。

请求包含：`interface_version`、`input_path`、`input_dir`、`input_name`（任务目录为 null）、`remote_dir`、`scheduler`、补齐默认值的 `parameters`、`staging_dir`。

响应：`script`（完整提交脚本文本）、非空相对路径 `input_files`、非空同步规则 `outputs`，可选 `resources` 对象。提交文件允许没有 shebang；保留用户原格式。MCP 保存为 `hpc-mcp-job.sh`，此文件名不能与输入冲突。helper 文件可通过 `__file__` 所在目录读取，不能依赖原 CWD 或隐式导入用户环境。

handler 只生成，不 SSH／rsync、不 bsub／sbatch、不修改原输入、不扫描提交其他任务或留下后台进程。用户选择一项输入／任务目录即生成独立 run；多项通过多个准备调用再交 workflow 管理，不 packing。输出大小预算覆盖 stdout/stderr；代码可信，不宣称限制任意本地副作用。

## 生命周期与独立审阅循环

1. `application_install(bundle_dir)`：静态安装 draft，执行无代码；返回 version 与 review_token。
2. `application_review_request(application, version, author_session)`：生成绑定文件哈希和版本的审阅材料、固定提示和七项检查清单。
3. agent 启动全新上下文的独立 reviewer。只传原脚本、handler、manifest 和本接口契约；不共享改造对话、作者解释或既有结论。reviewer 只静态分析，不执行脚本，不修改文件。
4. `application_review_submit(application, version, review_token, report)`：原样提交独立报告。revise 时 agent 按意见修改并 install 新版本，再启动新的独立 reviewer；重复直到 pass。报告缺项、会话相同、非全新上下文声明、pass 仍有差异／未知项都会拒绝。未通过禁止激活。
5. 用户确认后 `application_activate(..., confirmation_note)`：核对集群与调度器，保存设置校验值并激活。不能编造用户确认。
6. 重启 MCP 加载 `<application>_prepare` 及参数 schema；通用 application_prepare 可立即使用。prepare 冻结版本／参数／脚本，job_submit 单独上传提交。
7. 用户授权真实小任务后，`application_validate(run_id)` 查询状态、同步稳定日志并核对显式判据；不提交新任务。

报告格式（每项 checks 必须提供具体分析证据）：

```json
{
  "reviewer_session": "fresh-review-session-id",
  "fresh_context": true,
  "verdict": "pass",
  "checks": {
    "generation_branches": "各条件分支与原脚本对应关系及分析",
    "scheduler_directives": "队列、核数、节点和输出指令分析",
    "environment_setup": "环境初始化、顺序及 scratch 分析",
    "command_and_redirections": "程序命令、参数和重定向分析",
    "paths_and_outputs": "输入、输出、工作目录及清理范围分析",
    "parameter_defaults": "默认值与参数映射分析",
    "mcp_boundary": "拆除提交／历史与单任务接口的分析"
  },
  "differences": [],
  "unresolved": [],
  "allowed_changes": ["工作目录绑定为独立远程 run 目录"]
}
```

`differences` 为未批准的输出／行为差异；`unresolved` 为无法确定的行为；revise 至少填写一项可操作意见。只允许 MCP 边界迁移：单任务选择、remote_dir 绑定、JSON 参数接口、剥离提交及历史副作用；reviewer 必须解释具体变化，不自行批准计算命令、环境、默认值或生成分支变化。MCP 记录历次报告、时间和精确版本，不把静态 LLM 判断称为执行等价证明。

**审阅来源由客户端声明。** MCP 无法验证独立会话是否真实创建，session ID 和 fresh_context 不是身份认证。agent 必须实际启动 reviewer、原样提交报告；没有此能力则保持 draft。MCP 不内部调用模型、不需要模型 API 凭据；普通任务调用不重复审阅。旧版 checked 记录不能替代新门禁，须请求独立审阅。

不再要求 cases.json 或 references/，也不提供 application_check。旧包可保留这些归档文件，但 MCP 不执行、不比较或以其作为通过依据。仓库的已知原脚本对照测试仅用于开发回归，不是新应用学习的通过门槛。

## 更新、删除与恢复

application_update 与 install 使用相同入口，创建新版本，不隐式激活。独立审阅 pass → activate 后默认切换；旧任务仍固定原版本，已有专属工具固定启动时版本，重启更新工具 schema。再次 activate 已确认旧版本可切回；集群设置变化须明确审阅并重新激活，不继承旧运行验证。

`application_remove(application, dry_run=true)` 默认预览完整文件和占用；dry_run=false 应用于用户明确删除意图。未提交或非终态任务阻止卸载；未提交且未被工作流占用的任务可通过 job_cancel 本地放弃，之后不能再提交。已提交任务应完成／确认取消终态后再卸载，不能依据取消请求猜终态。

程序串行化安装、执行、删除；记录 removing 后清除应用文件与注册，再核验。中断后重复 apply 可恢复；重复删除返回 already_removed。卸载保留原输入、计算输出、任务快照／历史与最小审计，不自动删远程任务目录或系统依赖。文件变化、符号链接、路径越界明确拒绝，不以猜目录完成清理。

`application_cleanup(older_than_seconds=86400, dry_run=true)` 清除过期的受管理安装／运行暂存与无活动引用的非当前版本，默认仅预览、不安装计时器。冻结任务脚本可继续查询；清除旧插件版本后不能再以该版本重新准备。

## CLI 对应

`application-list/get/install/update/review-request/review-submit/activate/prepare/validate/remove/cleanup` 共用上述服务。review-request 接受 `--author-session`，review-submit 接受 `--report-file`；activate 的 review_token 为位置参数，用户确认说明用 `--note`。remove／cleanup 用 `--apply` 才删除。

旧 profile 的写入／准备命令仅返回迁移错误；profile-get/list 保留历史读取。gaussian-prepare CLI 只转发已注册 gaussian，接受 cluster、input_file、必填 `--project-root`，以及 `--parameters`、`--[no-]compact`，不接受旧 spec 或输入改写参数。
