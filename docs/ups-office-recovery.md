# UPS 办公室备用通道

服务器继续承担后台、台账、DHL/FedEx 查询与通知。UPS 官网在服务器直连不可用时，办公室 Chrome 可查询授权运单并回填同一台账。该路径依赖 Windows 主机在线及用户登录，不替代长期可用的服务器出口或 UPS 官方 API 凭据。

## 手动执行

在仓库根目录执行 `python tools/local_ups_channel.py --refresh-active --retry-failed`。

- `--skip-apply` 只采集，`--apply-only` 只回填已有结果。
- 仅在途有效包裹需要刷新；失败结果保留用于诊断，不能清空已保存的官网快照。
- `request_rejected` 表示官网拒绝请求。`not_found` 仅用于官网明确查无运单；HTTP 200 本身不算轨迹成功。
- 同一工作区整轮持文件锁；服务端回填持既有 `.auto-track.lock`，并校验绑定版本、时间和哈希。
- 回填需要同版本 `tools/server_merge_local_results.py` 与 `wire_results.py`。先发布服务器代码，再运行新本机通道。

## 恢复日常任务

使用当前 Windows 用户执行 `powershell -NoProfile -File tools/local_ups_task.ps1 -Register`。

计划任务名称 `Logistics-UPS-Office-Fallback`。每日 08:00 和登录时运行；错过时允许补跑，同一任务不并发；执行超时 3 小时，进程非零退出后每 30 分钟重试、最多两次。仅使用当前用户交互式登录，不保存 Windows 密码。运行日志保存在被 Git 排除的 `logs/`。

停止备用通道可在任务计划程序禁用此任务；服务器主服务继续工作。不要同时启动旧的 `daily_ups_loop.ps1` 常驻循环。

## 验收

1. 回归：`python -m pytest -q`；Windows Git Bash 的服务模拟用 Linux CI 复核。不得跳过绑定、并发、回填和通知幂等测试以获得通过。
2. 官网实票：统计在途有效包裹，逐票区分成功、明确拒绝、查无运单和网络失败。只计本轮真实官网响应；不把旧缓存当成新结果。
3. 回填：服务器保留一致性备份；成功结果的 `observed_at`、完整 `events` 与 `result_hash` 在包裹快照及认证 API 中一致。同单其他包裹保持；重复应用不增加相同通知任务。
4. 运行：确认生产健康、数据库完整、旧回滚容器停止。确认 Windows 任务触发器、执行路径、非零重试及 `IgnoreNew` 配置。
5. 局限：官网拒绝、无有效运单、办公室电脑离线均应保留为未恢复，不能报“全部稳定”。
