# 用户脚本应用插件契约（接口版本 1）

MCP 不内置应用执行方法。外部 agent 在首次设置时把用户脚本改造成标准 Python handler；MCP 负责固定的安装、对照检查、确认、调用与清理。所有目录默认位于 `state-dir/applications`，使用绝对 `--state-dir` 可显式改变整个数据位置。注册方法不自动调用 LLM。

## 文件与来源

```text
bundle/
├── manifest.json
├── handler.py
├── original/用户原脚本
├── cases.json
└── references/原生成结果.lsf
```

安装后位于 `applications/<application>/versions/<version>/`，增加受管理的 `record.json`，保存来源文件 SHA、状态、参数定义、对照与验证证据。原脚本只存档，不由 MCP 执行。包最多 2 MiB、128 个普通文件，不接受符号链接或保留文件名。插件 ID 为小写字母开头，最多 48 个小写字母／数字／下划线；核心工具命名空间不可占用。

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

## 生命周期与对照检查

1. `application_install(bundle_dir)`：静态安装 draft，执行无代码；返回 version 与 review_token。
2. 审阅完整源码和改造差异后，`application_check(application, version, review_token, review_note)`：运行 cases.json，与参考文件逐字节比较。失败保持未激活；成功为 checked。
3. 用户确认后 `application_activate(..., confirmation_note)`：核对集群／调度器，保存集群设置校验值并激活。确认状态与真实验证独立。
4. 重启 MCP：加载 `<application>_prepare` 工具，参数 schema 从 manifest 暴露。现有连接不自动刷新；稳定的 application_prepare 可立即使用新激活版本。
5. `application_prepare(application, cluster, input_path, project_root, parameters?, version?, compact=true)`：创建输入快照、冻结插件版本／参数／生成脚本。默认按应用／集群选择已确认版本；显式版本仍须曾确认，文件校验通过。job_submit 单独上传提交。
6. 用户授权真实小任务后，`application_validate(run_id)`：查询新状态、同步稳定日志并核对哈希，记录 passed／failed／pending 的判据范围；不提交新任务。

`cases.json` 为 1..512 项数组，每项包含 `request` 与 `expected_script`（包内相对参考文件名）。request 至少提供该 handler 所需的 input_name／remote_dir／parameters；MCP 在执行时绑定独立 staging_dir。参考必须来自原生成结果，不能仅用待测 handler 自我生成。目录绑定在双方相同条件下比較；未迁移行为明确列入审阅。

本仓库对 qg16 256 组、qvasp 576 组分支逐字比较，还对经过审阅的 qg16 `-P`／qvasp `-d` 原 CLI 生成文件与 MCP 快照做对照。原 CLI 测试只在临时目录和假调度命令环境运行；任意新用户脚本的预览开关不能直接执行。样例构建脚本 `tools/build_application_examples.py` 提取已知仓库生成函数，不运行原提交 CLI。

## 更新、删除与恢复

application_update 与 install 使用相同入口，创建新版本，不隐式激活。check → activate 后默认切换；旧任务仍固定原版本，已有专属工具固定启动时版本，重启更新工具 schema。再次 activate 已确认旧版本可切回；集群设置变化须明确审阅并重新激活，不继承旧运行验证。

`application_remove(application, dry_run=true)` 默认预览完整文件和占用；dry_run=false 应用于用户明确删除意图。未提交或非终态任务阻止卸载；未提交且未被工作流占用的任务可通过 job_cancel 本地放弃，之后不能再提交。已提交任务应完成／确认取消终态后再卸载，不能依据取消请求猜终态。

程序串行化安装、执行、删除；记录 removing 后清除应用文件与注册，再核验。中断后重复 apply 可恢复；重复删除返回 already_removed。卸载保留原输入、计算输出、任务快照／历史与最小审计，不自动删远程任务目录或系统依赖。文件变化、符号链接、路径越界明确拒绝，不以猜目录完成清理。

`application_cleanup(older_than_seconds=86400, dry_run=true)` 清除过期的受管理安装／运行暂存与无活动引用的非当前版本，默认仅预览、不安装计时器。冻结任务脚本可继续查询；清除旧插件版本后不能再以该版本重新准备。

## CLI 对应

`application-list/get/install/update/check/activate/prepare/validate/remove/cleanup` 共用上述服务。check／activate 的 review_token 为位置参数，审阅或确认说明用 `--note`；remove／cleanup 用 `--apply` 才删除。旧 profile 的写入／准备接口已撤出 MCP，并在 CLI 明确报迁移错误；profile_get/list 只保留历史读取。旧内置 Gaussian spec 准备不再作为应用回退，gaussian-prepare CLI 仅转发已注册 gaussian。
