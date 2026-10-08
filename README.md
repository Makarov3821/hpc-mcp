# hpc-mcp：Agent 安装与接入指南

项目仓库：[Makarov3821/hpc-mcp](https://github.com/Makarov3821/hpc-mcp)。

这是供 Codex、OpenCode 等 agent 使用的本地 stdio MCP 服务。通过用户已有的 SSH 配置管理 LSF／Slurm 集群：环境检查、队列查询、普通批处理作业提交、历史、状态、日志、取消和结果同步。

**Agent 应按下面顺序安装、配置并验收。** 不需要 OpenAI API key；MCP 服务本身不调用模型 API。完整参数和操作语义见 [接口参考](docs/REFERENCE.md)。

## 1. 检查前置条件

确认仓库绝对路径、可写的配置文件路径、固定的本地状态目录，以及用户指定的 SSH 别名、调度器和共享工作目录。已有配置优先复用；缺失的集群信息询问用户，不猜测。

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
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/hpc-mcp --help
```

未配置 GitHub SSH 身份时可使用 `git clone https://github.com/Makarov3821/hpc-mcp.git`。这与登录计算集群所需的 SSH 配置分别管理。

安装使用官方 Python MCP SDK v2（`mcp>=2,<3`）。若下载失败，报告依赖安装未完成，不将能运行标准库 CLI 当成 MCP 安装成功。

后续所有示例中的 `/ABS/REPO`、`/ABS/STATE`、`YOUR_SSH_ALIAS` 和路径均替换为实际值。MCP 启动命令必须使用绝对路径，避免依赖当前目录、激活的虚拟环境或 `PYTHONPATH`。

发行包、CLI 命令和 MCP 注册名统一为 `hpc-mcp`，Python 模块名为 `hpc_mcp`。CLI 默认读取当前目录的 `clusters.toml`，状态存放在 `.hpc-mcp/`；也可使用 `HPC_MCP_CONFIG`、`HPC_MCP_STATE` 环境变量或对应命令行参数指定路径。注册客户端时显式指定固定的绝对路径。

## 3. 配置集群

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

先从客户端调用 `settings_get`、`cluster_list`、`cluster_check`、`cluster_info`。`settings_get` 暴露完整设置模板、配置路径和状态路径；`cluster_configure` 可持久化更新集群，立即作用于当前服务。

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

## 6. 更新与旧版本迁移

停止客户端中的本服务，备份 `clusters.toml` 和**整个状态目录**（包含数据库、输入快照、输出及归档），再在仓库根目录更新：

```bash
git status --short
# 如有本地代码修改，先保存并处理；工作区干净后继续。
git pull --ff-only
.venv/bin/python -m pip install --upgrade -e '.[mcp]'
.venv/bin/hpc-mcp --help
```

更新不会主动覆盖集群配置或清空作业历史。若快进更新失败，报告分支分歧并处理，不自动重置工作区。重启客户端，重新检查 MCP 工具、`cluster_check` 和 `job_list`；更新不会自动提交测试作业。

从 `xn02-mcps` 迁移时，在旧虚拟环境中先执行 `.venv/bin/python -m pip uninstall xn02-mcps`，再按上面的命令安装。旧命令 `xn02`、模块 `xn02_mcps` 和环境变量 `XN02_CONFIG`／`XN02_STATE` 已改为新名称，需要同步修改自己的启动脚本和客户端配置。

Codex 的旧条目可用 `codex mcp remove xn02-clusters` 移除，再按第 4 节注册 `hpc-mcp`；OpenCode 删除旧 `mcp["xn02-clusters"]` 条目并添加新条目，保留其他服务。注册名前先查看现有配置，避免留下两个同时运行的副本。

**已有 `.xn02/` 状态目录继续通过 `--state-dir /ABS/REPO/.xn02` 使用。** 不要仅为了改名移动状态目录：数据库记录包含输入快照和输出的绝对路径。旧远程回执仍能恢复和过滤，新作业使用 `.hpc-mcp-*` 回执。集群名和 SSH 别名（例如 `xn02`）属于用户的连接配置，无需改名。若仓库本身也搬过目录，应保留旧快照路径可访问，或先完成路径迁移再验收旧作业。

## 7. 卸载与数据清理

先停止客户端中的本服务，解除注册，再卸载 Python 包：

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
```

源码在 `src/hpc_mcp/`，测试在 `tests/`。标准库 CLI 可通过 `PYTHONPATH=src python3 -m hpc_mcp` 使用，适合依赖安装前的诊断。

用户已在真实 LSF 上完成提交、状态查询和结果同步。本仓库的离线测试覆盖两种调度器；Slurm 实际作业及 MCP SDK 握手仍需目标环境验收，依赖锁文件尚未生成。

当前支持普通批处理脚本；复杂 Python 提交入口、数组、依赖、后台轮询和自动回传尚未实现。LSF 超出 `bjobs` 保留窗口时尚无长期记账回补。详细路线见 [PLAN.md](PLAN.md)。

## 许可证

本项目采用 GNU General Public License v3.0（`GPL-3.0-only`）。完整许可条款见 [LICENSE](LICENSE)。
