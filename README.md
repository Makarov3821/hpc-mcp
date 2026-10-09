# hpc-mcp：Agent 安装与接入指南

项目仓库：[Makarov3821/hpc-mcp](https://github.com/Makarov3821/hpc-mcp)。

这是供 Codex、OpenCode 等 agent 使用的本地 stdio MCP 服务。通过用户已有的 SSH 配置管理 LSF／Slurm 集群：环境检查、队列查询、普通批处理作业提交、历史、状态、日志、取消和结果同步。

**Agent 应按下面顺序安装、配置并验收。** 不需要 OpenAI API key；MCP 服务本身不调用模型 API。完整参数和操作语义见 [接口参考](docs/REFERENCE.md)。

## 1. 检查前置条件

确认仓库绝对路径、可写的配置文件路径、固定的本地状态目录，以及用户指定的 SSH 别名。已有配置优先复用；新增集群可先探测调度器，再请用户确认工作目录和应用运行方式，不猜测软件安装位置。

安装 agent 应把源码 checkout／虚拟环境与运行数据分开。配置、历史及输入快照放在用户选定的 agent 数据目录，例如 Codex 使用 `/home/USER/.codex/hpc-mcp/clusters.toml` 和 `/home/USER/.codex/hpc-mcp/state/`。其他客户端按其实际数据目录选择独立的 `hpc-mcp/` 子目录，或复用同一状态目录共享历史。不要默认把这些数据留在源码仓库或项目 A 中；始终在注册命令里指定 `--config`、`--state-dir` 的绝对路径。目录不可写时报告安装限制，不擅自更改客户端权限。

本地需要 Python 3.11+、OpenSSH、rsync，系统为 Linux/macOS；远程需要 Bash、rsync、sha256sum 和对应调度命令。

```bash
python3 --version
command -v ssh
command -v rsync
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes YOUR_SSH_ALIAS 'hostname; id -un'
```

SSH User、IdentityFile、Port、ProxyJump 留在用户的 OpenSSH 配置中。主机密钥未登记时先按用户环境核验，不自动关闭主机校验。远程工作目录应已存在并可写，计算节点能访问该目录。

## 2. 安装本地服务

首次安装从项目仓库克隆，再在仓库根目录安装。已有 checkout 时直接使用其目录，保留已有虚拟环境与配置：

```bash
git clone git@github.com:Makarov3821/hpc-mcp.git
cd hpc-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-mcp.lock -e '.[mcp]'
.venv/bin/hpc-mcp --help
```

未配置 GitHub SSH 身份时可使用 `git clone https://github.com/Makarov3821/hpc-mcp.git`。这与登录计算集群所需的 SSH 配置分别管理。

安装使用官方 Python MCP SDK v2（`mcp>=2,<3`）。若下载失败，报告依赖安装未完成，不将能运行标准库 CLI 当成 MCP 安装成功。

`requirements-mcp.lock` 保存已验收的 SDK 及依赖版本（当前为 SDK 2.3.0，Linux／Python 3.14 环境），通过 `-c` 约束安装。其他 Python 版本与平台仍需安装并运行协议测试验收；遇到不兼容时报告具体依赖，不静默忽略约束。项目自身仍支持 Python 3.11+。

后续所有示例中的 `/ABS/REPO`、`/ABS/STATE`、`YOUR_SSH_ALIAS` 和路径均替换为实际值。MCP 启动命令必须使用绝对路径，避免依赖当前目录、激活的虚拟环境或 `PYTHONPATH`。

发行包、CLI 命令和 MCP 注册名统一为 `hpc-mcp`，Python 模块名为 `hpc_mcp`。CLI 默认读取当前目录的 `clusters.toml`，状态存放在 `.hpc-mcp/`；也可使用 `HPC_MCP_CONFIG`、`HPC_MCP_STATE` 环境变量或对应命令行参数指定路径。注册客户端时显式指定固定的绝对路径。

## 3. 配置集群

新集群先完成以下闭环；仅有 SSH 别名即可开始，`serve` 可以在指定配置文件尚不存在时启动空服务：

1. `cluster_probe(ssh_host)`：保存登录信息、调度器候选和队列信息。软件环境发现最多查询一次有大小限制的 `module avail`；不扫盘、不递归搜索、不加载模块。多个调度器或命令不可见时请用户选择／提供可信登录初始化文件。
2. 用户提供常用脚本后调用 `script_inspect`，取得持久 `report_id`。Bash 支持静态指令和有序 source／export／module 设置；Python 提交器仅提取常量、参数声明、嵌入脚本及副作用证据，不导入或执行原程序。缺失、动态环境请用户补充；可用 `cluster_probe(paths=[明确绝对路径])` 检查具体路径，不扩展搜索。
3. 用下述 `cluster_configure`／`config-set` 保存连接设置；再整理应用模板定义，通过 `profile_draft(name, cluster, application, definition, report_ids)` 保存待审版本。资源事实、用户默认值和本次任务参数分别解释；同一集群可保存 Gaussian、MPI、容器等不同运行配置。
4. 向用户展示完整定义、有效默认资源、环境顺序、输入输出和未解析项。用户确认后，调用 `profile_confirm(profile_id, review_token, confirmation_note)`，记录确认并设置该集群／应用的默认配置。确认不表示计算节点验证通过。
5. `profile_validate(profile_id, command=[用户确认的短验证命令], parameters=参数)` 只准备小作业，使用该配置的资源、环境和启动器；先展示渲染脚本及资源，用户授权后才调用 `job_submit`。随后用 `profile_validate(profile_id, run_id=编号)` 查询状态、回传有限大小的日志并记录验证证据。排队、失败或证据不足不会标为验证通过。
6. 日常使用 `profile_plan(input_dir, cluster, application, parameters, project_root)` 准备独立任务，再用已有提交、状态与同步工具执行。默认配置固定模板版本和来源；单个任务覆盖只能使用声明参数。Gaussian 卡先用 `gaussian_inspect` 核对核数、内存和 checkpoint，需改写时仍用 Gaussian 专用辅助。

CLI 探测示例（`--queue-details` 可选择附加原始队列限制信息）：

```bash
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/AGENT/hpc-mcp/clusters.toml \
  --state-dir /ABS/AGENT/hpc-mcp/state cluster-probe YOUR_SSH_ALIAS
```

探测、脚本报告、确认、模板和验证记录存入同一 agent 状态 SQLite，可通过 `onboarding_report`、`profile_get` 和 `profile_list` 在重启后读取。示例 [Gaussian LSF 配置](examples/profiles/gaussian-lsf.json) 的路径、队列和资源仅为占位示范，必须按用户脚本调整；LSF 内存预留作用范围仍需站点确认。验证仅覆盖具体命令、参数和资源布局；改变参数会在任务历史中标为未验证，修改集群设置需创建新配置版本。远程软件内容变化无法通过本地读取自动发现，站点变更后应显式重新验证。

已有 `clusters.toml` 时先读取。新增或更新配置可以使用 CLI 的 JSON 设置接口：

```bash
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/REPO/clusters.toml config-set lab \
  '{"ssh_host":"YOUR_SSH_ALIAS","scheduler":"lsf","work_root":"/shared/home/user/jobs","output_mode":"all"}'
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/REPO/clusters.toml config-get lab
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/REPO/clusters.toml check lab
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/REPO/clusters.toml info lab
```

Slurm 使用 `"scheduler":"slurm"`。命令只在登录环境初始化后可用时，设置 `init_scripts` 为远程初始化文件的绝对路径。该文件会被执行，不能放入作业提交等副作用。

配置接口只更新指定设置、保留其他集群，写入前完整校验。也可使用 [TOML 示例](clusters.example.toml)。配置和状态目录无需放入版本控制。

`check` 的 `ok:true` 表示必要命令、目录权限和队列查询通过；共享存储、队列提交授权仍需实际作业验证。SSH 失败、解析失败和 warning 均应分别报告。

## 4. 注册到 Agent 客户端

只修改所选客户端的对应条目，保留用户其他设置。客户端目录不可写时提供准备好的配置片段，明确说明注册尚未完成。

### Codex

可以使用官方 CLI 注册命令：

```bash
codex mcp add hpc-mcp -- /ABS/REPO/.venv/bin/hpc-mcp \
  --config /ABS/REPO/clusters.toml --state-dir /ABS/STATE serve
codex mcp list
```

如需指定超时，可在用户或项目 Codex 配置的相应条目中设置：

```toml
[mcp_servers.hpc-mcp]
command = "/ABS/REPO/.venv/bin/hpc-mcp"
args = ["--config", "/ABS/REPO/clusters.toml", "--state-dir", "/ABS/STATE", "serve"]
startup_timeout_sec = 20
tool_timeout_sec = 1200
```

重启对应客户端后用 `/mcp` 检查连接。[官方 Codex MCP 文档](https://developers.openai.com/codex/mcp)

### OpenCode

合并到所选作用域的 `opencode.json`／`opencode.jsonc`，不要覆盖整个文件：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "hpc-mcp": {
      "type": "local",
      "command": ["/ABS/REPO/.venv/bin/hpc-mcp", "--config", "/ABS/REPO/clusters.toml", "--state-dir", "/ABS/STATE", "serve"],
      "enabled": true,
      "timeout": 20000
    }
  }
}
```

OpenCode 的 `timeout` 是工具发现超时（毫秒）；长耗时调用的执行限制需按所用版本另行确认。Codex 的 `tool_timeout_sec` 则应覆盖 SSH、传输和校验耗时。[官方 OpenCode MCP 文档](https://opencode.ai/docs/mcp-servers/)

## 5. 验收 MCP 工具与作业流程

每个新会话先调用 `update_check`，若 `update_available=true`，向用户提示上游提交并用 `update_plan` 获取更新命令；网络失败代表未知，不应说已是最新。然后从客户端调用 `settings_get`、`cluster_list`、`cluster_check`、`cluster_info`。`settings_get` 暴露完整设置模板、配置路径和状态路径；`cluster_configure` 可持久化更新集群，立即作用于当前服务。

用户授权测试计算后，使用符合目标队列的微型脚本（[LSF 示例](examples/hello-lsf/job.sh)、[Slurm 示例](examples/hello-slurm/job.sh)）：

1. `job_prepare(cluster, input_dir, script)`：返回本地快照和计划，不提交；检查上传清单与实际提交命令。
2. `job_submit(run_id)`：上传、校验、提交，取得 `job_id`。响应不明时调用 `job_recover`，不要创建重复作业。
3. `job_status`、`job_logs`：确认调度状态和日志；避免高频轮询。
4. `job_sync`：取回结果，查看 `output_dir`、`output_manifest`、`sync_options` 和 `final`。
5. 重启 MCP 后用 `job_list`／`job_get` 验证同一状态目录下的历史仍可读。

新作业默认同步执行目录中的全部普通文件（也包含上传的输入）；内部回执、符号链接不下载。已有作业保留旧的过滤设置。Agent 可以在每次同步时覆盖选择：

```json
{
  "run_id": "r_实际编号",
  "mode": "filtered",
  "includes": ["*.log", "*.chk", "results/***"],
  "excludes": ["results/tmp/***"],
  "layout": "direct",
  "overwrite": "replace",
  "checksum": true,
  "compress": false,
  "timeout": 600
}
```

`direct` 将最新结果直接放在 `run_id/outputs/`；`replace` 会先把旧目录归档到 `sync-history/`。`snapshot` 为每次同步创建独立目录。也可传 `destination` 指定其他本地目标；具体冲突及过滤规则见 [接口参考](docs/REFERENCE.md)。

安装验收应报告：客户端是否真正连接、发现了哪些工具、集群检查结果，以及作业提交／结果同步是否实际验证。仅运行 `serve` 没有输出是等待 stdio 请求，不能据此判定握手成功。

### 在原项目目录中提交独立任务与回传

一个输入卡或任务目录对应一次 `job_prepare` 和一个独立远程 `r_*` 目录。agent 自行识别用户选定的任务、生成对应运行脚本，再逐个调用；服务不扫描或自动提交其他输入卡，也不自动生成 Gaussian 脚本。

例如项目根目录为 `/ABS/A`，输入卡为 `/ABS/A/B/test1.gjf`，agent 在同目录准备仅运行该输入的 `test1-job.sh`。用下面的 MCP 参数准备：

```json
{
  "cluster": "lab",
  "project_root": "/ABS/A",
  "input_dir": "/ABS/A/B",
  "script": "test1-job.sh",
  "input_files": ["test1.gjf"],
  "outputs": ["test1.log", "test1.chk"],
  "max_input_bytes": 16777216
}
```

`input_files` 为精确的相对文件列表，提交脚本自动包含；续算所需的 chk 等依赖必须明确加入。省略它则快照整个任务目录，按输入排除规则过滤。`max_input_bytes` 限制保存的输入体积，超限会拒绝准备；快照不一定小，不能将大型依赖误当作无成本缓存。Gaussian 的 `%chk` 和运行命令必须与所声明的输出路径一致。

提交后使用返回的 `run_id` 查询状态、日志和同步。项目模式必须声明非空的过滤输出；`job_sync(run_id)` 默认将上述结果放回 `/ABS/A/B/test1.log`、`test1.chk`，保留远程相对路径。已有同名结果默认报错；明确使用 `overwrite="merge"` 可覆盖，上传清单中的输入仍受保护。项目目录回传不允许 `replace`，以免归档整个输入目录。多个任务应使用各自的输出名，避免通配符匹配公共 `stdout.log` 引起冲突。

同步暂存位于 `/ABS/A/.hpc-mcp-sync/<run_id>/<attempt_id>/`，成功安装结果后删除暂存并移除空父目录，不额外保存输出副本到 agent 状态目录。失败暂存保留以供排查。原有未指定 `project_root` 的作业继续使用原同步布局；显式 `destination` 也仍可选择其他结果位置。

清理失败／过期暂存可调用 `job_cache_cleanup(run_id, older_than_seconds=86400, dry_run=true)` 预览，再用 `dry_run=false` 执行。CLI 对应：

```bash
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/CONFIG --state-dir /ABS/STATE \
  cache-cleanup RUN_ID --older-than-seconds 86400
# 实际清理：在同一命令末尾添加 --apply
```

清理与提交／同步共用作业锁，仅处理该作业的同步暂存；不会删除输入快照、历史、已回传文件或远程数据。需要定时时，由 agent 按用户要求配置系统定时器重复执行指定作业的清理命令；默认不启动后台协调器；需要服务内自动保留策略时显式使用下节 monitor 工具。相同选择规则、目标和远程文件指纹下，失败下载可复用暂存续传；规则或文件变化时创建新尝试。

## 6. 生成单任务脚本与复用模板

`script_generate(scheduler, spec)` 只返回可审查的 Bash 脚本。`spec.command` 是程序和参数组成的列表，可指定 `stdin`／`stdout`／`stderr` 相对路径、`environment` 和计算节点使用的远程绝对 `init_scripts`。`resources` 支持队列／分区、CPU、内存和时间；完整字段见 [接口参考](docs/REFERENCE.md)。默认生成单任务共享内存脚本，也支持显式 MPI 布局、GPU 和容器；每个计划仍对应一个独立调度作业。

例如直接准备 Gaussian 作业，脚本只写入 agent 状态目录中的输入快照，原目录不新增脚本：

```json
{
  "cluster": "lab",
  "project_root": "/ABS/A",
  "input_dir": "/ABS/A/B",
  "spec": {
    "command": ["g16"],
    "stdin": "test1.gjf",
    "stdout": "test1.log",
    "resources": {"queue": "USER_QUEUE", "cpus": 4, "time_minutes": 60}
  },
  "input_files": ["test1.gjf"],
  "outputs": ["test1.log", "test1.chk"]
}
```

调用 `job_prepare_generated` 后检查返回的 `rendered.script`、上传清单和提交命令，再用 `job_submit(run_id)` 提交。显式输入列表会自动加入生成脚本和声明的 stdin；其他依赖须自行列出。生成脚本默认名 `hpc-mcp-job.sh`，存在同名输入时拒绝准备。

反复使用时导入 [LSF 模板](examples/templates/gaussian-lsf.json) 或 [Slurm 模板](examples/templates/gaussian-slurm.json)：

```bash
/ABS/REPO/.venv/bin/hpc-mcp --state-dir /ABS/STATE \
  template-import gaussian examples/templates/gaussian-lsf.json
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/CONFIG --state-dir /ABS/STATE \
  template-plan gaussian lab /ABS/A/B --project-root /ABS/A \
  --parameters '{"input":"test1.gjf","stem":"test1","checkpoint":"test1.chk","queue":"USER_QUEUE","cpus":4}'
# 核对计划后执行，PLAN_ID 是上一步返回的 plan_id：
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/CONFIG --state-dir /ABS/STATE template-run PLAN_ID
```

MCP 对应 `template_import(name, definition)`、`template_plan(...)`、`template_run(plan_id)`；`template_list`／`template_get` 查询保存的模板和历史版本。CLI 导入文件路径需替换成实际绝对路径或在仓库根目录执行。模板保存在同一个状态数据库中，每次导入创建新版本。参数通过 `{{name}}` 绑定，支持 string／integer／boolean；没有默认值的参数必须提供，不执行表达式。

`template_plan` 会创建本地输入快照，但不上传或提交；`plan_id` 就是 `run_id`。计划固定模板版本、参数、最终脚本和输入清单，之后编辑模板或原输入不会改变它。`template_run` 只执行该计划，重复调用遵循已有提交防重与回执恢复规则；重新准备或重跑计算应创建新计划。状态、日志、回传和缓存清理继续使用同一个 `run_id`。

Gaussian 示例不解析或修改输入卡。agent 应核对实际 `%chk`、`%nprocshared`、内存、运行时间以及计算节点的 g16 环境；续算 chk 等依赖必须加入模板输入列表。Slurm 内存区分 `job` 与 `per_cpu`；LSF 要求明确使用 `lsf_reservation`，其预留作用范围由站点配置决定，不表示硬内存限制。普通 Bash 脚本可先只读分析；复杂 Python 入口与任意 Shell 自动导入仍未实现。

### 分析现有脚本与准备 MPI／GPU 作业

先调用 `script_inspect(script_path)`；远程脚本传 cluster 和远程绝对路径。也可运行：

```bash
/ABS/REPO/.venv/bin/hpc-mcp script-inspect /ABS/REPO/examples/inspection/slurm.sh
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/CONFIG script-inspect /remote/job.sh --cluster lab
/ABS/REPO/.venv/bin/hpc-mcp script-generate slurm /ABS/REPO/examples/resources/mpi-slurm.json
```

分析不会执行源码，返回 SHA、行号和未解析项，并保存可追溯的 report_id。Bash 的 template_draft 只是待审草案；agent 核对后可纳入 profile_draft，或直接导入独立模板。变量、分支、多个命令和 Python 入口不自动转换；Python AST 已支持候选常量、参数声明、嵌入配置及副作用提取，`used-scripts/` 的三个提交器已有只读分析测试，完整兼容适配仍待开发。原始批处理脚本可继续用 job_prepare 保存原文，Python 提交器不能直接作为批处理脚本提交。

MPI 使用 tasks（进程数）、cpus（每进程 CPU）、nodes／tasks_per_node 和 launcher；OpenMP 线程在 environment 明确设置。Slurm 可指定精确均匀节点布局；LSF 按总 slots 和每 host 的 ptile 表达，不声称精确多节点数。GPU 使用 scheduler 专用 slurm_gres／slurm_gpus_per_task／lsf_gpu，容器支持可信远程 Apptainer／Singularity 镜像、显式挂载和单节点 scratch。镜像及软件不随任务上传；scratch 不改变输出 cwd，不自动回传中间文件。

[资源示例](examples/resources/) 中的远程路径、GPU 型号、启动器参数都是占位，agent 应先取得用户的站点设置，核对生成脚本与输入清单再准备／提交；MPI 主机发现、CPU binding、容器 ABI 和共享存储需要目标环境验证。参数和兼容限制见 [接口参考](docs/REFERENCE.md)。

### Gaussian 单任务与应用结果

agent 先调用 `gaussian_inspect(input_file)`，读取 Link0、Link1、CPU／内存、checkpoint 引用和候选输出。报告保留行号及未解析项；不扫描或提交其他卡，不猜测全部依赖。再用 `gaussian_prepare` 创建独立计划，确认后通过原有 `job_submit` 提交。

```bash
hpc-mcp gaussian-inspect /A/B/test1.gjf
hpc-mcp --config /ABS/AGENT/clusters.toml --state-dir /ABS/AGENT/state \
  gaussian-prepare lab /A/B/test1.gjf /ABS/hpc-mcp/examples/gaussian/spec-lsf.json \
  --project-root /A --output test1.log --output test1.chk
```

示例中的 `lab`、队列、程序命令、计算初始化文件和资源都必须按实际集群调整；[Gaussian 示例](examples/gaussian/)提供 LSF／Slurm 两种 spec 和最小输入卡，不代表已在目标集群运行。声明 CPU 不得少于输入卡要求；Slurm 内存会检查显式冲突，LSF reservation 的作用范围仍由站点决定，Gaussian `%mem` 也不包含全部进程开销。

需要改输入时显式指定 `changes`（CLI `--changes` JSON）：`cpus`、`memory`（例如 `2GB`）、`paths`。路径映射可用原始值，也可用 `oldchk:test1.chk`／`chk:test1.chk` 区分读入与输出。改写仅发生在执行快照，原始卡另存于状态目录的 `run_id/original-input/`；返回原始／有效 SHA-256 和 diff，原卡不变。跨目录旧 checkpoint 必须显式映射到执行目录内，源文件必须在项目 A 中；远程既有 checkpoint 不自动当成本地文件上传。前一 Link1 段产生的 checkpoint 不要求预先存在。

同步所需日志后调用 `gaussian_result(run_id)`，读取最新同步清单中的日志、验证哈希并流式检查正常／异常终止。正常终止数量需匹配 Link1 段数，日志末尾还须有正常终止证据；数量不符或证据不足返回 unknown。应用结果单独保存为 `application_result`，不改写调度器 state；即使调度器 DONE，也可能应用 failed。额外输入、复杂指令和未解析行为必须核对；同一路径不能同时作为上传输入与计算输出。

### 大文件同步与存储管理

先预览，再同步；大传输优先使用独立同步操作：

```bash
hpc-mcp sync-preview RUN_ID --include '*.log' --include '*.chk' \
  --max-file-bytes 10737418240 --max-total-bytes 21474836480
hpc-mcp sync-start RUN_ID --options '{"includes":["*.log","*.chk"],"stable_only":true,"resume":true}'
hpc-mcp sync-operation OPERATION_ID
```

对应 MCP 为 `job_sync_preview`、`job_sync_start`、`job_sync_operation`。后台 worker 仅执行这一次同步，不监控其他任务；本地机器仍需开机。操作和进度保存在原状态目录，MCP 重启后可以查询。失败或中断后先检查操作，再重试同步；不会重交计算任务。

`job_sync` 自动预检文件选择、单文件／总量与空间。默认单文件 10 GiB、总量 20 GiB、预留 256 MiB；预检按所选大小的两倍估算暂存和安装空间，可通过集群设置或本次参数调整。大小／mtime 在传输后复查，变化或缺文件则保留暂存并拒绝安装。`stable_only=true` 要求传输前后都能确认终态；普通运行中同步保留 partial 标记。预检和周期性占用检查不是文件系统硬配额，也不能提供远程文件的原子快照。

`resume` 默认开启：只有失败暂存的文件清单、时间信息和同步选项一致时才复用；rsync partial 文件可作为增量传输依据，返回 `sync_resumed` 与可用的 `sync_transfer_stats`。改变规则或源文件会新建暂存，旧缓存由清理工具处理。归位冲突时先检查已有结果，再明确选择 `overwrite=merge`，不自动覆盖输入卡。

各类清理独立控制，默认预览，确认范围后才应用：

```bash
hpc-mcp cache-cleanup RUN_ID --older-than-seconds 86400
hpc-mcp storage-cleanup RUN_ID --category snapshot --category sync_history
hpc-mcp input-cache-cleanup --older-than-seconds 86400 --max-cache-bytes 10737418240
hpc-mcp remote-cleanup RUN_ID
# 在核对上述预览后，对对应命令加 --apply。
```

`job_storage_cleanup` 只处理终态且所选输出同步完成的登记作业；可删除输入快照、旧输出快照和输出归档，保留历史及最新结果。删除快照后不能从这些文件重放任务。集群 `input_cache=true` 可启用共享内容缓存和独立 CoW 快照，默认关闭；空间收益取决于文件系统，不支持 reflink 时复制会增加占用，缓存清理不删除任务快照。

`job_remote_cleanup` 默认保留远程目录。应用清理需重新确认调度终态、验证已下载文件及原始提交回执，路径严格限定登记 run；**未选择回传的远程文件也会被永久删除**，必须先核对要保留的结果。单次清理工具不自带定时器；Phase 6 协调器可按明确启用的策略定期执行本地保留，自动远程删除始终关闭。

### 显式启用常驻监控

提交确认后，由 agent 只登记用户选定的 run，再启动协调器。默认 auto_sync=true，清理关闭；策略在登记时固定，不因 agent 退出而丢失：

```bash
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/CONFIG --state-dir /ABS/STATE \
  monitor-watch RUN_ID --sync-options '{"includes":["test1.log","test1.chk"],"overwrite":"error"}'
/ABS/REPO/.venv/bin/hpc-mcp --config /ABS/CONFIG --state-dir /ABS/STATE monitor-start
/ABS/REPO/.venv/bin/hpc-mcp --state-dir /ABS/STATE monitor-status
/ABS/REPO/.venv/bin/hpc-mcp --state-dir /ABS/STATE monitor-notifications --after-id 0
```

MCP 同名工具使用下划线。项目作业默认回原任务目录，暂存仍位于 A/.hpc-mcp-sync；协调器配置、日志和事件均在原 agent 状态目录。查询确认终态后回传，失败退避；连续同步失败到上限进入 needs_attention，agent 应报告错误并核对后用 monitor_watch(reset=true) 重新启用，不能自动改变覆盖规则。完成事件包含调度结局与结果路径；Gaussian 还应调用 gaussian_result 判断应用结果。agent 保存通知游标，重新连接后补读，通知不是桌面推送。

仅跟踪时使用 auto_sync=false／CLI --no-auto-sync；monitor_unwatch 停止安排该 run 后续动作。monitor_stop 请求协调器退出，monitor_status 确认 stopped；已开始的 detached worker 和清理可能继续完成，远程作业不会因此取消。重启会接管未结束 worker，断网或记账缺失不触发重复提交，也不据陈旧终态清理数据。

可选 cleanup_policy：`{"sync_cache_age_seconds":86400,"storage_categories":["sync_history","old_outputs"],"storage_age_seconds":604800}`。需要删除输入快照时显式加入 snapshot，删除后不能从这些文件重放。清理前重新确认终态并校验下载结果，保留项目输出和历史。全局输入缓存保留在 monitor_start.settings.input_cache_cleanup 独立启用。查询周期、预算、并发、同步容量和通知上限均由 [接口参考](docs/REFERENCE.md) 列出；示例见 [examples/monitor](examples/monitor/)。

本机关闭／休眠期间无法查询或下载，远程作业仍按调度器管理；醒来后按最新状态继续，不补跑积压轮询。detached 进程可跨 MCP 客户端退出，但不自动开机启动，注销时能否保留也取决于本机会话管理。需要服务托管时，可审查 [systemd 用户单元模板](examples/monitor/hpc-mcp-monitor.service)，替换全部 /ABS/... 后安装到 ~/.config/systemd/user/hpc-mcp-monitor.service；它使用前台 monitor-run 和已有登记策略，不要同时另起 monitor-start：

```bash
systemctl --user daemon-reload
systemctl --user enable --now hpc-mcp-monitor.service
systemctl --user status hpc-mcp-monitor.service
# 更新或暂时停止：
systemctl --user stop hpc-mcp-monitor.service
# 卸载托管：
systemctl --user disable --now hpc-mcp-monitor.service
rm -- ~/.config/systemd/user/hpc-mcp-monitor.service
systemctl --user daemon-reload
```

模板采用 on-failure 重启和 control-group 停止方式；服务退出可能中断同组同步 worker，恢复后识别 interrupted 并按暂存续传，不保证 worker 跨服务停止存活。若用户希望注销后／开机无登录时仍运行，应单独确认该运行需求及系统策略，再配置用户 lingering；不擅自改变其他用户服务设置。[systemd 服务配置](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)、[进程停止范围](https://github.com/systemd/systemd/blob/main/man/systemd.kill.xml)、[用户 lingering](https://www.freedesktop.org/software/systemd/man/252/loginctl.html)。Windows／macOS 服务托管及系统单元实机验收尚未实现；当前进程锁和身份检查面向 Linux。

## 7. 更新与旧版本迁移

MCP 提供 `update_check(force=false, max_age_seconds=86400, timeout=10)` 和 `update_plan`（相同参数）。前者通过公开 GitHub HTTPS API 比较本地提交与上游 `main`，后者返回带 `argv`、`cwd` 的有序命令，不直接执行更新。当前版本号可能不变，因此以提交而非版本号判断更新。检查结果保存在指定状态目录的 `update-check.json`，默认一天复用；服务启动不主动联网。MCP 会向客户端提供会话检查指引，实际提示依赖 agent 调用工具，不是桌面推送通知。

```bash
/ABS/hpc-mcp/.venv/bin/hpc-mcp --state-dir /ABS/AGENT/hpc-mcp/state update-check
/ABS/hpc-mcp/.venv/bin/hpc-mcp --state-dir /ABS/AGENT/hpc-mcp/state update-plan --force
```

agent 应检查 `ok`、`blockers`、`commands`；存在启动中／运行中／停止中的协调器或 queued／running 同步 worker 时更新计划会阻止安装，先停止协调器或托管服务并确认 stopped，再通过 `job_get` 中的 `last_sync_operation` 查询并等待结束或核查中断。执行前停止 MCP 连接、再次核对工作区，并按 `cwd` 依次运行 `argv`，任何一步失败立即停止。计划固定检查到的提交，使用 `git fetch`、`git merge --ff-only` 与现有虚拟环境中的 pip，依赖仍遵循仓库锁定文件。有未保存改动、非 `main` 分支、非官方 origin 或分叉时不生成自动更新命令；普通非 editable 安装需按第 2 节重新安装。失败后修复问题再重启服务，不把安装失败当作更新成功。

旧版没有这些工具，需要先按以下命令手动更新一次。备份数据后更新；重启仍使用原来的配置、状态路径，并确认 `update_check`／`update_plan` 可用。

停止客户端中的本服务，备份 `clusters.toml` 和**整个状态目录**（包含数据库、输入快照、输出及归档），再在仓库根目录更新：

```bash
git status --short
# 如有本地代码修改，先保存并处理；工作区干净后继续。
git pull --ff-only
.venv/bin/python -m pip install --upgrade -c requirements-mcp.lock -e '.[mcp]'
.venv/bin/hpc-mcp --help
```

更新不会主动覆盖集群配置或清空作业历史。若快进更新失败，报告分支分歧并处理，不自动重置工作区。重启客户端，重新检查 MCP 工具、`cluster_check` 和 `job_list`；更新不会自动提交测试作业。

从 `xn02-mcps` 迁移时，在旧虚拟环境中先执行 `.venv/bin/python -m pip uninstall xn02-mcps`，再按上面的命令安装。旧命令 `xn02`、模块 `xn02_mcps` 和环境变量 `XN02_CONFIG`／`XN02_STATE` 已改为新名称，需要同步修改自己的启动脚本和客户端配置。

Codex 的旧条目可用 `codex mcp remove xn02-clusters` 移除，再按第 4 节注册 `hpc-mcp`；OpenCode 删除旧 `mcp["xn02-clusters"]` 条目并添加新条目，保留其他服务。注册名前先查看现有配置，避免留下两个同时运行的副本。

**已有 `.xn02/` 状态目录继续通过 `--state-dir /ABS/REPO/.xn02` 使用。** 不要仅为了改名移动状态目录：数据库记录包含输入快照和输出的绝对路径。旧远程回执仍能恢复和过滤，新作业使用 `.hpc-mcp-*` 回执。集群名和 SSH 别名（例如 `xn02`）属于用户的连接配置，无需改名。若仓库本身也搬过目录，应保留旧快照路径可访问，或先完成路径迁移再验收旧作业。

## 8. 卸载与数据清理

先调用 monitor_stop 并确认 stopped；若使用 systemd 托管，先按上节禁用并移除用户单元。通过 job_sync_operation 确认已开始的 worker 完成或中断，再停止客户端中的本服务、解除注册并卸载 Python 包：

```bash
codex mcp remove hpc-mcp
codex mcp list
/ABS/REPO/.venv/bin/python -m pip uninstall hpc-mcp
```

只执行实际使用的客户端步骤。OpenCode 用户删除对应配置作用域中的 `mcp["hpc-mcp"]` 条目；手动配置 Codex 的用户删除对应 `[mcp_servers.hpc-mcp]` 表。重启客户端，确认服务不再出现；迁移遗留的旧条目也应移除。

包卸载保留 `clusters.toml`、本地状态目录、显式指定的同步目标，以及集群上的作业和文件。卸载不会取消远程作业：需要取消时，应在解除注册前调用 `job_cancel` 或使用调度器命令，并确认结果。

完全移除时，先核对配置里的本地状态路径和各集群 `work_root`，备份需要保留的结果，再删除已确认归本项目使用的本地配置、状态、同步目标、专用虚拟环境和 checkout。远程只清理确认属于本项目且作业已结束的 `r_*` 目录，不删除共享 `work_root` 或 SSH 密钥／配置。不要把卸载当成授权清空计算结果；agent 应按用户指定的保留或清理范围执行。

## 开发与当前边界

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
# 安装 MCP extra 后，使用其虚拟环境执行完整测试，包括真实 stdio 子进程：
.venv/bin/python -m unittest discover -s tests -v
```

源码在 `src/hpc_mcp/`，测试在 `tests/`。标准库 CLI 可通过 `PYTHONPATH=src python3 -m hpc_mcp` 使用，适合依赖安装前的诊断。

当前测试包含 Gaussian、增量续传、同步进程恢复、清理保护及 Phase 7 的探测／确认／验证；完整协议测试使用官方 SDK。用户已在真实 LSF 上完成此前的提交、状态查询和结果同步。本仓库的离线测试覆盖两种调度器；官方 SDK 2.3.0 的真实 stdio 测试覆盖现代协议发现、旧版初始化握手、48 个工具及其参数发现、首次无配置启动、结构化结果、配置更新、脚本生成、模板／应用配置保存、确认、计划和重启后历史读取，全程不访问 SSH 或提交计算任务。未安装 SDK 时该测试明确跳过，不能当作协议验收通过。Slurm 实际作业、LSF 归档回退、新集群引导闭环及所用 agent 客户端仍需目标环境验收。[官方 SDK 客户端文档](https://py.sdk.modelcontextprotocol.io/client/)

当前支持新集群只读探测、脚本证据提取、应用配置确认及短作业验证、MPI／GPU／容器生成、Gaussian 单任务辅助和带预检／续传的同步；常驻监控和自动回传为显式启用。复杂 Python 入口完整兼容、批量并发控制、数组、作业依赖和用量统计尚未实现；packing 已取消。LSF 状态查询支持 `bacct`／`bhist` 归档回退及终止原因，归档已清理或不可访问时仍无法补齐最终状态。详细规则见 [接口参考](docs/REFERENCE.md)，路线见 [PLAN.md](PLAN.md)。

## 许可证

本项目采用 GNU General Public License v3.0（`GPL-3.0-only`）。完整许可条款见 [LICENSE](LICENSE)。
