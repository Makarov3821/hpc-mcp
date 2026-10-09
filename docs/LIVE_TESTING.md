# 真机验收清单

离线与官方 SDK 测试已通过；本清单用于核对实际 LSF／Slurm 和 agent 客户端。始终使用同一套 `--config /ABS/CONFIG --state-dir /ABS/STATE`，保留命令 JSON、run_id、workflow_id 及原始记账证据。仅在用户授权的集群、队列和小任务范围执行。

## 1. 安装与新集群闭环

1. 重启 MCP，确认 tools/list 有 56 个工具，settings_get 可发现 workflow／accounting 设置；旧历史仍可读取。
2. cluster_probe 检查 SSH、队列、明确工作目录；可选 module avail。不存在的路径应明确失败，不递归搜索软件环境。
3. script_inspect 分析实际脚本；profile_draft 后逐项核对队列、核数、内存范围、初始化顺序、程序和输出规则。复杂脚本未解析项由用户补充。
4. 用户确认后 profile_confirm，再用 profile_validate 准备小验证作业。审阅脚本、资源后 job_submit，完成后再次 profile_validate 读取计算节点证据；排队不应显示已验证。
5. 重启客户端，检查 profile_get 与默认绑定仍在；新增参数覆盖应有独立验证范围。

## 2. 独立任务与并发

准备 A/B 和 A/D 两个微型任务，每项只选自己的输入；输出写各自 results 或明确日志。workflow_plan 限制 max_in_flight=1、submissions_per_minute=2、max_submissions_per_tick=1。

审阅并授权这份计划后 workflow_start；先手动 workflow_tick，等待至少 poll_interval_seconds 再推进。检查第二项仅在第一项新确认终态后提交，排队／未知状态仍占容量。已登记任务直接 job_submit 应被阻止。

首次提交后重启 MCP，再查 workflow_status 并推进，确认不重复提交。workflow_pause 后推进不应放行新任务；暂停不取消远程作业。可再显式启动监控协调器，检查 agent 退出后授权计划仍推进，测试完 monitor_stop 并确认 stopped。

## 3. 文件依赖与错误路径

父任务写一个小文件 parent.chk，子任务准备只包含输入与脚本，读取未来的 old.chk。使用 files_ready 映射 parent.chk → old.chk，先核对 JSON 计划再授权。

检查父成功后有依赖同步操作，子任务实际编号发生变化；其远程独立目录中 old.chk 内容正确，原子输入和准备快照不变。用实际编号查状态、sync；输出仍回原子任务目录。归位已有文件发生冲突时应报告并等待明确处理。

分别验证父非零退出、父取消、缺少 parent.chk：子任务不能错误启动。对 Gaussian 应用成功条件，用 gaussian_prepare 准备父任务；核对调度 DONE 但 Gaussian Error termination 时不会放行。后续 Gaussian 输入若引用尚未生成的 oldchk，先使用通用／模板 prepare；gaussian_prepare 当前要求旧 checkpoint 已在本地。

## 4. 输出、记账与清理

1. 对实际执行编号 monitor_watch，明确 log／chk 及覆盖策略；检查最终同步回 A/B、A/D，成功暂存不积压。清理默认关闭，先预览再决定应用。
2. 完成后 job_usage 刷新记账，与原 bacct／sacct 输出对照。核对排队秒数、运行秒数、申请／分配／实际 CPU 的区别和内存单位；Slurm RSS 不当作整个作业峰值。
3. job_usage(refresh=false) 与 usage_report(project_root=A) 应不访问 SSH；统计显示已知／缺失数量和分页范围。刷新失败须保留旧证据并标记陈旧，缺失指标不显示为零。
4. 保存一个成功任务和一个失败／缺失样本的 JSON；重启后重复缓存查询，确认历史和执行编号关系不丢失。

## 记录验收结论

分别记录调度器／版本、队列、应用环境、客户端、通过项和原始错误。实际记账格式与站点权限不一致时提供脱敏输出用于回归，不通过猜测字段或放宽身份检查完成验收。数组、完整动态 Python 提交器转换和 packing 不属于本次验收范围。
