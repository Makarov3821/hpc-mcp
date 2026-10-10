# hpc-mcp：Agent 安装与接入指南

项目仓库：[Makarov3821/hpc-mcp](https://github.com/Makarov3821/hpc-mcp)。

这是供 Codex、OpenCode 等 agent 使用的本地 stdio MCP 服务。通过用户已有的 SSH 配置管理 LSF／Slurm 集群：环境检查、队列查询、普通批处理作业提交、历史、状态、日志、取消和结果同步。

**Agent 按本指南完成安装、注册、集群引导和验收。** MCP 服务不调用模型 API，不需要 OpenAI API key。完整参数见 [接口参考](docs/REFERENCE.md)，目标集群验收见 [测试清单](docs/PLUGIN_TESTING.md)，功能路线见 [PLAN.md](PLAN.md)。

## 1. 确定安装位置与前置条件

**首次安装默认放在当前 agent 的数据目录下，只有用户明确指定其他位置时才更改。** 源码、虚拟环境、配置和状态统一收纳在专用 `hpc-mcp/` 子目录中，项目输入与最终输出保留在原项目位置。

| 客户端 | 默认安装根目录 |
| --- | --- |
| Codex | 当前 `CODEX_HOME` 下的 `hpc-mcp/`；未设置时为 `~/.codex/hpc-mcp/` |
| OpenCode | 实际数据目录下的 `hpc-mcp/`；通常为 `~/.local/share/opencode/hpc-mcp/` |
| 其他 agent | 该客户端实际数据目录下的 `hpc-mcp/`；无法确定时询问用户 |

OpenCode 数据目录与注册配置所在目录是两个位置，按客户端实际设置核对。[OpenCode 数据存储说明](https://opencode.ai/docs/troubleshooting/#storage)。多个客户端默认分别安装；共享安装或状态目录须由用户明确指定。

安装前先检查已有 MCP 注册条目及其路径。**已有安装原地更新，不因新默认值搬迁源码或历史。** 默认目录不可写时报告具体路径，等待用户指定替代位置；不要自行退回当前项目、临时目录或其他安装位置。

默认目录布局：

```text
<agent 数据目录>/hpc-mcp/
├── repo/                 # Git checkout；专用虚拟环境为 repo/.venv/
├── clusters.toml         # 集群连接配置
└── state/                # 历史、输入快照、监控记录
    ├── applications/    # 注册 handler、manifest、来源与版本证据
    └── profiles/*.json   # 旧配置，仅供迁移时读取

<项目 A>/
├── B/test1.gjf           # 原始输入；输出按选择回到 B/
└── .hpc-mcp-sync/        # 同步暂存，成功归位后删除
```

输入快照可能包含大型 checkpoint，应按需要设置输入容量与保留策略。没有声明 project_root 的旧式任务，其输出也可能保存在 state 中；项目模式不额外在 state 留一份已归位输出。

本地需要 Python 3.11+、Git、OpenSSH 和 rsync；当前完整协议验收环境为 Linux。远程需要 Bash、rsync、sha256sum 以及 LSF／Slurm 命令。macOS 安装及运行兼容性需单独验收，Windows 原生运行尚未支持。

```bash
python3 --version
command -v git
command -v ssh
command -v rsync
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes YOUR_SSH_ALIAS 'hostname; id -un'
```

SSH User、IdentityFile、Port、ProxyJump 留在用户的 OpenSSH 配置中。主机密钥未登记时按用户环境核验。远程 work_root 应已存在且可写，并能被计算节点访问；应用路径从用户提供的脚本或明确设置获取。

## 2. 安装本地服务

先按所用客户端确定安装根目录，下面两种选择只执行一种。用户明确指定其他位置时，将 HPC_MCP_HOME 替换成该绝对路径。

Codex：

```bash
# Codex：沿用当前 CODEX_HOME，未设置时使用 ~/.codex。
HPC_MCP_HOME="${CODEX_HOME:-$HOME/.codex}/hpc-mcp"
```

OpenCode：

```bash
# OpenCode：先核对实际数据目录；默认位置示例。
HPC_MCP_HOME="$HOME/.local/share/opencode/hpc-mcp"
```

检查该目录尚未安装后，再执行首次安装。已有 repo 时转到第 7 节更新，不覆盖配置、状态或已有 checkout。

```bash
mkdir -p -- "$HPC_MCP_HOME"
HPC_MCP_HOME="$(cd -- "$HPC_MCP_HOME" && pwd -P)"
git clone https://github.com/Makarov3821/hpc-mcp.git "$HPC_MCP_HOME/repo"
cd -- "$HPC_MCP_HOME/repo"
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-mcp.lock -e '.[mcp]'
export HPC_MCP_CONFIG="$HPC_MCP_HOME/clusters.toml"
export HPC_MCP_STATE="$HPC_MCP_HOME/state"
.venv/bin/hpc-mcp --config "$HPC_MCP_CONFIG" --state-dir "$HPC_MCP_STATE" --help
```

HTTPS clone 不需要 GitHub SSH 身份。已经配置 GitHub SSH 的用户也可将来源改为 `git@github.com:Makarov3821/hpc-mcp.git`，目标仍是同一个 repo 目录。GitHub 与计算集群的 SSH 身份分别管理。

安装使用官方 Python MCP SDK v2（`mcp>=2,<3`）。`requirements-mcp.lock` 固定已验收依赖（Linux／Python 3.14／SDK 2.3.0），通过 `-c` 约束安装。下载或兼容性失败时报告安装未完成，不静默忽略约束；标准库 CLI 能启动不代表 MCP 协议可用。

后文 `/ABS/HPC_MCP` 代表上述安装根目录的**实际绝对路径**；`/ABS/A` 为项目根目录，YOUR_SSH_ALIAS 为用户的集群别名。TOML／JSON 注册片段中的路径须替换成实际值，不依赖 shell 变量、`~` 展开或当前工作目录。

注册和后续 CLI 操作始终指定同一套绝对 `--config`、`--state-dir`。程序未传参数且未设置环境变量时仍回退到当前目录的 clusters.toml／.hpc-mcp；这是底层 CLI 行为，**安装 agent 不应使用这个回退**。上述 export 只影响当前 shell，客户端注册应显式写入路径。

发行包、CLI 命令和注册名为 `hpc-mcp`，Python 模块名为 `hpc_mcp`。

## 3. 配置集群

新安装的 MCP 保留集群与任务基础工具，**没有内置 Gaussian／VASP 执行方法**。先完成连接设置，再由用户提供脚本注册应用。`serve` 可在配置文件尚不存在时启动空服务。

1. `cluster_probe(ssh_host)` 保存 SSH 身份、调度器、队列与工作目录证据；软件发现最多一次有界 module avail，不扫盘。候选或路径不明确时请求用户补充。
2. 用 `cluster_configure`／`config-set` 保存连接；常用队列、核数、初始化与程序路径以用户脚本为准，不推断默认执行方式。
3. 用户提供提交脚本或 qg16／qvasp 源码，进入下面的标准应用注册流程。登录检查不表示计算节点软件已验证。

### 用户脚本 → 应用插件 → 固定执行

agent 首次改造用户源码：普通 LSF／Slurm 脚本作为生成目标，写一个保持原格式的 Python 生成器；批处理器保留原生成函数与条件分支，只改参数／目录接口并拆除直接提交。handler 负责生成；MCP 接管独立任务快照、上传、提交、状态与输出归位。日常不重新解释源码，不在 MCP 内部调用 LLM。

标准包包含 handler.py、manifest.json 和 original/ 原脚本。详细契约、请求／响应、错误与清理边界见 [应用插件指南](docs/APPLICATIONS.md)。插件位于 `state-dir/applications/应用/versions/版本/`，不写源码安装目录或用户项目。

适配时先读取 `settings_get.onboarding.applications_root`，在其中创建唯一的 `.install-draft-*` 暂存目录；改造包、审阅 JSON 和需要的辅助文件全部放在这里，不写入当前工作目录、用户项目或 repo。默认不生成 `GAUSSIAN.md` 等应用说明：MCP 不读取这些文档作为执行配置，实际行为由 handler 与 manifest 决定。来源和审阅报告由 MCP 保存；安装成功且报告持久化后，只删除本次创建的确切暂存目录。中断遗留暂存可通过 application_cleanup 预览后清理。用户明确要求额外说明时，也保存在受管应用包中，不放到工作目录。

1. `application_install(bundle_dir)` 静态保存 draft，返回版本与 review_token，不执行代码。
2. 调用 `application_review_request(application, version, author_session)`，agent 启动一个全新上下文的独立 reviewer，只提供原脚本、handler、manifest 和接口约束，不传改造对话或作者结论。reviewer 分析所有条件分支、默认值及生成结果差异，返回结构化报告。
3. 用 `application_review_submit(..., report)` 原样提交报告；revise 时根据意见修改、安装新版本并重新独立审阅。不存在未批准差异或未决项才能 pass；没有独立 reviewer 能力时保持草稿，不模拟报告。
4. 独立审阅通过后，用户核对后 `application_activate(..., confirmation_note)` 激活。重启 MCP 可发现 gaussian_prepare／vasp_prepare 等工具及声明参数；稳定 application_prepare 可以立即使用。
5. `application_prepare(application, cluster, input_path, project_root, parameters)` 准备独立 run，再按用户意图调用 job_submit。输入卡由 agent 预先规范，不暗中改写。
6. 授权一个实际小任务，完成后 `application_validate(run_id)` 核对注册判据与日志哈希。确认激活不等于真实验证通过；未配置判据返回 unverified。

未注册软件返回 application_not_registered，并请求用户提供模板。不会猜测软件路径、MPI 环境或 Slurm 转换；未迁移的 packing／批量扫描明确排除，多任务用多个独立 run 与 workflow 管理。

[Gaussian](examples/applications/gaussian/) 与 [VASP](examples/applications/vasp/) 是仓库原脚本的迁移样例，**不会自动安装**。先复制到 agent 数据目录，按用户脚本修改 manifest 中 clusters、队列和路径，再独立审阅、用户确认。还有普通 [LSF](examples/applications/hello_lsf/)／[Slurm](examples/applications/hello_slurm/) 模板样例。

```bash
# BUNDLE 为已由 agent 改造并整理到数据目录中的应用插件包。
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state application-install /ABS/BUNDLE
# 阅读代码后，使用上一步返回的精确版本与 token；note 记录实际审阅。
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state application-review-request gaussian 1 --author-session AUTHOR_SESSION
# 将返回材料交给独立 reviewer，报告也保存在本次数据目录暂存中
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state application-review-submit gaussian 1 REVIEW_TOKEN \
  --report-file /ABS/HPC_MCP/state/applications/.install-draft-UNIQUE/review.json
# 用户确认后才执行：
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state application-activate gaussian 1 REVIEW_TOKEN --note '用户确认了该版本与适用集群'
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state application-prepare gaussian lab /ABS/A/B/test1.gjf --project-root /ABS/A
```

更新走 application_update → 独立 review → 用户确认 activate，已有任务绑定旧版。删除先 application_remove 预览，再用 dry_run=false（CLI --apply）执行；未提交／活动任务先处理，输入／输出／任务历史与共享 Python 依赖保留。安装失败暂存和无引用旧版本可用 application_cleanup 清理，默认只预览。

CLI 探测示例（`--queue-details` 可选择附加原始队列限制信息）：

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state cluster-probe YOUR_SSH_ALIAS
```

探测、脚本报告和任务历史保存在同一 agent 状态目录；应用插件与注册证据按版本保存。旧 profile_get/list 仅用于迁移时读取。应用插件样例中的路径、队列和资源必须按用户脚本调整。验证只覆盖记录的具体版本、参数和布局；改变参数不继承验证，集群设置变化会拒绝准备，须显式审阅并重新激活／验证。远程软件内容变化无法通过本地读取自动发现，站点变更后应显式重新验证。

已有 `clusters.toml` 时先读取。新增或更新配置可以使用 CLI 的 JSON 设置接口：

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --state-dir /ABS/HPC_MCP/state --config /ABS/HPC_MCP/clusters.toml config-set lab \
  '{"ssh_host":"YOUR_SSH_ALIAS","scheduler":"lsf","work_root":"/shared/home/user/jobs","output_mode":"all"}'
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --state-dir /ABS/HPC_MCP/state --config /ABS/HPC_MCP/clusters.toml config-get lab
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --state-dir /ABS/HPC_MCP/state --config /ABS/HPC_MCP/clusters.toml check lab
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --state-dir /ABS/HPC_MCP/state --config /ABS/HPC_MCP/clusters.toml info lab
```

Slurm 使用 `"scheduler":"slurm"`。命令只在登录环境初始化后可用时，设置 `init_scripts` 为远程初始化文件的绝对路径。该文件会被执行，不能放入作业提交等副作用。

配置接口只更新指定设置、保留其他集群，写入前完整校验。也可使用 [TOML 示例](clusters.example.toml)。配置和状态目录无需放入版本控制。

`check` 的 `ok:true` 表示必要命令、目录权限和队列查询通过；共享存储、队列提交授权仍需实际作业验证。SSH 失败、解析失败和 warning 均应分别报告。

## 4. 注册到 Agent 客户端

只修改所选客户端的对应条目，保留用户其他设置。客户端目录不可写时提供准备好的配置片段，明确说明注册尚未完成。

### Codex

可以使用官方 CLI 注册命令：

```bash
codex mcp add hpc-mcp -- /ABS/HPC_MCP/repo/.venv/bin/hpc-mcp \
  --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state serve
codex mcp list
```

如需指定超时，可在用户或项目 Codex 配置的相应条目中设置：

```toml
[mcp_servers.hpc-mcp]
command = "/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp"
args = ["--config", "/ABS/HPC_MCP/clusters.toml", "--state-dir", "/ABS/HPC_MCP/state", "serve"]
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
      "command": ["/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp", "--config", "/ABS/HPC_MCP/clusters.toml", "--state-dir", "/ABS/HPC_MCP/state", "serve"],
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

应用插件任务按 handler 返回的明确 outputs 同步；低层 job_prepare 未指定规则时仍可采用全文件模式。内部回执、符号链接不下载。已有作业保留旧的过滤设置。Agent 可以在每次同步时覆盖选择：

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

未指定 project_root 时，`direct` 将最新结果放在 state 中的 `run_id/outputs/`；`replace` 会先把旧输出归档到 `sync-history/`。项目任务默认回原任务目录，不允许 replace，见下一节。`snapshot` 为每次同步创建独立目录。也可传 `destination` 指定其他本地目标；具体冲突及过滤规则见 [接口参考](docs/REFERENCE.md)。

安装验收应报告：客户端是否真正连接、发现了哪些工具、集群检查结果，以及作业提交／结果同步是否实际验证。仅运行 `serve` 没有输出是等待 stdio 请求，不能据此判定握手成功。

### 在原项目目录中提交独立任务与回传

一个输入卡或任务目录对应一次 `job_prepare` 和一个独立远程 `r_*` 目录。agent 自行识别用户选定的任务、生成对应运行脚本，再逐个调用；服务不扫描或自动提交其他输入卡；需要生成运行脚本时使用第 6 节的生成器或模板。

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
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  cache-cleanup RUN_ID --older-than-seconds 86400
# 实际清理：在同一命令末尾添加 --apply
```

清理与提交／同步共用作业锁，仅处理该作业的同步暂存；不会删除输入快照、历史、已回传文件或远程数据。需要定时时，由 agent 按用户要求配置系统定时器重复执行指定作业的清理命令；默认不启动后台协调器；需要服务内自动保留策略时显式使用下节 monitor 工具。相同选择规则、目标和远程文件指纹下，失败下载可复用暂存续传；规则或文件变化时创建新尝试。

## 6. 低层脚本与任务管理

下面的通用生成工具只用于用户明确指定的脚本／资源，不能作为未注册软件的自动执行回退。日常应用任务优先使用已注册 handler。

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
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  template-import gaussian /ABS/HPC_MCP/repo/examples/templates/gaussian-lsf.json
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  template-plan gaussian lab /ABS/A/B --project-root /ABS/A \
  --parameters '{"input":"test1.gjf","stem":"test1","checkpoint":"test1.chk","queue":"USER_QUEUE","cpus":4}'
# 核对计划后执行，PLAN_ID 是上一步返回的 plan_id：
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state template-run PLAN_ID
```

MCP 对应 `template_import(name, definition)`、`template_plan(...)`、`template_run(plan_id)`；`template_list`／`template_get` 查询保存的模板和历史版本。CLI 示例文件路径也须替换成实际绝对路径。模板保存在同一个状态数据库中，每次导入创建新版本。参数通过 `{{name}}` 绑定，支持 string／integer／boolean；没有默认值的参数必须提供，不执行表达式。

`template_plan` 会创建本地输入快照，但不上传或提交；`plan_id` 就是 `run_id`。计划固定模板版本、参数、最终脚本和输入清单，之后编辑模板或原输入不会改变它。`template_run` 只执行该计划，重复调用遵循已有提交防重与回执恢复规则；重新准备或重跑计算应创建新计划。状态、日志、回传和缓存清理继续使用同一个 `run_id`。

Gaussian 示例不解析或修改输入卡。agent 应核对实际 `%chk`、`%nprocshared`、内存、运行时间以及计算节点的 g16 环境；续算 chk 等依赖必须加入模板输入列表。Slurm 内存区分 `job` 与 `per_cpu`；LSF 要求明确使用 `lsf_reservation`，其预留作用范围由站点配置决定，不表示硬内存限制。Bash 和 Python 脚本可先静态分析；动态 Python 入口的完整转换与任意 Shell 自动导入仍未实现。

### 分析现有脚本与准备 MPI／GPU 作业

先调用 `script_inspect(script_path)`；远程脚本传 cluster 和远程绝对路径。也可运行：

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  script-inspect /ABS/HPC_MCP/repo/examples/inspection/slurm.sh
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  script-inspect /remote/job.sh --cluster lab
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  script-generate slurm /ABS/HPC_MCP/repo/examples/resources/mpi-slurm.json
```

分析不会执行源码，返回 SHA、行号和未解析项，并保存可追溯的 report_id。Bash 的 template_draft 只是待审草案；agent 核对后用于编写应用 handler，或在用户明确要求时导入独立低层模板。变量、分支、多个命令和 Python 入口不自动转换；Python AST 已支持候选常量、参数声明、嵌入配置及副作用提取，存档的 qg16／qvasp 提交器已有只读分析测试，应用 handler 迁移样例与原生成对照见 examples/applications 和插件指南。原始批处理脚本可继续用 job_prepare 保存原文，Python 提交器不能直接作为批处理脚本提交。

MPI 使用 tasks（进程数）、cpus（每进程 CPU）、nodes／tasks_per_node 和 launcher；OpenMP 线程在 environment 明确设置。Slurm 可指定精确均匀节点布局；LSF 按总 slots 和每 host 的 ptile 表达，不声称精确多节点数。GPU 使用 scheduler 专用 slurm_gres／slurm_gpus_per_task／lsf_gpu，容器支持可信远程 Apptainer／Singularity 镜像、显式挂载和单节点 scratch。镜像及软件不随任务上传；scratch 不改变输出 cwd，不自动回传中间文件。

[资源示例](examples/resources/) 中的远程路径、GPU 型号、启动器参数都是占位，agent 应先取得用户的站点设置，核对生成脚本与输入清单再准备／提交；MPI 主机发现、CPU binding、容器 ABI 和共享存储需要目标环境验证。参数和兼容限制见 [接口参考](docs/REFERENCE.md)。

### Gaussian 输入与应用结果分析

`gaussian_inspect(input_file)` 仍是只读分析工具，保留行号、Link0／Link1、资源与 checkpoint 证据，不提供内置执行方案。注册 Gaussian handler 后用 application_prepare 或重启后的 gaussian_prepare 准备，再用 job_submit 提交。CLI gaussian-prepare 只调用注册 handler，不接受旧 spec／changes 回退。

输入卡由 agent 在任务目录中明确规范，核数／内存／checkpoint 的修改不隐式发生在插件里。回传日志后，可用 `gaussian_result(run_id, log_path="test1.log")` 做已有 Gaussian 终止分析；也可用 application_validate 核对 manifest 的显式标记。应用终止与调度 DONE 分开记录，均不保证所有科学结果正确。

### 大文件同步与存储管理

先预览，再同步；大传输优先使用独立同步操作：

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  sync-preview RUN_ID --include '*.log' --include '*.chk' \
  --max-file-bytes 10737418240 --max-total-bytes 21474836480
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  sync-start RUN_ID --options '{"includes":["*.log","*.chk"],"stable_only":true,"resume":true}'
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state sync-operation OPERATION_ID
```

对应 MCP 为 `job_sync_preview`、`job_sync_start`、`job_sync_operation`。后台 worker 仅执行这一次同步，不监控其他任务；本地机器仍需开机。操作和进度保存在原状态目录，MCP 重启后可以查询。失败或中断后先检查操作，再重试同步；不会重交计算任务。

`job_sync` 自动预检文件选择、单文件／总量与空间。默认单文件 10 GiB、总量 20 GiB、预留 256 MiB；预检按所选大小的两倍估算暂存和安装空间，可通过集群设置或本次参数调整。大小／mtime 在传输后复查，变化或缺文件则保留暂存并拒绝安装。`stable_only=true` 要求传输前后都能确认终态；普通运行中同步保留 partial 标记。预检和周期性占用检查不是文件系统硬配额，也不能提供远程文件的原子快照。

`resume` 默认开启：只有失败暂存的文件清单、时间信息和同步选项一致时才复用；rsync partial 文件可作为增量传输依据，返回 `sync_resumed` 与可用的 `sync_transfer_stats`。改变规则或源文件会新建暂存，旧缓存由清理工具处理。归位冲突时先检查已有结果，再明确选择 `overwrite=merge`，不自动覆盖输入卡。

各类清理独立控制，默认预览，确认范围后才应用：

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  cache-cleanup RUN_ID --older-than-seconds 86400
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  storage-cleanup RUN_ID --category snapshot --category sync_history
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  input-cache-cleanup --older-than-seconds 86400 --max-cache-bytes 10737418240
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state remote-cleanup RUN_ID
# 在核对上述预览后，对对应命令加 --apply。
```

`job_storage_cleanup` 只处理终态且所选输出同步完成的登记作业；可删除输入快照、旧输出快照和输出归档，保留历史及最新结果。删除快照后不能从这些文件重放任务。集群 `input_cache=true` 可启用共享内容缓存和独立 CoW 快照，默认关闭；空间收益取决于文件系统，不支持 reflink 时复制会增加占用，缓存清理不删除任务快照。

`job_remote_cleanup` 默认保留远程目录。应用清理需重新确认调度终态、验证已下载文件及原始提交回执，路径严格限定登记 run；**未选择回传的远程文件也会被永久删除**，必须先核对要保留的结果。单次清理工具不自带定时器；Phase 6 协调器可按明确启用的策略定期执行本地保留，自动远程删除始终关闭。

### 显式启用常驻监控

提交确认后，由 agent 只登记用户选定的 run，再启动协调器。默认 auto_sync=true，清理关闭；策略在登记时固定，不因 agent 退出而丢失：

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state \
  monitor-watch RUN_ID --sync-options '{"includes":["test1.log","test1.chk"],"overwrite":"error"}'
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state monitor-start
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state monitor-status
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  monitor-notifications --after-id 0
```

MCP 同名工具使用下划线。项目作业默认回原任务目录，暂存仍位于 A/.hpc-mcp-sync；协调器配置、日志和事件均在原 agent 状态目录。查询确认终态后回传，失败退避；连续同步失败到上限进入 needs_attention，agent 应报告错误并核对后用 monitor_watch(reset=true) 重新启用，不能自动改变覆盖规则。完成事件包含调度结局与结果路径；Gaussian 还应调用 gaussian_result 判断应用结果。agent 保存通知游标，重新连接后补读，通知不是桌面推送。

仅跟踪时使用 auto_sync=false／CLI --no-auto-sync；monitor_unwatch 停止安排该 run 后续动作。monitor_stop 请求协调器退出，monitor_status 确认 stopped；已开始的 detached worker 和清理可能继续完成，远程作业不会因此取消。重启会接管未结束 worker，断网或记账缺失不触发重复提交，也不据陈旧终态清理数据。

可选 cleanup_policy：`{"sync_cache_age_seconds":86400,"storage_categories":["sync_history","old_outputs"],"storage_age_seconds":604800}`。需要删除输入快照时显式加入 snapshot，删除后不能从这些文件重放。清理前重新确认终态并校验下载结果，保留项目输出和历史。全局输入缓存保留在 monitor_start.settings.input_cache_cleanup 独立启用。查询周期、预算、并发、同步容量和通知上限均由 [接口参考](docs/REFERENCE.md) 列出；示例见 [examples/monitor](examples/monitor/)。

本机关闭／休眠期间无法查询或下载，远程作业仍按调度器管理；醒来后按最新状态继续，不补跑积压轮询。detached 进程可跨 MCP 客户端退出，但不自动开机启动，注销时能否保留也取决于本机会话管理。需要服务托管时，可审查 [systemd 用户单元模板](examples/monitor/hpc-mcp-monitor.service)，将其中 /ABS/REPO 设为安装根目录下的 repo，/ABS/CONFIG 设为 clusters.toml，/ABS/STATE 设为 state；使用实际绝对路径替换后安装到 ~/.config/systemd/user/hpc-mcp-monitor.service；它使用前台 monitor-run 和已有登记策略，不要同时另起 monitor-start：

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

### 批量任务、依赖与用量

每个输入卡／任务目录先独立 prepare，核对资源、环境和输出规则。用 `workflow_plan` 将选定的 prepared run 登记为一个持久工作流，默认禁用；向用户展示计划中的任务、依赖、文件映射和限制。取得这份计划的明确授权后，调用 `workflow_start(workflow_id, review_token, confirmation_note)`。它只启用计划；`workflow_tick` 或已启动的监控协调器才逐项提交。

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  workflow-plan project RUN_B RUN_D --limits '{"max_in_flight":1,"submissions_per_minute":2}'
# 核对返回的计划并记录用户授权后：
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml \
  --state-dir /ABS/HPC_MCP/state \
  workflow-start WORKFLOW_ID REVIEW_TOKEN --confirmation-note '用户授权上述任务和限制'
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state workflow-tick WORKFLOW_ID
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state workflow-status WORKFLOW_ID
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state workflow-pause WORKFLOW_ID
```

所有命令继续使用相同的 `--config`／`--state-dir`。在途数量包括排队、运行、挂起及提交结局未知的任务，仅限制这个工作流；不是调度器实际运行数或整个账户配额。已登记的任务由工作流统一提交，普通 job_submit 不能绕过限制。暂停停止后续提交，不取消已交作业；上传失败可显式 workflow_retry，拒绝提交或计算失败需要准备新任务。未知提交先恢复回执，不盲目重交。

依赖分别表达调度成功、应用成功及文件就绪；应用成功目前仅支持 gaussian_prepare 的 Gaussian 元数据。显式文件映射可把父任务的 `test1.chk` 交给子任务的 `old.chk`：稳定同步、清单和哈希通过后生成新的执行快照，不覆盖原输入或准备快照。子任务尚不存在的依赖文件不用先伪造；以普通／模板 prepare 准备其余输入即可。`workflow_status.tasks[初始run_id].run_id` 指向实际执行编号，后续查询、同步和取消使用这个编号。

协调器可以在 agent 退出后推进已授权工作流，但不会替它们自动登记最终结果回传。取得实际提交编号后，再用 monitor_watch 登记所需输出与清理策略。依赖文件同步只服务文件交接。具体限制、错误恢复和示例见 [接口参考](docs/REFERENCE.md#工作流与用量统计)。

`job_usage(run_id)` 查询并保存这项任务的记账证据；`usage_report(project_root="/ABS/A")` 汇总已缓存的项目记录，不自动遍历远程历史。区分申请资源、分配 CPU 时间和实际 CPU 时间；缺失值保留 null，失败刷新保留旧证据并标记陈旧。统计范围与 Slurm 峰值 RSS 的口径随结果返回，不自动修改应用配置。准备真机验收时使用 [测试清单](docs/PLUGIN_TESTING.md)。

## 7. 更新与旧版本迁移

MCP 提供 `update_check(force=false, max_age_seconds=86400, timeout=10)` 和 `update_plan`（相同参数）。前者通过公开 GitHub HTTPS API 比较本地提交与上游 `main`，后者返回带 `argv`、`cwd` 的有序命令，不直接执行更新。当前版本号可能不变，因此以提交而非版本号判断更新。检查结果保存在指定状态目录的 `update-check.json`，默认一天复用；服务启动不主动联网。MCP 会向客户端提供会话检查指引，实际提示依赖 agent 调用工具，不是桌面推送通知。

```bash
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state update-check
/ABS/HPC_MCP/repo/.venv/bin/hpc-mcp --config /ABS/HPC_MCP/clusters.toml --state-dir /ABS/HPC_MCP/state update-plan --force
```

agent 应检查 `ok`、`blockers`、`commands`；存在启动中／运行中／停止中的协调器或 queued／running 同步 worker 时更新计划会阻止安装，先停止协调器或托管服务并确认 stopped，再通过 `job_get` 中的 `last_sync_operation` 查询并等待结束或核查中断。执行前停止 MCP 连接、再次核对工作区，并按 `cwd` 依次运行 `argv`，任何一步失败立即停止。计划固定检查到的提交，使用 `git fetch`、`git merge --ff-only` 与现有虚拟环境中的 pip，依赖仍遵循仓库锁定文件。有未保存改动、非 `main` 分支、非官方 origin 或分叉时不生成自动更新命令；普通非 editable 安装需按第 2 节重新安装。失败后修复问题再重启服务，不把安装失败当作更新成功。

旧版没有这些工具，需要先按以下命令手动更新一次。备份数据后更新；重启仍使用原来的配置、状态路径，并确认 `update_check`／`update_plan` 可用。

停止客户端中的本服务，备份 `clusters.toml` 和**整个状态目录**（包含数据库、输入快照、输出及归档），再进入原安装的 repo 更新：

```bash
cd -- /ABS/HPC_MCP/repo
git status --short
# 如有本地代码修改，先保存并处理；工作区干净后继续。
git pull --ff-only
.venv/bin/python -m pip install --upgrade -c requirements-mcp.lock -e '.[mcp]'
.venv/bin/hpc-mcp --help
```

更新不会主动覆盖集群配置或清空作业历史。若快进更新失败，报告分支分歧并处理，不自动重置工作区。重启客户端，重新检查 MCP 工具、`cluster_check` 和 `job_list`；更新不会自动提交测试作业。

从 `xn02-mcps` 迁移时，在旧虚拟环境中先执行 `.venv/bin/python -m pip uninstall xn02-mcps`，再按上面的命令安装。旧命令 `xn02`、模块 `xn02_mcps` 和环境变量 `XN02_CONFIG`／`XN02_STATE` 已改为新名称，需要同步修改自己的启动脚本和客户端配置。

Codex 的旧条目可用 `codex mcp remove xn02-clusters` 移除，再按第 4 节注册 `hpc-mcp`；OpenCode 删除旧 `mcp["xn02-clusters"]` 条目并添加新条目，保留其他服务。注册名前先查看现有配置，避免留下两个同时运行的副本。

旧安装仍使用原注册的源码与配置路径，第 2 节的默认布局只用于首次安装。

**已有 `.xn02/` 状态目录继续通过 `--state-dir /ABS/OLD_INSTALL/.xn02` 使用。** 不要仅为了改名移动状态目录：数据库记录包含输入快照和输出的绝对路径。旧远程回执仍能恢复和过滤，新作业使用 `.hpc-mcp-*` 回执。集群名和 SSH 别名（例如 `xn02`）属于用户的连接配置，无需改名。若仓库本身也搬过目录，应保留旧快照路径可访问，或先完成路径迁移再验收旧作业。

## 8. 卸载与数据清理

先调用 monitor_stop 并确认 stopped；若使用 systemd 托管，先按第 6 节的服务托管步骤禁用并移除用户单元。通过 job_sync_operation 确认已开始的 worker 完成或中断，再停止客户端中的本服务、解除注册并卸载 Python 包：

```bash
codex mcp remove hpc-mcp
codex mcp list
/ABS/HPC_MCP/repo/.venv/bin/python -m pip uninstall hpc-mcp
```

只执行实际使用的客户端步骤。OpenCode 用户删除对应配置作用域中的 `mcp["hpc-mcp"]` 条目；手动配置 Codex 的用户删除对应 `[mcp_servers.hpc-mcp]` 表。重启客户端，确认服务不再出现；迁移遗留的旧条目也应移除。

包卸载保留 `clusters.toml`、本地状态目录、显式指定的同步目标，以及集群上的作业和文件。卸载不会取消远程作业：需要取消时，应在解除注册前调用 `job_cancel` 或使用调度器命令，并确认结果。

默认安装的卸载范围是该 agent 数据目录中的专用 hpc-mcp/ 子目录，不是整个 agent 数据目录。用户指定的外部安装按实际注册路径处理。

完全移除时，先核对配置里的本地状态路径和各集群 `work_root`，备份需要保留的结果，再删除已确认归本项目使用的本地配置、状态、同步目标、专用虚拟环境和 checkout。远程只清理确认属于本项目且作业已结束的 `r_*` 目录，不删除共享 `work_root` 或 SSH 密钥／配置。不要把卸载当成授权清空计算结果；agent 应按用户指定的保留或清理范围执行。

## 开发与当前边界

在 repo 根目录执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
# 安装 MCP extra 后，使用其虚拟环境执行完整测试，包括真实 stdio 子进程：
.venv/bin/python -m unittest discover -s tests -v
```

源码在 `src/hpc_mcp/`，测试在 `tests/`。标准库 CLI 可通过 `PYTHONPATH=src python3 -m hpc_mcp` 使用，适合依赖安装前的诊断。

离线回归包括原 qg16 的 256 组、qvasp 的 576 组条件逐字对照，原 CLI 安全预览产物与 MCP 快照比较，LSF／Slurm 模板的离线提交／同步／卸载，以及官方 SDK 现代／旧版 stdio、重启后的动态工具与 schema。完整验收需安装 MCP extra；缺少 SDK 会明确跳过，不能视为协议通过。插件真实 Gaussian／VASP 计算及客户端重启刷新仍需目标环境验收，见 [验收清单](docs/PLUGIN_TESTING.md)。

当前支持新集群只读探测、脚本证据提取、应用配置确认及短作业验证、MPI／GPU／容器生成、Gaussian 单任务辅助和带预检／续传的同步；常驻监控和自动回传为显式启用。批量并发控制、任务依赖和用量统计已有实现；数组及复杂 Python 入口完整兼容仍待开发，packing 已取消。LSF 状态查询支持 `bacct`／`bhist` 归档回退及终止原因，归档已清理或不可访问时仍无法补齐最终状态。详细规则见 [接口参考](docs/REFERENCE.md)，路线见 [PLAN.md](PLAN.md)。

## 许可证

本项目采用 GNU General Public License v3.0（`GPL-3.0-only`）。完整许可条款见 [LICENSE](LICENSE)。
