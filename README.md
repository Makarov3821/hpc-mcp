# xn02-mcps：Agent 安装与接入指南

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
.venv/bin/xn02 --help
```

未配置 GitHub SSH 身份时可使用 `git clone https://github.com/Makarov3821/hpc-mcp.git`。这与登录计算集群所需的 SSH 配置分别管理。

安装使用官方 Python MCP SDK v2（`mcp>=2,<3`）。若下载失败，报告依赖安装未完成，不将能运行标准库 CLI 当成 MCP 安装成功。

后续所有示例中的 `/ABS/REPO`、`/ABS/STATE`、`YOUR_SSH_ALIAS` 和路径均替换为实际值。MCP 启动命令必须使用绝对路径，避免依赖当前目录、激活的虚拟环境或 `PYTHONPATH`。

## 3. 配置集群

已有 `clusters.toml` 时先读取。新增或更新配置可以使用 CLI 的 JSON 设置接口：

```bash
/ABS/REPO/.venv/bin/xn02 --config /ABS/REPO/clusters.toml config-set lab \
  '{"ssh_host":"YOUR_SSH_ALIAS","scheduler":"lsf","work_root":"/shared/home/user/jobs","output_mode":"all"}'
/ABS/REPO/.venv/bin/xn02 --config /ABS/REPO/clusters.toml config-get lab
/ABS/REPO/.venv/bin/xn02 --config /ABS/REPO/clusters.toml check lab
/ABS/REPO/.venv/bin/xn02 --config /ABS/REPO/clusters.toml info lab
```

Slurm 使用 `"scheduler":"slurm"`。命令只在登录环境初始化后可用时，设置 `init_scripts` 为远程初始化文件的绝对路径。该文件会被执行，不能放入作业提交等副作用。

配置接口只更新指定设置、保留其他集群，写入前完整校验。也可使用 [TOML 示例](clusters.example.toml)。配置和状态目录无需放入版本控制。

`check` 的 `ok:true` 表示必要命令、目录权限和队列查询通过；共享存储、队列提交授权仍需实际作业验证。SSH 失败、解析失败和 warning 均应分别报告。

## 4. 注册到 Agent 客户端

只修改所选客户端的对应条目，保留用户其他设置。客户端目录不可写时提供准备好的配置片段，明确说明注册尚未完成。

### Codex

可以使用官方 CLI 注册命令：

```bash
codex mcp add xn02-clusters -- /ABS/REPO/.venv/bin/xn02 \
  --config /ABS/REPO/clusters.toml --state-dir /ABS/STATE serve
codex mcp list
```

如需指定超时，可在用户或项目 Codex 配置的相应条目中设置：

```toml
[mcp_servers.xn02-clusters]
command = "/ABS/REPO/.venv/bin/xn02"
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
    "xn02-clusters": {
      "type": "local",
      "command": ["/ABS/REPO/.venv/bin/xn02", "--config", "/ABS/REPO/clusters.toml", "--state-dir", "/ABS/STATE", "serve"],
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

## 开发与当前边界

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

源码在 `src/xn02_mcps/`，测试在 `tests/`。标准库 CLI 可通过 `PYTHONPATH=src python3 -m xn02_mcps` 使用，适合依赖安装前的诊断。

用户已在真实 LSF 上完成提交、状态查询和结果同步。本仓库的离线测试覆盖两种调度器；Slurm 实际作业及 MCP SDK 握手仍需目标环境验收，依赖锁文件尚未生成。

当前支持普通批处理脚本；复杂 Python 提交入口、数组、依赖、后台轮询和自动回传尚未实现。LSF 超出 `bjobs` 保留窗口时尚无长期记账回补。详细路线见 [PLAN.md](PLAN.md)。

## 许可证

本项目采用 GNU General Public License v3.0（`GPL-3.0-only`）。完整许可条款见 [LICENSE](LICENSE)。
