# 生产上线验收报告

日期：2026-09-15

候选分支：`codex/production-readiness-fixes`

验收对象：`4e061da`（业务代码范围 `e1adf41..f05cf7b`）

## 结论

代码和隔离环境自动化验收通过；生产上线条件尚未全部满足，当前不能签署正式上线。Linux 专用硬截止测试、三家官网授权实票、HTTPS/反向代理、Docker 管理面、异地备份 RPO/RTO 和 72 小时灰度仍需在服务器完成。

## 已执行并通过

第一层提交门禁：

- `python -m pytest -q`：166 passed，1 skipped。跳过项为 Windows 不执行的 Linux `SIGALRM` 测试。
- `python -m compileall -q .`：退出 0。
- `bash -n deploy/entrypoint.sh`：退出 0。
- `bash -n deploy/deploy-from-git.sh`：退出 0。
- `docker compose -f deploy/docker-compose.yml config --quiet`：退出 0。
- `git diff --check origin/master..HEAD`：退出 0。

第二层隔离自动化：

- 数据、换单、存储、状态、对账：29 passed。
- 通知、任务接管、回执、并发恢复：47 passed，1 skipped。
- 备份、恢复、篡改拒绝、原件回滚、证据清理：14 passed。
- 停滞、过期、逐包裹日报：25 passed。
- 后台认证、RBAC、CSRF、会话、请求边界：32 passed。
- UPS/DHL/FedEx 响应归一化：19 passed。

测试均使用临时数据库、临时附件目录和测试通知桩，没有发送正式通知，也没有修改生产台账。

## 官网实票结果

仅使用用户提供的运单做只读查询，报告不重复记录完整单号：

| 承运商 | 样本 | 结果 | 验收状态 |
| --- | --- | --- | --- |
| UPS | `1ZC23W53D4…` | 未收到 `GetStatus` 官方响应，返回 `no GetStatus data` | 未通过；需服务器代理/授权出口复测 |
| FedEx | `888000505999` | 官网明确返回 tracking number not found；`not_found` 分类正确 | 解析链路通过；有效实票未验收 |
| DHL | 未提供授权样本 | 未执行 | 待验收 |

按计划要求的每家至少 5 条有效运单、在途/异常/签收覆盖、完整事件对照和成功率分母记录尚未具备，不能把官网层标为生产通过。

## 上线前未完成项

1. 在干净 Linux runner 运行完整测试，确保 `SIGALRM` 硬截止测试通过且没有新增跳过。
2. 使用候选副本完成独立命名卷恢复、附件切换失败回滚和异地副本恢复，记录实测 RPO/RTO。
3. 准备专用测试群、机器人、两个测试用户，以及 UPS/DHL/FedEx 各 5 条授权有效样本。
4. 在服务器验证现有容器 `Mounts`；若仍是 bind mount，先停写、备份、迁移到命名卷并核对订单数、附件数和 `integrity_check`，禁止直接启动空卷。
5. 配置 HTTPS 反向代理、Secure Cookie、可信代理地址，验证 2375 管理面限制和替代管理通道。
6. 完成 72 小时灰度，持续记录 watcher、调度、抓取、回填、通知、备份心跳及 dead/unknown/review 积压。

在这些证据完成前，验收结果为“候选代码可合并，生产上线待验收”。
