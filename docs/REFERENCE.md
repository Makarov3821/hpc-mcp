# Agent 接口与设置参考

MCP 字典工具同时提供 JSON 文本内容及 `structuredContent` 对象，方便 agent 与程序客户端消费；22 个工具通过 `tools/list` 暴露参数 schema。协议验证见 `tests/test_stdio.py`，需要安装官方 MCP extra；验证只操作临时配置、快照、模板和历史，不访问集群。

## 单任务脚本生成

`script_generate(scheduler, spec)` 纯预览，不执行命令或写文件。`spec` 接受以下字段，未知字段拒绝：

| 字段 | 内容 |
| --- | --- |
| `command` | 必填非空字符串数组，程序及 argv；不是待解析的 Shell 命令 |
| `resources` | 下表中的资源设置，默认 CPU 为 1 |
| `environment` | 环境变量名到字符串值的对象，默认空对象 |
| `init_scripts` | 计算节点上可信初始化文件的绝对路径数组，默认空数组 |
| `stdin`／`stdout`／`stderr` | 可选的执行目录内相对文件路径；不允许绝对路径或 `..` |

| 资源字段 | 语义 |
| --- | --- |
| `cpus` | 正整数；Slurm 单 task 的 `--cpus-per-task`，LSF `-n` 并限制单 host |
| `queue` | Slurm partition／LSF queue；省略使用调度器默认值 |
| `memory_mb`／`memory_scope` | 必须一起指定；Slurm `job` 使用单节点 `--mem`，`per_cpu` 使用 `--mem-per-cpu`；LSF 仅接受 `lsf_reservation`，生成显式 MB 的 `rusage[mem=...]` |
| `time_minutes` | 正整数分钟，渲染为调度器对应的时间格式 |
| `account`／`qos` | Slurm 专用标识，不自动转换为 LSF 参数 |
| `lsf_resource_requirement` | 可选 LSF 原生资源表达式，由用户核对站点支持，不自动转换到 Slurm |

当前只生成单节点共享内存作业，不生成 MPI/GPU/数组配置。LSF 内存预留按 slot 或 job 的作用范围由站点配置决定；预留不等于硬限制。[Slurm sbatch](https://slurm.schedmd.com/sbatch.html)、[LSF 资源要求](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=o-r)。

脚本使用 `set -euo pipefail`、引用的 source/export 和最后的 `exec`，保留程序退出码。argv、环境值、重定向路径使用 Shell 引用；若用户显式选择 `bash -c` 等解释器，其内部代码仍由用户负责。初始化先执行，随后设置声明的环境变量；这些 compute 初始化与集群登录环境的 `init_scripts` 是分别配置的。声明的 stdout/stderr 父目录在远程执行时创建，stdin 与 stdout/stderr 不可同名。

`job_prepare_generated(cluster, input_dir, spec, outputs, project_root?, input_files?, script_name?, output_exclude?, input_exclude?, max_input_bytes?)` 把生成脚本只写入状态目录的输入快照，并返回原有 prepared run 与 `rendered`。默认 `script_name="hpc-mcp-job.sh"`，限定为非保留的文件名，不覆盖原输入。同样要求明确输出过滤；显式输入列表自动包含脚本和 stdin，其他依赖仍需声明。`job_submit` 才上传／执行。CLI `script-generate SCHEDULER SPEC.json`、`prepare-generated CLUSTER INPUT_DIR SPEC.json --output PATTERN ...`。

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
失败下载保留 staging；跨尝试续传和输出大小上限尚未实现。全部下载可能很大，应根据作业结果选择模式和超时。

`final=true` 需要本次成功查询到终止状态且传输成功；运行中或查询失败返回 partial。它表示所选文件传输完成，不保证应用计算正确或覆盖执行目录之外的输出。
输出默认不自动写回原输入目录；远程结果不会被清理。

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

## 更新检查与安装维护

- `update_check(force=false, max_age_seconds=86400, timeout=10)`：每个新会话调用一次，向用户报告可用更新。公开 HTTPS API 比较本地提交和官方仓库 main；不访问 SSH。`force` 跳过缓存，缓存最长 604800 秒，0 禁用复用；每次请求超时 1..60 秒，最多两个请求。GitHub 限流或离线返回 `ok=false`、`update_available=null`，不覆盖上次成功缓存。
- `update_plan` 接受同样参数，重新读取本地 Git 状态后返回 `check`、`blockers`、`commands`、`restart_required`。每条命令包含 `argv`（优先使用）、`cwd` 和供人阅读的 `display`。只为干净的官方 main editable/source 安装生成固定提交的快进方案；本地超前、分叉、其他 origin 或普通 wheel 安装需要人工处理。已经最新时 commands 为空。
- 工具不执行更新。停止 MCP 连接后再次核对 Git 工作区，备份配置和完整状态目录，依次执行命令，失败即停。重启使用相同绝对配置、状态路径，然后验证工具发现和历史读取。远程作业继续由调度器运行，无需重新提交。
- `settings_get.updates` 暴露默认检查参数及 `cached_check`。缓存是历史结果，可能过期；需要及时判断时调用 `update_check`。状态目录只多出一个小型 JSON 缓存，不修改任务数据。不自动配置定时任务或客户端文件，也不自动安装更新。
