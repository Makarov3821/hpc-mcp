# 本地 handler 执行边界（Phase 10A）

生成提交脚本与远程执行是两个边界。本执行器只处理注册的 Python handler，不执行原 wrapper，不约束远程计算脚本的集群账户权限。普通 prepare 与 workflow 依赖派生调用同一执行器；独立代码审阅与用户确认仍然必需。

## 安装与能力探测

默认 `isolated`，需要 Linux、bubblewrap 和 libseccomp（Debian/Ubuntu 包名为 `bubblewrap`、`libseccomp2`）。不自动提权安装。namespace、seccomp 或所需 bubblewrap 选项被宿主限制时，`handler_runner_status(probe=true)` 返回 `available=false` 与诊断，生成返回 `application_isolation_unavailable`，不自动降级。仅存在 bwrap 命令不等于隔离可用。

```bash
.venv/bin/hpc-mcp --state-dir /ABS/agent/hpc-mcp/state handler-runner-status
```

## 可见文件与权限

- 空白文件系统中只开放 Python/系统运行库、`/plugin` 下的插件副本、声明依赖副本与选定输入副本，均为只读。系统第三方 Python 包目录被遮蔽，不继承用户 site 或 PYTHONPATH。
- 输入卡只开放该文件；任务目录开放该目录中未排除的文件。原绝对输入路径保持一致。`.ssh`、`.aws`、`.codex`、`.agents`、`.git`、集群配置及 MCP 状态目录不随任务目录复制；外部凭据／agent 配置目录不能作为输入。符号链接、特殊文件、超过 8192 个条目或容量预算的输入拒绝。
- `/staging` 是有容量上限的 tmpfs；临时文件使用 `staging_dir` 或 TMPDIR。`/tmp` 为只读目录，避免改变脚本内远程 `/tmp/...` 路径的 resolve 结果。handler 不应依赖宿主 CWD。
- 不继承 SSH_AUTH_SOCK、API 密钥或任意宿主环境。单独 network/PID/user namespace、丢弃 capabilities、禁止新增 user namespace，seccomp 禁止 socket、fork/clone、mount、ptrace 等；最多一个 handler 进程，不支持线程／子进程。
- 内存地址空间、CPU 时间、文件大小、文件描述符和 core dump 使用硬 rlimit；墙钟与 stdout/stderr 总量由父进程限制。tmpfs 限制暂存总容量。返回成功／失败后删除本地副本；隔离 namespace 随进程结束销毁。宿主进程被强杀时遗留的 applications/.run-* 副本使用 application_cleanup 按年龄预览／清理，沿用应用锁，正常运行中不并发删除。

## 配置与依赖

`settings_get` 和 `handler_runner_status` 返回完整配置与权限；配置保存在 `<state-dir>/handler-runner.json`。`handler_runner_configure(settings, confirmation_note)` 是完整替换，省略字段恢复默认值。CLI 使用 `handler-runner-configure --settings-file /ABS/settings.json --confirmation-note '用户确认说明'`。

| 设置 | 默认值 | 范围 |
| --- | --- | --- |
| mode | isolated | isolated / trusted |
| memory_bytes | 256 MiB | 32 MiB..4 GiB |
| staging_bytes | 64 MiB | 4096..1 GiB |
| input_bytes | 64 MiB | 1..1 GiB |
| dependency_bytes | 32 MiB | 1..256 MiB，总计 |
| timeout_seconds | 30 | 1..60 |
| max_output_bytes | 262144 | 1024..1048576 |

实际时间与输出预算取 runner 和 manifest 的较小值。声明的顶层依赖最多 32 个；不自动 pip install。确认激活时固定依赖来源与内容哈希，运行时复制／复核，变化返回 `application_dependency_changed`，须复查并重新激活；旧版本若尚无依赖固定记录，先重新激活。第三方依赖的递归 Python 依赖也须明确声明，namespace 包不支持，二进制扩展的外部库需在可见系统运行库内。任务记录保存执行模式、权限、预算及依赖指纹。

`trusted` 仅供用户明确接受宿主用户文件／网络权限时使用；agent 必须解释权限扩大后再获取确认。此模式只有墙钟／协议输出限制和尽力进程组终止，没有文件、网络、内存、进程数或暂存容量隔离。状态及任务证据明确显示 `trusted-subprocess`，不能描述为沙箱。配置确认说明来自可信客户端，不是用户身份认证；不可信客户端能更改配置的风险由部署权限控制。
