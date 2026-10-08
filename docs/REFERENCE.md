# Agent 接口与设置参考

## 设置层级

优先级为：本次工具参数 → 作业准备时保存的文件选择 → 集群默认值。传输超时、校验、压缩和布局默认使用当前集群配置。
`None`/参数省略表示继承；空列表表示明确不包含／不排除任何模式。所有生效的同步参数保存到 `sync_options` 和事件历史。

`settings_get()` 返回完整默认设置模板和运行时路径；`settings_get(cluster)` 返回有效设置；`cluster_configure(cluster, settings)` 接受部分 JSON 设置并原子保存 TOML，立即更新服务中的配置。
服务启动时的配置路径和状态路径分别由 `--config`/`XN02_CONFIG`、`--state-dir`/`XN02_STATE` 设置，不能在运行中移动历史数据库。

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
| `input_exclude` | `.git,.venv,__pycache__,.aws,.ssh,.codex,.agents,.xn02,clusters.toml` 数组 | 输入排除，按文件名或相对路径 glob 匹配 |

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
内部 `.xn02-*` 回执始终被排除；符号链接不下载，特殊文件不作为结果文件支持。这些是接口边界，不能通过 include 模式关闭。
失败下载保留 staging；跨尝试续传和输出大小上限尚未实现。全部下载可能很大，应根据作业结果选择模式和超时。

`final=true` 需要本次成功查询到终止状态且传输成功；运行中或查询失败返回 partial。它表示所选文件传输完成，不保证应用计算正确或覆盖执行目录之外的输出。
输出默认不自动写回原输入目录；远程结果不会被清理。

## CLI 对应选项

```bash
xn02 --config /abs/clusters.toml config-get lab
xn02 --config /abs/clusters.toml config-set lab '{"sync_layout":"direct","sync_overwrite":"replace"}'
xn02 prepare lab /abs/input job.sh --output-mode filtered --output '*.chk' --output '*.log' \
  --output-exclude '*.tmp' --input-exclude '*.bak' --max-input-bytes 2147483648
xn02 sync RUN_ID --mode all --exclude '*.tmp' --destination /abs/results \
  --overwrite replace --checksum --compress --timeout 600
xn02 sync RUN_ID --mode filtered --include 'results/***' --layout direct \
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
Slurm 查询 squeue 再尝试 sacct；LSF 使用 bjobs -a。查询失败保留最后观测并返回错误，任务消失不推断成功。

环境检查不会创建目录。队列可见不表示可以提交；共享目录可访问性需计算节点验证。LSF 槽位统计、Slurm 每节点内存及 GRES 原始字符串均保留调度器意义。
SSH 强制免交互和主机身份校验，身份参数在 OpenSSH 配置管理；本服务不存储密钥或密码。
