# Agent 接口与设置参考

MCP 字典工具同时提供 JSON 文本内容及 `structuredContent` 对象，方便 agent 与程序客户端消费；33 个工具通过 `tools/list` 暴露参数 schema。协议验证见 `tests/test_stdio.py`，需要安装官方 MCP extra；验证只操作临时配置、快照、模板和历史，不访问集群。

## 单任务脚本生成

`script_generate(scheduler, spec)` 纯预览，不执行命令或写文件。`spec` 接受以下字段，未知字段拒绝：

| 字段 | 内容 |
| --- | --- |
| `command` | 必填非空字符串数组，程序及 argv；不是待解析的 Shell 命令 |
| `resources` | 下表中的资源设置，默认 CPU 为 1 |
| `environment` | 环境变量名到字符串值的对象，默认空对象 |
| `init_scripts` | 计算节点上可信初始化文件的绝对路径数组，默认空数组 |
| `stdin`／`stdout`／`stderr` | 可选的执行目录内相对文件路径；不允许绝对路径或 `..` |
| `output_directories` | 可选相对目录数组，在执行时 mkdir；默认空数组 |

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

## 更新检查与安装维护

- `update_check(force=false, max_age_seconds=86400, timeout=10)`：每个新会话调用一次，向用户报告可用更新。公开 HTTPS API 比较本地提交和官方仓库 main；不访问 SSH。`force` 跳过缓存，缓存最长 604800 秒，0 禁用复用；每次请求超时 1..60 秒，最多两个请求。GitHub 限流或离线返回 `ok=false`、`update_available=null`，不覆盖上次成功缓存。
- `update_plan` 接受同样参数，重新读取本地 Git 状态后返回 `check`、`blockers`、`commands`、`restart_required`。每条命令包含 `argv`（优先使用）、`cwd` 和供人阅读的 `display`。只为干净的官方 main editable/source 安装生成固定提交的快进方案，存在 queued／running 同步操作时阻止更新（先查询／等待完成）；本地超前、分叉、其他 origin 或普通 wheel 安装需要人工处理。已经最新时 commands 为空。
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
