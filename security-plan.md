# 安全风险与修复计划

本计划记录项目审查中发现的风险；前三项已通过临时目录与本地调度器模拟复现。
修复只影响本地状态、结果同步和远程清理边界，不修改用户 SSH 配置或自动提交真实任务。

| 编号 | 优先级 | 风险 | 当前状态 | 修复方向 |
| --- | --- | --- | --- | --- |
| SEC-001 | 高 | 显式同步目标允许包含状态目录，结果可覆盖 history.sqlite3 | 已修复，离线回归通过 | 授权与执行共用双向目录重叠检查，保留受管 outputs 例外 |
| SEC-002 | 高 | overwrite=error 在检查与复制之间会覆盖并发创建的文件 | 已修复，离线回归通过 | 在目标文件系统暂存完整文件，原子安装且禁止覆盖；目录 fd 防止路径符号链接替换 |
| SEC-003 | 高 | 同步后远程已选结果改变，清理仍可删除尚未保存的内容 | 已修复，离线回归通过 | 预览和实际删除均在远程核对已选结果 SHA-256；变化、缺失、新匹配文件或不安全路径拒绝清理 |
| SEC-004 | 中 | 文件归位与数据库登记之间缺少持久化恢复，可能留下部分结果 | 已修复，新增中断恢复测试 | 持久化安装日志与文件身份/哈希，按原范围接续；成功登记后再清理暂存 |
| SEC-005 | 中 | SSH 输出预算在进程结束后检查，临时文件可提前耗尽磁盘 | 已修复，有界管道测试通过 | 并发读取 stdout/stderr 与写入 stdin；超限立即终止本次进程组，不写入输出临时文件 |
| SEC-006 | 架构边界 | 用户确认与独立审阅由客户端声明，服务无法验证真实用户或独立上下文 | 已实现可选外部签名门禁；部署边界仍适用 | 独立签名者持有私钥；精确计划/报告、公钥角色、有效期、委托与撤销校验 |
| SEC-007 | 中 | 历史事件持续累积，查询缺少 run_id 索引和保留策略 | 已修复，索引/分页/保留/归档测试通过 | 按 run 与游标索引；限制事件页体积；紧凑遥测保留 1000 条；审计证据显式归档 |

## 修复前代码依据与复现

- SEC-001：`submission.sync_policy` 与 `JobService.job_sync` 只阻止目标位于 state
  内部，没有阻止 state 位于目标内部。输入位于其他目录时，授权同步到 state 的父目录，
  远程输出 `state/history.sqlite3` 可覆盖数据库，随后报 `file is not a database`。
- SEC-002：`JobService.job_sync` 先检查 `dest.exists()` 再调用
  `shutil.copytree(..., dirs_exist_ok=True)`。在两步间创建同名结果后，
  `overwrite=error` 仍覆盖新文件。现有 run 锁不能协调不同 run 或外部写入者。
- SEC-003：`JobService.job_remote_cleanup` 检查本地 output_manifest 哈希、调度终态
  和提交回执，但没有复查远程结果。同步后修改远程 `results/value.txt`，清理预览仍返回成功。
- SEC-004：`JobService.job_sync` 在文件归位、删除暂存后才写入 synced 事件。
- SEC-005：`SSHTransport.run` 使用文件捕获 stdout/stderr，退出后才检查 ssh_output_limit。
- SEC-006：`submission.authorize_locked` 检查 token 与确认说明；
  `ApplicationService.review_submit` 检查 reviewer_session 与 fresh_context 声明。
- SEC-007：`History.update` 追加完整 changes 到 events；`History.events` 无分页读取，
  events 表未建立 run_id 索引。

## 验收与限制

前三项需要确定性的离线回归测试，覆盖拒绝路径及正常同步/清理。
使用真实本地 Bash、rsync 和 SHA-256，所有数据位于临时目录，不依赖凭据或真实集群。
隔离执行器测试需要宿主支持 bubblewrap；沙箱能力不足时必须区分环境限制与产品缺陷。

SEC-002 的逐文件原子安装不代表整批文件具有事务性；SEC-004 支持新安装日志的接续，
遇到原范围/远程选择变化或其他写入者修改文件会保留证据、要求人工核查，不做整批回滚。
SEC-003 的删除前校验不能阻止远程任意进程在校验后继续写入；严格保证需要生产者停止写入，
或集群侧不可变快照/共同遵守的写入协议。只校验已选择的输出，未选择的数据仍按授权清理范围删除。

## 第一批修复与验证记录

- SEC-001：`sync_support.validate_destination` 为授权与执行提供统一检查，拒绝 state
  的祖先目录，也覆盖包含 state 的原地回传目标。默认受管输出及正常显式目标保持可用。
- SEC-002：新增 `result_install.py`，目标文件系统中暂存、校验和刷写完整文件。
  error/replace 使用原子 hardlink 安装且拒绝现存文件；merge 使用原子 replace。
  目录 fd 与 O_NOFOLLOW 防止目录符号链接替换；保留结果文件权限和时间。
  安装失败保留下载暂存以供排查，已安装文件不宣称整体事务回滚。
- SEC-003：清理前按最近一次实际同步规则重新预览远程清单，拒绝新出现的匹配文件。
  已选结果的远程 SHA-256 与安全路径检查位于执行预览/删除的同一 SSH 命令中，
  校验失败不执行删除。未选择的文件仍遵循原清理授权范围。
- 新增 `tests/test_result_safety.py`，11 项确定性测试覆盖计划与执行的目录重叠、
  并发文件/目录/符号链接创建、父目录替换、merge 不写入符号链接目标、
  远程等大小等 mtime 内容变化、缺失、符号链接、新匹配文件、哈希失败、特殊文件名
  和同步权限/时间保留。该模块已全部通过。
- 更新 `docs/REFERENCE.md` 的同步与远程清理契约，README 添加本计划入口。
- 验证：完整套件 309 项全部通过（包含 MCP stdio 与隔离执行器测试，沙箱外运行）；
  最后调整后的相关模块 86 项全部通过；compileall 与 git diff --check 通过。
  命令：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`；
  定向测试：`PYTHONPATH=src:tests .venv/bin/python -m unittest test_result_safety test_jobs test_sync_storage test_submission -v`。
- 第一批修复提交为 `7139c8f`：Harden result synchronization and remote cleanup。

## 剩余风险修复

- SEC-004：新增 `sync_installation.py`，在复制临时文件前记录 inode、临时名，
  在发布前记录完整文件身份，发布后刷写目录并登记进度。重启后仅接续原批准范围，
  复查远程 fingerprint、根目录身份与已安装文件 inode/SHA-256；等内容的新 inode 也不覆盖。
  replace 备份包含内容校验，已移动的完整备份不重复归档。完成状态先入数据库再删除暂存，
  清理失败记为 deferred；原版本未记录日志的中断不能自动补证据。
- SEC-005：SSH 使用非阻塞管道和 selectors，在写 stdin 的同时读两条输出流。
  每条流独立限额，超限/超时终止本次进程组并回收，不依赖事后检查临时文件。
- SEC-006：按用户选择实现可选外部 Ed25519 签名审批。私钥不进入 MCP。
  启动时提供受保护的公钥策略；签名绑定协议域、状态目录、动作、对象、精确 token、
  actor、nonce 与有效期。审阅和审批使用不同公钥角色；工作流/batch/setup 仅委托给
  已签署的具体子计划。任务撤销封禁原 nonce 对该 run 的复用；后台继续核对已接收的签名。
  配置/迁移和材料摘要规范见 `docs/EXTERNAL_APPROVALS.md`，启用安装使用 approval extra。
  默认兼容模式明确显示 client_attested；本次没有配置真实签名端或启用真实公钥策略。
- SEC-007：events 添加 run/id、time/id、run/kind/id 索引，事件页按游标、数量和约 1 MiB
  内容预算限制；单条 detail 超过 64 KiB 返回大小/哈希，原 JSON 可按字符分段读取。
  常规状态/进度事件紧凑化，每个 run 保留最近 1000 条，完整当前诊断与安装日志保存在 run。
  授权等审计事件不会自动删除；显式归档先 fsync 完整 JSONL gzip，再事务性删除事件行。
  严格模式的归档签名绑定精确事件 ID 与证据摘要，不能复用到另一批事件。
- 验证增加 `test_external_approvals.py`、`test_security_persistence.py` 和 SSH 管道回归，
  包含真实子进程 SIGKILL 中断复制后的恢复。最终完整套件 **336 项全部通过**
  （包括真实 MCP stdio、隔离执行器与签名/崩溃恢复测试，沙箱外运行），没有跳过项。
  compileall、git diff --check 和 pip check 通过。
  全套命令：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`。
  本轮新增 27 项回归（相对第一批 309 项）：签名审批、恢复/归档/大事件分段，以及 SSH 双向管道。

## 持续适用的部署与存储边界

- 外部签名认证背书，不证明聊天身份、独立上下文实际创建或科学正确性。
  私钥、服务源码、启动配置、公钥策略与状态目录必须由适当 OS 权限保护；
  agent 若可修改它们，不能把服务内签名检查称为独立权限边界。
- 归档策略作用于 events 表；run、应用、setup/workflow 对象和其他证据不自动删除。
  归档文件继续占用磁盘；SQLite 会复用删除后的空页，不自动 VACUUM。
  容量、归档保留和外部备份仍需按用户需要显式安排。
- 未调用真实集群、未提交计算、未修改 SSH/agent 配置；目标环境仍需验收。
