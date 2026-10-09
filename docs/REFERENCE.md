# Agent 接口与设置参考

MCP 字典工具同时提供 JSON 文本内容及 `structuredContent` 对象，方便 agent 与程序客户端消费；48 个工具通过 `tools/list` 暴露参数 schema。协议验证见 `tests/test_stdio.py`，需要安装官方 MCP extra；验证只操作临时配置、快照、模板和历史，不访问集群。

## 新集群引导与应用运行配置

`serve` 可在指定配置文件不存在时启动空服务，不自动写入占位集群。配置、报告与运行数据继续由绝对 `--config`／`--state-dir` 定位。

| MCP 工具 | 参数与行为 |
| --- | --- |
| `cluster_probe` | 必填 ssh_host；scheduler=null、work_root=null、paths=null、module_avail=true、max_module_bytes=32768、timeout=30、cluster=null、queue_details=false。SSH 别名校验与普通连接一致；timeout 1..300，模块输出限制 1024..262144 字节，paths 最多 64 个明确的远程绝对路径。cluster 指定时复用既有可信登录初始化与连接参数，SSH 目的地必须一致。返回持久 report_id、候选调度器、队列、模块候选、路径检查与待补信息；不修改配置。 |
| `onboarding_report(report_id)` | 读取并校验不可变探测／脚本／验证报告，不访问 SSH。 |
| `profile_draft(name, cluster, application, definition, report_ids=null)` | 保存不可变待审版本，definition 使用 template_import 的 schema；report_ids 最多 32 个，关联来源、校验值、缺失项和未解析项。名称／应用标识 1..100 字符，字母数字开头，其余允许点、下划线、连字符。返回 review_token；尚不可用于准备任务。 |
| `profile_confirm(profile_id, review_token, confirmation_note, make_default=true)` | 用户确认后保存 exact draft 的确认记录并创建固定 version=1 的内部模板；note 为非空、最多 4096 字符，记录用户对配置和未解析项的确认。默认绑定 cluster/application；重复确认不创建模板新版本。不能验证或提交作业。 |
| `profile_get(profile_id)` | 返回定义、确认、来源和最近验证证据；本地集群设置变化标记 requires_recheck 与 stale。远程软件变化仍需显式复验。 |
| `profile_list(cluster=null, application=null, limit=50, offset=0)` | 分页列出各版本及默认绑定，limit 1..500，offset 非负。 |
| `profile_plan(input_dir, profile_id=null, cluster=null, application=null, parameters=null, project_root=null)` | 指定 profile_id，或选择 cluster/application 的已确认默认配置。准备一项任务，固定定义与参数；历史附 profile_id、版本、review_token、来源和验证范围。配置发生变化需重新建档确认；不提交。 |
| `profile_validate(profile_id, command=null, parameters=null, run_id=null)` | command 必须是用户核对的小型 argv，parameters 为模板绑定；只准备 probe。传 run_id 时不能再传 command／parameters：查询已准备验证作业的状态，成功终态后同步日志并记录证据。 |

探测只使用登录信息、调度命令定位、队列查询和具体路径 test；软件发现最多执行一次 `module avail`。没有模块系统、查询失败、超限或信息不完整均保持未知；不递归列目录、扫描安装位置、自动加载候选模块或执行用户提交器。`queue_details=true` 附加固定的 `bqueues -l`／`scontrol show partition` 原始诊断证据，不推断账户／QOS 权限或推荐核数。未知站点策略请用户补充。

配置关联和确认保存在状态 SQLite，报告校验且不可变。手动配置可不带报告；附带的报告需要与目标 SSH 目的地一致。confirmation_note 是 agent 对外部用户确认的记录，MCP 无法独立证明用户确实回复；agent 不得自行填充确认。内部模板名为 `profile.<profile_id>`，准备始终固定版本 1；改变定义创建新 profile 草案，不改写已有版本。

验证使用临时本地 marker 创建独立输入快照，临时源目录随准备结束清理，长期数据仍在 agent 状态目录。作业按有效资源、初始化、启动器和容器运行小型 command，不上传应用输入；替换应用重定向为 validation.log／validation.err。环境需要 Bash、cat、hostname；scratch 中的持久输出仍写在 run 目录。先检查脚本和资源，授权后通过 job_submit 提交；网络响应不明沿用 job_recover。

检查只接受该 profile 的专用 probe，不接受普通历史作业作验证依据。要求新鲜成功终态、稳定同步、日志 SHA 和每个声明任务的 marker／hostname 证据；日志单文件限 1 MiB、总量 2 MiB。失败保持 failed，排队／状态未知／同步失败保持 pending；分别保存用户确认与验证状态。验证覆盖该命令、参数与资源布局的提交、共享目录、初始化和回传，不能替代应用科学结果判定。参数与最近验证的绑定不一致时，新任务记为 unverified；本地集群设置变化拒绝复用旧确认。重新验证远程软件需显式操作，不隐式扫描或执行版本探测。

CLI 对应 `cluster-probe`、`onboarding-report`、`profile-draft`、`profile-confirm`、`profile-get`、`profile-list`、`profile-plan`、`profile-validate`，均可用 `--help` 获取完整参数。例如先调整示例中的站点路径和资源，再运行：

```bash
hpc-mcp --config /ABS/AGENT/clusters.toml --state-dir /ABS/AGENT/state \
  profile-draft gaussian lab gaussian examples/profiles/gaussian-lsf.json --report-id REPORT_ID
hpc-mcp --config /ABS/AGENT/clusters.toml --state-dir /ABS/AGENT/state \
  profile-confirm PROFILE_ID REVIEW_TOKEN --confirmation-note '用户确认了资源、环境和待核对项'
hpc-mcp --config /ABS/AGENT/clusters.toml --state-dir /ABS/AGENT/state \
  profile-validate PROFILE_ID --command '["bash","-c","command -v g16"]' \
  --parameters '{"input":"test.gjf","stem":"test"}'
```

最后一条只准备验证作业；command -v 仅验证命令可见性，完整程序运行和版本需用户指定相应小测试。以本次返回的 run_id 单独提交，完成后用 `profile-validate PROFILE_ID --run-id RUN_ID` 记录证据。

## 结构化脚本生成

`script_generate(scheduler, spec)` 纯预览，不执行命令或写文件。`spec` 接受以下字段，未知字段拒绝：

| 字段 | 内容 |
| --- | --- |
| `command` | 必填非空字符串数组，程序及 argv；不是待解析的 Shell 命令 |
| `resources` | 下表中的资源设置，默认 CPU 为 1 |
| `environment` | 环境变量名到字符串值的对象，默认空对象 |
| `init_scripts` | 计算节点上可信初始化文件的绝对路径数组，默认空数组 |
| `setup_steps` | 默认空数组；按顺序执行 source／module_load／module_purge／export，非空时不能同时设置 init_scripts 或 environment |
| `stdin`／`stdout`／`stderr` | 可选的执行目录内相对文件路径；不允许绝对路径或 `..` |
| `output_directories` | 可选相对目录数组，在执行时 mkdir；默认空数组 |
| `launcher` | 可选 `{kind, arguments?}`，kind 为 srun/mpirun/mpiexec，arguments 为受支持的启动器选项 argv |
| `container` | 可选可信远程镜像、运行时及挂载；见下节 |
| `scratch` | 可选单节点临时目录策略；见下节 |
| `remote_dependencies` | `{path, kind}` 数组，远程绝对路径；kind 为 file/directory/executable，运行前检查，不上传 |

| 资源字段 | 语义 |
| --- | --- |
| `cpus` | 默认 1，每任务 CPU；Slurm `--cpus-per-task`，LSF 总 slots = tasks × cpus |
| `tasks` | 默认 1，MPI 进程数；大于 1 必须显式声明 launcher |
| `nodes` | 默认 null；Slurm 省略按 1 生成，精确节点数不得超过 tasks；LSF 仅接受 null/1，不假装 ptile 能指定精确多节点数 |
| `tasks_per_node` | 可选正整数；Slurm 当前要求均匀布局 nodes × tasks_per_node = tasks；LSF 按 cpus 换算为 span[ptile=slots]，全任务同 host 时用 span[hosts=1] |
| `slurm_gres` | Slurm 原生 `gpu[:MODEL]:COUNT`，按节点申请；与 GPUs per task 互斥 |
| `slurm_gpus_per_task` | Slurm 原生 `[MODEL:]COUNT`，须使用 srun；站点需要支持相应 TRES 配置 |
| `slurm_constraint` | Slurm 节点 feature 表达式，限制为单行字面量字符集 |
| `lsf_gpu` | LSF 原生冒号分隔字符串，必须包含 num=N[/task\|host]；支持字段取决于站点版本，不转换为 Slurm |
| `queue` | Slurm partition／LSF queue；省略使用调度器默认值 |
| `memory_mb`／`memory_scope` | 必须一起指定；Slurm `job` 仅单节点，`per_node` 使用 `--mem`，`per_cpu` 使用 `--mem-per-cpu`；LSF 仅接受 `lsf_reservation`，生成显式 MB 的 `rusage[mem=...]` |
| `time_minutes` | 正整数分钟，渲染为调度器对应的时间格式 |
| `account`／`qos` | Slurm 专用标识，不自动转换为 LSF 参数 |
| `lsf_resource_requirement` | 可选 LSF 原生资源表达式，由用户核对站点支持，不自动转换到 Slurm |

默认保持单任务共享内存行为；可显式配置 MPI/GPU，不生成数组配置。LSF 内存预留按 slot 或 job 的作用范围由站点配置决定；预留不等于硬限制。[Slurm sbatch](https://slurm.schedmd.com/sbatch.html)、[LSF 资源要求](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=o-r)。

脚本使用 `set -euo pipefail`、引用的 source/export 和最后的 `exec`（启用 scratch 时由 EXIT trap 清理并保留原退出码），保留程序退出码。argv、环境值、重定向路径使用 Shell 引用；若用户显式选择 `bash -c` 等解释器，其内部代码仍由用户负责。初始化先执行，随后设置声明的环境变量；这些 compute 初始化与集群登录环境的 `init_scripts` 是分别配置的。声明的 stdout/stderr 父目录在远程执行时创建，stdin 与 stdout/stderr 不可同名。

`setup_steps` 最多 128 项，逐项引用且不改变原顺序：`{kind:"source",path:"/absolute/init.sh"}`、`{kind:"module_load",modules:["openmpi/4.1"]}`、`{kind:"module_purge"}`、`{kind:"export",name:"OMP_NUM_THREADS",value:"4"}`。module_load 最多 32 个字面模块名，不接受选项／命令替换；不自动初始化模块系统。该设置只在作业执行时运行；source 是用户确认的可信代码。声明的整数 OMP_NUM_THREADS 仍受 CPU 校验；容器暂不支持 ordered export steps，容器环境继续使用 environment/init_scripts。

`job_prepare_generated(cluster, input_dir, spec, outputs, project_root?, input_files?, script_name?, output_exclude?, input_exclude?, max_input_bytes?)` 把生成脚本只写入状态目录的输入快照，并返回原有 prepared run 与 `rendered`。历史中的 generation 保存有效资源、执行配置和警告，即使后续清理快照仍可核对当时的设置。默认 `script_name="hpc-mcp-job.sh"`，限定为非保留的文件名，不覆盖原输入。同样要求明确输出过滤；显式输入列表自动包含脚本和 stdin，其他依赖仍需声明。`job_submit` 才上传／执行。CLI `script-generate SCHEDULER SPEC.json`、`prepare-generated CLUSTER INPUT_DIR SPEC.json --output PATTERN ...`。

## MPI、容器与临时空间

`launcher.kind="srun"` 仅用于 Slurm，自动加 `--ntasks` 和 `--cpus-per-task`；`mpirun`／`mpiexec` 自动加 `-np`／`-n`。任务／CPU／节点计数不允许被 arguments 覆盖。srun 转发 mpi、cpu-bind、distribution、hint、label、unbuffered、exclusive、kill-on-bad-exit 选项；MPI 转发 map-by、bind-to、rank-by、mca、host、hostfile、oversubscribe 及部分 MPICH 的 bootstrap/bind-to/hostfile/genv/env 选项。不接受任意别名或多应用 config。`--map-by` 中 PE 须匹配 cpus，ppr:N:node 须匹配显式 tasks_per_node。

OpenMP 线程通过 environment 显式设置；整数 OMP_NUM_THREADS 不得超过 cpus。LSF slots 的 CPU 作用范围与 MPI host/bootstrap/binding 必须按实际站点配置；不会自动从分配槽位推导正确主机文件。[LSF span 语义](https://www.ibm.com/docs/SSWRJV_10.1.0/lsf_admin/span_string.html)、[Slurm GPU 资源](https://slurm.schedmd.com/gres.html)。LSF 自定义 resource requirement 不能再包含 span；声明 lsf_gpu 时也不能叠加 GPU 资源表达式。Gaussian 辅助不支持 MPI/Linda，通用生成器新增 MPI 不改变该限制。

`container` 字段：runtime 默认 apptainer，可选 singularity；image 必填远程绝对文件路径，运行前检查可读；binds 默认为空数组，每项为 `{source, destination, read_only=true}`，路径必须绝对且不能含逗号／冒号；gpu 可选 nv/rocm，须同时请求调度器 GPU。启动顺序为 launcher → container exec → command。运行时必须支持 --no-eval；声明环境通过 APPTAINERENV_／SINGULARITYENV_ 显式传入，避免镜像默认值覆盖及二次求值。自动以 rw 挂载 run cwd 并设置容器 cwd，保持相对输入／输出位置。镜像、初始化脚本和挂载内容是可信远程依赖，既不复制到输入快照，也不保证每个节点的可用性、镜像不可变或 MPI ABI 兼容。[Apptainer exec](https://apptainer.org/docs/user/latest/cli/apptainer_exec.html)、[环境与求值](https://apptainer.org/docs/user/latest/environment_and_metadata.html)、[SingularityCE exec](https://docs.sylabs.io/guides/4.6/user-guide/cli/singularity_exec.html)。

`scratch` 必填 root（远程绝对目录）；environment_variable 默认 TMPDIR，也可显式指定 GAUSS_SCRDIR 等非保留变量；cleanup 默认 true。执行时 mktemp 新建唯一私有目录，结束时只删除该新目录并保留程序退出码；cleanup=false 保留供诊断。scratch 不改变 cwd，也不自动回传其中数据；持久结果必须由程序写到 run 目录。容器中显式挂载 scratch，并传入对应变量。只支持能确定单 host 的布局；节点突然离线、SIGKILL 或磁盘故障可能留下目录。生成器不探测／创建远程 root，实际权限由运行环境决定。

## 已有脚本只读分析

`script_inspect(script_path, cluster=null, max_bytes=262144)`：默认读取本地 UTF-8 普通文件，拒绝文件符号链接；指定 cluster 时通过已配置 SSH 读取一个远程绝对路径，禁止遍历路径，不跟随文件链接，不递归取依赖。max_bytes 为 1..1048576，远程还受 ssh_output_limit 限制。配置中的可信登录 init_scripts 仍按正常 SSH 规则初始化，分析的源脚本不执行。

返回 source（位置、来源、大小、SHA-256）、带源码行号／原文／certainty 的 directives、evidence、commands、paths、unresolved、extracted_spec，以及 template_draft。Slurm 第一条可执行语句之后的指令标为 inactive；LSF stdin 模式的后置指令继续分析，重复资源采用后项，所有原始证据保留。调度指令内容是字面量，变量不会被展开；run 名、cwd 和调度器日志由 job_submit 覆盖。[Slurm sbatch](https://slurm.schedmd.com/sbatch.html)、[LSF 作业脚本](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=bsub-write-job-scripts)。

静态 Bash 子集支持常用资源、内存／整分钟时间、source 绝对路径、export、module load／purge、mkdir -p、单条 argv 命令及 stdin/stdout/stderr 重定向。交错的初始化及模块操作生成有序 setup_steps。命令替换、变量、分支、循环、多行、追加／复杂描述符、多个命令和未知选项保持 unresolved；引用中的动态符号也保守拒绝转换。没有 shebang 时解释器为 inferred，LSF -n 的 MPI／共享内存含义也必须核对。候选 argv 路径仅 inferred，不自动确定依赖。

`.py` 后缀或 Python shebang 选择 AST 分析：返回候选 constants、CLI parameters、嵌入配置和潜在副作用 evidence，保留行号和原文；不求值名称、调用、f-string、分支或生成器。AST 限 20000 节点、常量递归深度 12、容器元素 128，各报告类别最多 256 项；syntax warning／动态逻辑保留未解析证据，Python 不产生自动 template_draft。MCP／CLI 的 script_inspect 保存报告并返回 report_id，供 profile_draft 引用。

仅无未解析项、单调度器、单静态命令并有明确程序输出时返回通过生成器验证的模板草案；requires_review 始终 true，不证明 Shell 语义完全等价。agent 核对初始化顺序、资源、所有输入、输出和路径后，才能将 draft 作为 definition 传给 template_import；报告本身不是 definition。不改写原脚本，复杂场景仍可用 job_prepare 保存原文。CLI：`script-inspect FILE [--cluster NAME] [--max-bytes N]`。示例见 `examples/inspection/`。

## 模板与固定计划

模板为结构化 JSON 定义，包含必填 `scheduler`、`spec`、非空 `outputs`，以及可选 `parameters`、`script_name`、`input_files`、`input_exclude`、`output_exclude`、`max_input_bytes`。`parameters` 是名字到 `{type, default?, description?}` 的映射；支持 string、integer、boolean，没有 default 就是必填。`{{name}}` 全值占位保留参数类型，嵌入字符串时转为文本；只替换值、不替换键，不求值，不允许未知参数或未声明占位。资源整数应用全值占位，argv 和环境最终仍须是字符串。

| MCP 工具 | 行为 |
| --- | --- |
| `template_import(name, definition)` | 校验后在状态 SQLite 中创建新版本，保留已有版本及 SHA-256；不执行或导入任意脚本源码 |
| `template_list(limit=50, offset=0)` | 按名称分页列出每个模板的最新版本 |
| `template_get(name, version=null)` | 读取指定版本；省略版本取最新；校验存储内容的 SHA-256 |
| `template_plan(name, cluster, input_dir, parameters=null, version=null, project_root=null)` | 绑定参数、校验调度器、生成脚本并准备输入快照；不提交，返回 `plan_id=run_id` |
| `template_run(plan_id)` | 提交已经固定的计划，沿用提交锁、防重与不明回执恢复；不重新读取最新模板 |

每个计划保存模板名、版本、定义校验值、完整定义、参数和实际脚本；准备快照变化会阻止提交，原目录变化不会改变快照。模板更新对已有计划无影响；每次重新规划创建新 run。模板操作不自动修改计算输入卡，也不扫描其他任务。目录回传与缓存规则保持不变。

CLI 对应 `template-import NAME DEFINITION.json`、`template-list`、`template-get NAME [--version N]`、`template-plan NAME CLUSTER INPUT_DIR --parameters JSON [--version N] [--project-root A]`、`template-run PLAN_ID`。模板定义最大 256 KiB；名称最长 100 字符，使用字母／数字／点／下划线／短横线。此阶段尚未实现任意 Shell/Python 提交入口的源码导入。

## 设置层级

优先级为：本次工具参数 → 作业准备时保存的文件选择 → 集群默认值。传输超时、校验、压缩和布局默认使用当前集群配置。
`None`/参数省略表示继承；空列表表示明确不包含／不排除任何模式。所有生效的同步参数保存到 `sync_options` 和事件历史。

`settings_get()` 返回完整默认设置模板和运行时路径；`settings_get(cluster)` 返回有效设置；`cluster_configure(cluster, settings)` 接受部分 JSON 设置并原子保存 TOML，立即更新服务中的配置。
服务启动时的配置路径和状态路径分别由 `--config`/`HPC_MCP_CONFIG`、`--state-dir`/`HPC_MCP_STATE` 设置，不能在运行中移动历史数据库。

## 独立项目任务

`job_prepare` 新增 `project_root`（项目母目录 A）和 `input_files`（精确相对文件列表，自动包含提交脚本）。`input_dir` 必须在 A 内；省略列表则按原规则快照整个任务目录。每次准备创建独立作业，调用方负责选择输入、生成运行脚本和声明依赖。

指定 `project_root` 时必须提供非空 `outputs`，使用过滤同步。准备记录固定的项目和输入目录；默认回传到输入目录，按远程相对路径归位。可显式指定其他 `destination`。回传到输入目录仅允许 `error`／`merge`；`error` 按结果文件检查冲突，`merge` 覆盖同名结果，但两者都禁止覆盖上传清单中的输入和跟随目标符号链接。多文件安装并非整体原子操作，安装中断可能留下部分结果，错误会记录在历史中。

暂存在 `A/.hpc-mcp-sync/<run_id>/<attempt_id>/`，成功后移走或删除，并移除空父目录；失败暂存保留。`job_cache_cleanup(run_id, older_than_seconds=86400, dry_run=true)` 在作业锁下预览或清理过期暂存，返回路径及字节数。CLI 为 `cache-cleanup RUN_ID --older-than-seconds N [--apply]`。仅处理登记作业的暂存目录，不删除快照、历史、已安装结果或远程目录；不内置定时器。未指定项目的旧作业也可清理状态目录中的 `sync-attempts/`。

## 集群设置全表

| 字段 | 默认值／约束 | 用途 |
| --- | --- | --- |
| `ssh_host` | 必填，主机名或 SSH 别名 | 复用 OpenSSH 的用户、密钥、端口和跳板配置 |
| `scheduler` | 必填，`lsf`/`slurm` | 调度器适配 |
| `work_root` | 必填，远程绝对路径 | 独立执行目录的根位置 |
| `init_scripts` | `[]`，远程绝对路径数组 | 每次远程命令前执行的可信初始化文件 |
| `connect_timeout` | `10`，1–300 秒 | SSH 建连超时 |
| `command_timeout` | `30`，1–300 秒 | 单个远程命令超时 |
| `transfer_timeout` | `300`，1–86400 秒 | 单次 rsync 超时 |
| `ssh_output_limit` | `1048576`，正整数 bytes | 单个命令 stdout/stderr 的解析大小上限 |
| `output_mode` | `all`/`filtered`，默认 `all` | 全目录或按规则选择 |
| `output_include` | `["**"]` | filtered 模式包含规则 |
| `output_exclude` | `[]` | 排除规则，优先于包含规则 |
| `sync_layout` | `snapshot`/`direct`，默认 `snapshot` | 独立快照或直接 outputs 目录 |
| `sync_overwrite` | `error`/`replace`/`merge`，默认 `error` | 已存在目标的处理策略 |
| `transfer_checksum` | `true` | rsync 使用内容校验比较文件 |
| `transfer_compress` | `false` | rsync 传输压缩 |
| `max_input_bytes` | `1073741824`，正整数 bytes | 准备时允许的输入总大小 |
| `input_exclude` | `.git,.venv,__pycache__,.aws,.ssh,.codex,.agents,.hpc-mcp,.xn02,.hpc-mcp-sync,clusters.toml` 数组 | 输入排除，按文件名或相对路径 glob 匹配 |

修改 `ssh_host`、调度器、工作根目录或初始化文件不会迁移旧作业，旧作业会拒绝使用不一致的连接配置。
其他设置可更新后立即使用；从文件手动修改设置需重启当前 MCP 进程。
配置接口会规范化整个配置文件，不保留原 TOML 注释；不同写入操作使用文件锁避免丢失更新。

## 作业工具参数

| 工具 | 参数 |
| --- | --- |
| `job_prepare` | `cluster,input_dir,script,outputs?,output_mode?,output_exclude?,input_exclude?,max_input_bytes?` |
| `job_submit` | `run_id` |
| `job_status` / `job_recover` / `job_cancel` / `job_get` | `run_id` |
| `job_list` | `cluster?`, `limit=50`（1–500）, `offset=0` |
| `job_logs` | `run_id`, `stream=stdout`（stdout/stderr）, `lines=100`（1–1000） |
| `job_sync` | `run_id,mode?,includes?,excludes?,destination?,layout?,overwrite?,checksum?,compress?,timeout?` |

`outputs` 或 `includes` 显式传入时默认采用 filtered，除非另行指定 all。all 忽略包含列表，仍应用排除列表。
模式是相对于远程执行目录的 rsync glob：无斜杠的 `*.chk` 匹配各层文件名，`results/***` 匹配该目录全部内容。
空包含列表在 filtered 模式下选择零个文件；返回无匹配文件 warning。绝对模式、`..`、控制字符和反斜杠被拒绝。

## 同步目标与冲突语义

- `snapshot`：默认目标为 `state/run_id/outputs/同步编号/`，不覆盖旧同步。
- `direct`：默认目标为 `state/run_id/outputs/`。
- `destination`：指定本地目标，优先于 layout；输出直接置于该目录，不增加同步编号。
- `error`：目标已存在时拒绝，尚未发起下载。
- `replace`：先下载到 staging，成功后归档整个旧目标到 `state/run_id/sync-history/同步编号/`，再安装新结果。返回 `sync_backup`；可用该路径定位被移动的旧输出。
- `merge`：成功下载后合并目标，覆盖同名文件、保留没有下载的旧文件。`output_manifest` 只描述本次下载文件，不包含保留的旧文件。

输入目录、输入快照、历史数据库与文件系统根不能用作覆盖目标。目标中的符号链接被拒绝。
内部 `.hpc-mcp-*` 和旧版 `.xn02-*` 回执始终被排除；符号链接不下载，特殊文件不作为结果文件支持。这些是接口边界，不能通过 include 模式关闭。
失败下载保留 staging；续传和大小上限见下文。全部下载可能很大，先预览并设置相应上限。

`final=true` 需要本次成功查询到终止状态且传输成功；运行中或查询失败返回 partial。它表示所选文件传输完成，不保证应用计算正确或覆盖执行目录之外的输出。
非项目旧作业默认不写回输入目录；项目作业默认归位。同步不会自动清理远程结果，远程清理是独立工具。

## CLI 对应选项

```bash
hpc-mcp --config /abs/clusters.toml config-get lab
hpc-mcp --config /abs/clusters.toml config-set lab '{"sync_layout":"direct","sync_overwrite":"replace"}'
hpc-mcp prepare lab /abs/input job.sh --output-mode filtered --output '*.chk' --output '*.log' \
  --output-exclude '*.tmp' --input-exclude '*.bak' --max-input-bytes 2147483648
hpc-mcp sync RUN_ID --mode all --exclude '*.tmp' --destination /abs/results \
  --overwrite replace --checksum --compress --timeout 600
hpc-mcp sync RUN_ID --mode filtered --include 'results/***' --layout direct \
  --overwrite merge --no-checksum --no-compress
```

重复的列表选项构成替换列表，而不是追加默认列表；CLI 显式空列表通过 config-set JSON 配置，MCP 可以直接传 `[]`。
`--help` 可发现全部选项；CLI 输出 JSON，成功退出码 0，失败退出码 1。

## 操作可靠性

prepare 创建本地输入快照，不执行脚本或上传。submit 在校验快照、上传与远程 SHA-256 校验后提交，覆盖作业名、工作目录和调度日志路径。
调度 CPU、内存、队列、时限等资源配置目前写在实际脚本中，不提供任意 scheduler_options，也不改写程序内部路径。
上传的脚本和文件名不能含控制字符或反斜杠；符号链接和明显的数组指令被拒绝。运行数据、内部回执和状态目录不可作为普通输入。

提交意图保存在 SQLite，远程原子目录锁避免重复提交。响应丢失后只读回执恢复，不能盲目重试；回执缺失或响应无法解析维持 submission_unknown。
上传失败可重试，拒绝提交需重新 prepare；取消只是请求，状态查询确认最终结果。
Slurm 查询 squeue 再尝试 sacct。LSF 首先使用包含 `exit_reason` 的 `bjobs -a -o`，旧版本不支持该字段时回退到原三列格式；必要时使用 `bjobs -l` 补充终止原因，再尝试 `bacct -l -S` 和 `bhist -a -l -n 0 -S` 查询记账／事件归档。历史查询从准备日期前一天开始，涵盖时区差异；归档保留、权限和命令超时仍可能限制查询结果。

LSF 返回 `exit_code`、`exit_reason`、`termination_reason`、`signal` 及 `status_source`。仅 `EXIT` 且终止原因明确为 `TERM_OWNER`／`TERM_FORCE_OWNER`／`TERM_ADMIN`／`TERM_FORCE_ADMIN`／`TERM_BUCKET_KILL` 时归一化为 `cancelled`；限制超时、外部信号或原因不明的 `EXIT` 保持 `failed`。取消请求和退出码 130 本身不证明取消，原始状态仍保留为 `EXIT`。原因语义依据 [IBM 终止原因文档](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=logging-termination-reasons)。

长格式只解析匹配作业 ID 的块及调度器时间戳事件，有作业名时校验其是否为 run_id；重复记录和不支持的格式记入诊断，不猜测结果。已有实时状态或退出码与历史冲突时保留优先观测。所有查询失败时保留最后状态并返回错误，任务消失不推断成功。已有 `EXIT` 观测但无法补齐原因时仍返回该观测和失败查询诊断。

环境检查不会创建目录。队列可见不表示可以提交；共享目录可访问性需计算节点验证。LSF 槽位统计、Slurm 每节点内存及 GRES 原始字符串均保留调度器意义。
SSH 强制免交互和主机身份校验，身份参数在 OpenSSH 配置管理；本服务不存储密钥或密码。

## 常驻监控与自动整理

监控只作用于显式登记且已确认提交的 run，不扫描新输入，不调用提交／取消或远程清理。所有控制、任务策略和通知都保存在同一状态 SQLite；协调器使用独立进程和状态目录级 flock 单实例锁。MCP 连接退出不影响 detached 模式；本机停止时无法继续本地查询／下载，恢复后从已有身份和同步操作接续，不补发重复作业。

| 工具 | 参数／行为 |
| --- | --- |
| `monitor_watch` | run_ids（1..500）、auto_sync=true、sync_options=null、cleanup_policy=null、reset=false；固定有效同步规则、目标、覆盖及限制，仅登记、不启动 |
| `monitor_unwatch` | run_id；停用后续动作，保留记录；已登记同步 worker 或已开始清理可以完成，不取消远程任务 |
| `monitor_start` | settings=null；省略重用已保存设置，启动时捕获当前集群配置。重复启动返回 already_running，修改设置须先停；返回 starting 后查询状态 |
| `monitor_stop` | 无参数；请求优雅退出，不等待长查询；用 status 确认停止，已有 detached 同步 worker 独立完成 |
| `monitor_status` | limit=50（1..500）、offset=0；本地读取 alive、heartbeat、状态、最新循环、监控策略、进度、错误、最后调度观测和日志路径，不访问 SSH |
| `monitor_notifications` | after_id=0、limit=50（1..500）；返回终态整理、重试、需处理事件；保存 next_after_id 为下次游标，cursor_gap 表示旧通知已淘汰 |

同一策略重复 watch 不重置进度或重试计数；显式 reset=true 或改变策略才重新启用。阶段为 watching、syncing、retry_wait、complete、needs_attention、disabled。只有本轮有效查询确认终态才开始回传；状态缺失、解析失败或断网保留原调度观测并退避。成功、失败、取消均可回传日志；调度终态与 Gaussian 应用成功仍是不同判断。重试达到 sync_max_attempts 后停止自动同步，不自动把覆盖策略改为 merge。空输出选择也计为失败，防止无结果时清除快照。

每个状态查询窗口以 poll_interval_seconds 为最小间隔，同时限制任务数、SSH 命令数及并发。同集群／scope 的即时状态优先合并 squeue 或 bjobs；缺失记录、旧字段不支持及无原因的 LSF EXIT 走原有逐任务归档／原因回退，仍占查询预算。预算用完的任务延后且按上次实际查询时间公平推进，不使用陈旧成功状态作决定。worker 内的同步前后状态检查另受其超时及并行同步容量限制。[Slurm 查询接口](https://slurm.schedmd.com/squeue.html)、[LSF 多任务查询](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=reference-bjobs)。

`settings_get.monitor` 暴露以下全部协调器设置；未知字段、bool 代替整数或超界值拒绝：

| settings 字段 | 默认／范围 |
| --- | --- |
| poll_interval_seconds | 60，5..86400 |
| retry_initial_seconds / retry_max_seconds | 60 / 3600；5..86400 / 5..604800，最大不得小于初始；指数退避，实际推进仍受查询窗口和预算限制 |
| sync_max_attempts | 5，1..100；连续失败上限 |
| query_concurrency | 2，1..8；查询组并发上限，组内回退串行 |
| max_jobs_per_cycle | 50，1..500 |
| max_status_commands_per_cycle | 20，1..500；一个窗口内状态 SSH 命令预算 |
| max_sync_operations | 2，1..16；包括其他工具发起的 active worker；不会阻止用户另外手动启动 worker |
| batch_queries | true；可关闭以适配旧站点 |
| cleanup_interval_seconds | 3600，30..604800 |
| notifications_limit | 1000，10..10000；只淘汰通知，不删除作业历史 |
| input_cache_cleanup | null 默认关闭；可显式设置 older_than_seconds（默认 86400）及 max_cache_bytes（默认 10 GiB），均非负整数 |

watch 的 sync_options 同 job_sync，省略项在登记时解析并固定；stable_only 强制 true。已有 worker 会被接管查询，不重复启动；进程中断后沿已有操作记录与 resume 规则恢复，目标冲突保持报错。已有完整下载必须与请求规则匹配并逐文件 SHA 校验才复用；结果被用户修改后不会默默重新覆盖，进入 needs_attention。更新集群配置后重新启动协调器以刷新捕获配置；已准备 run 的目标身份保护仍生效。

cleanup_policy 默认 `{sync_cache_age_seconds:null, storage_categories:[], storage_age_seconds:604800}`，全部关闭。显式年龄为非负整数；storage_categories 只接受 snapshot、sync_history、old_outputs。每次清理都要求新确认终态、完整且非空的已下载结果及本地 SHA 校验；沿用作业锁与原有路径保护，保留最新项目结果和历史。snapshot 包括原始／有效输入，删除后不能重放。大结果校验会产生本地读取开销；输入内容缓存的全局保留策略是独立、显式启用的本地机制，不依赖远程查询。没有自动远程删除功能。

日志为 STATE/monitor.log，最多当前文件及两个约 1 MiB 的轮转文件；进程中断状态由 PID 和唯一实例标识判断，不会把 PID 复用当成协调器存活。heartbeat 是上次写入时间，长查询或本地校验期间可能滞后，alive 为实际进程身份检查。通知是本地可拉取数据，不发送桌面、邮件或 Slack 消息。

CLI：monitor-watch RUN_ID... [--no-auto-sync] [--sync-options JSON] [--cleanup-policy JSON] [--reset]；monitor-unwatch RUN_ID；monitor-start [--settings JSON]；monitor-stop；monitor-status [--limit N --offset N]；monitor-notifications [--after-id N --limit N]。monitor-run 使用前台进程及同一控制锁，供可选服务管理器调用；与 detached 模式不要同时启动。停机、更新及服务安装／卸载步骤见 README。systemd 用户服务可能在停止时终止同 cgroup 内的同步 worker，此时重启后按 interrupted／resume 规则恢复，不承诺该模式的 worker 跨服务停止继续执行。

## 更新检查与安装维护

- `update_check(force=false, max_age_seconds=86400, timeout=10)`：每个新会话调用一次，向用户报告可用更新。公开 HTTPS API 比较本地提交和官方仓库 main；不访问 SSH。`force` 跳过缓存，缓存最长 604800 秒，0 禁用复用；每次请求超时 1..60 秒，最多两个请求。GitHub 限流或离线返回 `ok=false`、`update_available=null`，不覆盖上次成功缓存。
- `update_plan` 接受同样参数，重新读取本地 Git 状态后返回 `check`、`blockers`、`commands`、`restart_required`。每条命令包含 `argv`（优先使用）、`cwd` 和供人阅读的 `display`。只为干净的官方 main editable/source 安装生成固定提交的快进方案，存在启动中／运行中／停止中的协调器或 queued／running 同步操作时阻止更新（先停止协调器，再查询／等待 worker 完成）；本地超前、分叉、其他 origin 或普通 wheel 安装需要人工处理。已经最新时 commands 为空。
- 工具不执行更新。停止 MCP 连接后再次核对 Git 工作区，备份配置和完整状态目录，依次执行命令，失败即停。重启使用相同绝对配置、状态路径，然后验证工具发现和历史读取。远程作业继续由调度器运行，无需重新提交。
- `settings_get.updates` 暴露默认检查参数及 `cached_check`。缓存是历史结果，可能过期；需要及时判断时调用 `update_check`。状态目录只多出一个小型 JSON 缓存，不修改任务数据。不自动配置定时任务或客户端文件，也不自动安装更新。

## Gaussian 接口

| 工具 | 参数／行为 |
| --- | --- |
| `gaussian_inspect` | `input_file`；只读最多 4 MiB 的 `.gjf`／`.com`，返回 SHA-256、分段、字段行号、确定性、资源和候选输出 |
| `gaussian_prepare` | `cluster,input_file,project_root,spec,outputs,changes?,dependencies?,allow_unresolved=false,max_input_bytes?`；只准备一个 run，提交使用 `job_submit` |
| `gaussian_result` | `run_id,log_path?,max_bytes=1048576`；必须先完成所选输出同步，只读取最新下载清单中的日志并核对哈希 |

`spec` 同生成器，可通过 `output_directories` 声明相对输出目录；Gaussian 自动补 checkpoint 父目录。默认 stdin 为卡名、stdout 为同名 `.log`。`outputs` 必须明确，`dependencies` 是相对输入列表。`changes` 只接受 cpus 正整数、memory 单位字符串、paths 对象；paths 的键可以是原始值或 `chk:原始值`／`oldchk:原始值`，值是执行目录内的相对路径。改写仅在快照内发生，各 Link1 段使用显式资源改写，并返回 diff、原始／有效哈希。原卡另存于 run_id/original-input/，application.original_snapshot 给出位置；original_manifest 记录校验值，原始副本不上传。snapshot 清理同时涵盖原始与有效快照。

检查 `%chk`、`%oldchk`、`%mem`、`%nprocshared`／`%nproc`；整数内存值支持 KB／MB／GB／TB 和 KW／MW／GW／TW，无单位按 8 字节 word，倍率按 1024。未知、动态或越界字段报告 unresolved，不求值；`allow_unresolved=true` 表示调用方已检查限制。未声明的 Gaussian 默认环境设置不推断。

本地 `%oldchk` 必须存在，显式映射的源也须在项目内；前序 Link1 产生的 checkpoint 不作为外部输入。首次 checkpoint-based route 需要明确旧 checkpoint，读入与输出应分开。路径不自动跟随环境变量、远程 home 或 symlink。资源检查覆盖显式 CPU 与 Slurm 内存冲突；LSF 预留作用范围及软件开销须核对站点。

`application_result` 独立于调度状态。日志使用完整流式扫描、SHA-256 校验和有上限的证据列表；`max_bytes` 为返回证据文本上限（1024..16777216），不是扫描文件大小上限。异常终止为 failed；正常终止数量匹配计划 Link1 段且日志末尾正常终止时 succeeded；否则 unknown。任意非 Gaussian 任务需显式指定其 Gaussian log，默认 stdout.log；工具提供终止证据，不验证所有化学结果。

## 同步预检、续传与异步操作

`job_sync` 增加 `resume?`、`max_file_bytes?`、`max_total_bytes?`、`reserve_bytes?` 和 `stable_only=false`。除 stable_only 外省略使用集群默认值：

| 集群设置 | 默认值 | 约束 |
| --- | --- | --- |
| `max_output_file_bytes` | 10737418240（10 GiB） | 正整数 |
| `max_output_bytes` | 21474836480（20 GiB） | 正整数 |
| `sync_reserve_bytes` | 268435456（256 MiB） | 非负整数 |
| `sync_resume` | true | boolean |
| `input_cache` | false | boolean；影响下一次准备，不迁移已有快照 |

`job_sync_preview(run_id,mode?,includes?,excludes?,max_file_bytes?,max_total_bytes?,reserve_bytes?,timeout?)` 使用 rsync dry-run，选择语义与实际传输相同；返回 files/path/size/mtime、count、bytes、free_bytes、required_free_bytes 和 blockers。只查询远程，临时本地预览目录自动删除。元数据输出受 `ssh_output_limit` 限制，超过时失败，不把截断清单当完整结果。

同步自动执行预检，估算两倍所选大小加 reserve 的空间需求；目标和暂存不同文件系统时分别检查。传输中周期性检查暂存占用，超过总量上限两倍则终止进程，增长可能有检查间隔内的超量，非硬磁盘配额。rsync 单文件限制及下载后的路径／大小复核避免静默遗漏被当作成功。

失败暂存只有在完整选项及源清单 fingerprint 一致时复用；拒绝 symlink，私有 `.hpc-mcp-transfer-partial` 从不安装成结果。源变化或选项变化创建新暂存。传输完成后重新查询状态并比对源大小／mtime，再安装结果；这不是远程原子快照，元数据不变的并发写入仍可能无法识别。`stable_only=true` 要求前后都确认终态，否则保留缓存返回失败。默认允许运行中同步，但结果是 partial。`sync_transfer_stats` 可报告 rsync 统计的 transferred_bytes／reused_bytes；`sync_progress` 报告暂存或已安装大小，不代表网络实时速度。

`job_sync_start(run_id,options={})` 接受公开 job_sync 参数对象，启动独立进程，返回持久 operation_id。`job_sync_operation(operation_id)` 返回 queued/running/succeeded/failed/interrupted、progress 和 result/error。一个 run 仅允许一个已登记活动操作；中断后先查询再重试。重启 MCP 不终止既有 worker；机器关机不会继续传输，未结束操作需核查后恢复，不会自动提交计算任务。操作历史保存配置快照，不存 SSH 密钥；run.last_sync_operation 保存最近操作编号，agent 重启后可从 job_get 找回。

CLI：`sync-preview` 对应预览参数，`sync` 增加 `--[no-]resume`、`--max-file-bytes`、`--max-total-bytes`、`--reserve-bytes`、`--stable-only`；`sync-start RUN_ID --options JSON`，`sync-operation OPERATION_ID` 查询状态。

## 存储与远程清理

| 工具 | 默认参数与边界 |
| --- | --- |
| `job_cache_cleanup` | 既有失败暂存清理，older_than_seconds=86400，dry_run=true；不删除输入／结果 |
| `job_storage_cleanup` | run_id,categories=["sync_history"],older_than_seconds=86400,dry_run=true；categories 可选 snapshot／sync_history／old_outputs，只处理终态且同步 complete 的任务 |
| `input_cache_cleanup` | older_than_seconds=86400,max_cache_bytes=10737418240,dry_run=true；按年龄或容量从最旧的共享 blob 开始清理 |
| `job_remote_cleanup` | run_id,dry_run=true；重新确认终态、验证非空已下载结果哈希和原提交回执，检查远程 canonical 路径后才允许删除 |

本地清理保留数据库、manifest、模板和应用证据元数据，保护最新结果及项目中已归位的输出。删除 snapshot 会记录 snapshot_removed，之后无法从这些文件重放；旧输出／归档删除不可恢复。所有本地清理受操作锁约束，拒绝路径越界和 symlink。

开启 input_cache 后，输入先以 SHA-256 存为 blob，再独立复制／reflink 到快照；不 hardlink 原输入、缓存或其他快照，修改一份不会污染另一份。缓存复用前校验内容；不支持 CoW 时会额外复制占用磁盘。清理缓存不删除快照，维护事件写入 events 的 maintenance 标识。默认不启用，也不创建后台计时器。

远程清理默认预览目录用量。dry_run=false 永久删除本 run 中未回传的文件，先确认已选择全部需要保留的数据；成功记录 remote_removed，不修改其他目录。同步完成只表示所选文件完整，并不表示已下载所有输出。CLI `storage-cleanup RUN_ID --category ...`、`input-cache-cleanup`、`remote-cleanup RUN_ID`，对应删除需显式加 `--apply`。
