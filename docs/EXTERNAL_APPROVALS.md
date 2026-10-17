# 可选外部签名审批

默认 `client_attested` 模式继续兼容现有客户端：确认说明和 reviewer 会话信息由客户端声明。
需要独立审批者背书时，在服务启动时配置 `--approval-policy /ABS/public-policy.json`
或环境变量 `HPC_MCP_APPROVAL_POLICY`。`approval_status` 和 `settings_get.approval`
返回实际模式。服务只保存公钥和签名凭据，不生成、读取或保存私钥，不调用模型 API。
签名校验使用 approval extra 的 cryptography，版本范围与 requirements-mcp.lock 一致。
启用时使用 `.venv/bin/python -m pip install -c requirements-mcp.lock -e '.[mcp,approval]'`；
当前标准 MCP extra 的锁定依赖也已包含该版本，但显式声明 approval 不依赖 SDK 的传递依赖。

## 信任边界

签名证明配置的审批者或 reviewer 背书了精确内容，不证明聊天用户身份、reviewer 实际运行方式
或计算结果的科学正确性。独立上下文仍由真实 reviewer 负责，并在其签名报告中声明。

私钥应放在独立审批系统或独立 OS 账户下。服务源码、启动配置、公钥策略和状态目录也必须
受到访问控制：如果 agent 可以修改这些位置或更换服务进程，它仍能绕过服务内门禁。
不能把同一 agent 可访问的私钥文件或可任意修改的启动参数称为独立审批通道。
本项目提供签名验证与动作绑定，不代替部署隔离，也不自带聊天 UI 或远程签名服务。

## 公钥策略

以下只是格式示例；公钥替换为独立签名端提供的原始 32 字节 Ed25519 公钥的标准 base64：

```json
{
  "version": 1,
  "max_lifetime_seconds": 86400,
  "keys": {
    "human-approver": {
      "public_key": "BASE64_RAW_ED25519_PUBLIC_KEY",
      "role": "approver"
    },
    "independent-reviewer": {
      "public_key": "BASE64_RAW_ED25519_PUBLIC_KEY",
      "role": "reviewer"
    }
  }
}
```

一个 key ID 对应一种角色；配置不同公钥并由不同主体保管，以落实审批和审阅分离。
策略在进程启动时读取；轮换或撤销公钥后，应重启 MCP、协调器和 worker，并保持同一策略。
CLI 设置的绝对策略路径通过环境传给后台进程。策略缺失、格式错误或签名失败拒绝操作，
不回退到客户端声明模式。已有客户端声明的任务或应用在严格模式下需要重新确认/审阅。

```bash
hpc-mcp --config /ABS/clusters.toml --state-dir /ABS/state \
  --approval-policy /ABS/public-policy.json serve
```

## 签名请求与回传

1. 读取并展示真实任务/批次/工作流计划，或者把原始源码、review_request 和报告交给独立 reviewer。
2. 使用 `approval_request(action, subject, token)` 或 CLI `approval-request ACTION SUBJECT TOKEN`
   获得 claims 模板。签名端必须独立核对实际材料与 token，不能只对 agent 提供的摘要盲签。
3. 签名端填入已配置的 actor、带时区的 issued_at 和 expires_at；保留其他字段与 nonce。
   签名对象是 claims 的 UTF-8 JSON：键排序，ensure_ascii=false，allow_nan=false，
   separators 为 `(',', ':')`。使用 Ed25519 签名，将 64 字节签名转换为标准 base64。
4. 客户端仅回传 `{ "claims": {...}, "signature": "BASE64_SIGNATURE" }`，调用
   `approval_import(envelope)` 或 CLI `approval-import /ABS/envelope.json`。
5. 再调用原有 authorize/start/confirm/activate 接口。导入本身不执行被批准的动作。

签名请求包含 version、domain（hpc-mcp.approval.v1）、state_dir、action、subject、token、nonce、actor、issued_at、expires_at。
state_dir 是规范化绝对路径，防止跨状态目录复用；action 和 subject 防止跨操作、跨对象复用。
单个 envelope 最大 8 KiB，有效期不得超过策略的 max_lifetime_seconds；接受时检查有效期。
已接受的自动化许可按接收时间验证，允许任务在签名接受窗口结束后继续完成。
更改计划需新签名；撤销任务许可会封禁该 nonce 对这个 run 的授权，原签名不能再次恢复它。

签名端使用 Python 时，规范化和签名相当于：

```python
payload = json.dumps(claims, sort_keys=True, ensure_ascii=False,
                     allow_nan=False, separators=(",", ":")).encode("utf-8")
signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
```

private_key 仅存在于独立签名端，上述片段不应在 agent 进程内执行。

## 动作绑定

| action | subject | token 和材料 |
| --- | --- | --- |
| job_authorize | run_id | job_submission_plan.review_token，完整脚本、输入、集群与同步/清理权限 |
| workflow_start | workflow_id | workflow.review_token，完整具体 execution_plans 和依赖同步范围 |
| batch_start | batch_id | batch.review_token，完整 frozen_plan；内部只委托给其固定 workflow 与 run |
| setup_flow_confirm | flow_id | business.confirmation_token；应用流程包含精确 activation_plan |
| application_review_submit | application/version | SHA-256(canonical JSON({packet_sha256: SHA-256(canonical JSON(review_request)), report: 原始报告}))，需要 reviewer 角色 |
| application_activate | application/version | application_activation_plan.approval_token，包含来源、审阅证据、集群、依赖和 runner 设置 |
| handler_runner_configure | handler-runner | SHA-256(canonical JSON(完整规范化 RunnerSettings))，不是部分配置对象 |
| history_archive | events | history_archive 预览的 approval_token，绑定年龄/数量及精确事件 ID 和证据摘要；事件变化需重新预览签名 |

缺少签名的门禁返回 external_approval_required 和 approval_request，便于请求对应签名。
review_submit 的签名覆盖原报告和原始审阅材料摘要，不能仅修改 reviewer_session 字符串通过。
应用激活和 prepare 复核已签署的审阅/激活材料，后台提交/同步/清理复核 run 或聚合计划的签名。
材料、依赖、runner 设置或配置变化会拒绝原许可；依赖派生的新输入仍需重新审阅和批准。

本模式覆盖上述“确认/背书”门禁；只读查询、撤销、暂停与取消仍沿用现有接口。
它不构成针对所有工具或宿主文件写入的通用访问控制系统。
