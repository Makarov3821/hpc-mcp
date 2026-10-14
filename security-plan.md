# 安全风险与修复计划

本计划记录项目审查中发现的风险；前三项已通过临时目录与本地调度器模拟复现。
修复只影响本地状态、结果同步和远程清理边界，不修改用户 SSH 配置或自动提交真实任务。

| 编号 | 优先级 | 风险 | 当前状态 | 修复方向 |
| --- | --- | --- | --- | --- |
| SEC-001 | 高 | 显式同步目标允许包含状态目录，结果可覆盖 history.sqlite3 | 已修复，离线回归通过 | 授权与执行共用双向目录重叠检查，保留受管 outputs 例外 |
| SEC-002 | 高 | overwrite=error 在检查与复制之间会覆盖并发创建的文件 | 已修复，离线回归通过 | 在目标文件系统暂存完整文件，原子安装且禁止覆盖；目录 fd 防止路径符号链接替换 |
| SEC-003 | 高 | 同步后远程已选结果改变，清理仍可删除尚未保存的内容 | 已修复，离线回归通过 | 预览和实际删除均在远程核对已选结果 SHA-256；变化、缺失、新匹配文件或不安全路径拒绝清理 |
| SEC-004 | 中 | 文件归位与数据库登记之间缺少持久化恢复，可能留下部分结果 | 待处理 | 安装日志与崩溃恢复；识别已安装且哈希一致的结果 |
| SEC-005 | 中 | SSH 输出预算在进程结束后检查，临时文件可提前耗尽磁盘 | 待处理 | 执行期间监测输出体积，超限终止进程并保留有界诊断 |
| SEC-006 | 架构边界 | 用户确认与独立审阅由客户端声明，服务无法验证真实用户或独立上下文 | 已知边界，待设计 | 如需强制人工审批，采用 agent 无法代填的客户端或独立审批通道 |
| SEC-007 | 中 | 历史事件持续累积，查询缺少 run_id 索引和保留策略 | 待处理 | 为事件查询加索引，设计可审计的归档与保留策略 |

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

SEC-002 的逐文件原子安装不代表整批文件具有事务性；中途失败与数据库登记的恢复属于 SEC-004。
SEC-003 的删除前校验不能阻止远程任意进程在校验后继续写入；严格保证需要生产者停止写入，
或集群侧不可变快照/共同遵守的写入协议。只校验已选择的输出，未选择的数据仍按授权清理范围删除。

## 本次修复与验证记录

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
- SEC-004 至 SEC-007 保留为后续工作；没有调用真实集群或修改 SSH/agent 配置。
