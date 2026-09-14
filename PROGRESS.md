# 生产化进度

## 2026-09-14：UPS 官方未找到状态与发布

- UPS 官网 `trackDetails.errorCode=504` 或明确的未找到文本现在归类为 `not_found`，不再误报未知状态，也不进行无效浏览器重试；与 FedEx 的官网未找到语义一致。
- 本地验证：UPS 未找到归类和不重试回归测试通过；全量 `python -m pytest -q` 为 124 passed / 1 个 Linux 专用测试跳过，编译、入口脚本语法和 diff 检查通过。
- PR #15 已合并为 `a2ea9a1` 并发布；生产镜像 manifest 为 `f0ee704d5399…`，容器 `healthy`、重启数 0、数据库 144 单且 `integrity_check=ok`。
- 发布后对 UPS 官网明确未找到响应复测，返回 `not_found=true` 且无第二次浏览器重试；切换前备份 `/app/backups/logistics-backup-20260914-175633.zip`，旧容器 `logistics-track-rollback-ups-notfound-20260914` 与旧镜像 `logistics-track:rollback-ups-notfound-20260914` 保留。

## 2026-09-14：IM 历史查询稳定性与发布

- watcher 的历史查询对同一分页最多重试 3 次（硬上限 5 次），单请求默认 30 秒/最高 60 秒，整轮默认 180 秒/最高 300 秒；重试期间不推进持久游标，确保单轮在 10 分钟自愈阈值内结束。已落库消息由消息 ID 去重，分页中途失败后安全重读。
- CLI 非零退出现在保留退出码和截断后的错误原因，超时/调用异常与无效 JSON 分开报告；历史响应必须包含数组类型的 `messages`，缺字段、错误对象或错误类型都按临时失败重试。
- 本地验证：同页重试、CLI 错误原因、响应结构和整轮硬预算回归测试 4 passed；全量 `python -m pytest -q` 为 122 passed / 1 个 Linux 专用测试跳过，编译、入口脚本语法和 diff 检查通过。
- PR #13 已合并为 `e4e14c0` 并发布；Linux CI 为 123 passed。生产镜像 manifest 为 `ed9aeb0e0095…`，容器 `healthy`、重启数 0、数据库 144 单且 `integrity_check=ok`。
- 发布后用生产身份和持久游标执行真实 IM 历史只读查询，2.82 秒成功返回有效游标；切换前备份 `/app/backups/logistics-backup-20260914-173406.zip`，旧容器 `logistics-track-rollback-watcher-20260914` 与旧镜像 `logistics-track:rollback-watcher-20260914` 保留。

## 2026-09-14：OCR 超时预算与发布

- 生产 OCR 网关 `/v1/models` 在 2.8 秒内返回 HTTP 502；收件原件和队列状态均保留，没有把外部故障记成业务成功。
- 消除 OCR 的嵌套故障重试：单个 HTTP 请求最多 60 秒且 Linux 生产通过进程定时器约束完整响应读取；提供方错误只由持久收件队列最多重试 3 次。完整 OCR 与录单过程共享最多 190 秒硬预算，低于外层 210 秒期限；每个录单子进程最多使用原有 120 秒及剩余预算。
- 成功响应但未识别配对直接进入人工审核；录单 rc=1 或意外退出、超时回持久队列重试，业务 rc=2 才进入人工审核。TLS 证书校验、原件保留和失败关闭规则不变。
- 本地验证：请求上限、provider failure、无配对、录单超时与退出码分类定向测试 6 passed；全量 `python -m pytest -q` 为 118 passed / 1 个仅 Linux 运行的慢流总时限测试跳过，编译、入口脚本语法和 diff 检查通过。Linux 慢流测试由 CI 和服务器候选执行。
- PR #11 已合并为 `61e1ce4` 并发布；Linux CI 为 119 passed，服务器候选慢流在 1.02 秒触发硬超时。生产镜像 manifest 为 `374d30fa5b0c…`，容器 `healthy`、重启数 0、数据库 144 单且 `integrity_check=ok`。
- 发布后现有 OCR 项均在数秒内返回明确 HTTP 502，不再触发 210 秒外层强杀；活动收件队列归零，24 个失败原件完整保留在 dead，待提供方恢复后从后台重试。同期增量官网管线回填 22 / 失败 0，UPS 脱敏真实样本继续返回 `ok=true / 签收`。
- 切换前一致性备份为 `/app/backups/logistics-backup-20260914-170556.zip`；旧容器 `logistics-track-rollback-ocr-20260914` 与旧镜像 `logistics-track:rollback-ocr-20260914` 保留。

## 2026-09-14：承运商响应稳定性与发布

- UPS 与 FedEx 的浏览器响应回调只收集官方响应对象，JSON 读取和状态归一化移到页面等待循环，避免同步 Playwright API 在回调重入时抛错后被吞掉；解析异常现在保留错误类型，不再统一误报为官网无数据。
- UPS 官方响应归一化独立成纯函数，运单号匹配忽略大小写和首尾空白；未知状态继续失败关闭。
- 本地验证：`python -m pytest -q` 为 114 passed；新增末尾响应排空与 FedEx DOM 优先级回归测试；`python -m compileall -q .`、`bash -n deploy/entrypoint.sh`、`git diff --check` 通过。
- 服务器候选烟测：通过生产容器实际 Xray 代理访问 UPS 官网，脱敏历史真实样本返回 `ok=true / 签收` 且包含轨迹详情。FedEx 仍缺已确认的真实运单，当前只完成官方响应解析单测，不能记为端到端通过。
- PR #9 已合并为 `784234c` 并发布，生产镜像 manifest 为 `93d879fc8873…`。容器 `healthy`、重启数 0、共享内存 1 GiB，数据库 144 单且 `integrity_check=ok`；watcher 子进程确认继承 `UPS_PROXY` 和 `UPS_DISABLE_HTTP2=1`，发布后真实 UPS 脱敏样本继续返回 `ok=true / 签收` 和详情。
- 切换前一致性备份为 `/app/backups/logistics-backup-20260914-163705.zip`；旧容器 `logistics-track-rollback-carrier-20260914` 与旧镜像 `logistics-track:rollback-carrier-20260914` 保留。

## 2026-09-14：稳定性恢复候选

- 云表格同步改为非核心步骤；企业身份未绑定时保留失败日志，但不再把已完成的物流抓取、台账回填、备份和通知任务打回重试。
- OCR 将供应商/网络错误与确定性无配对分开：前者最多重试 3 次，后者原子完成收件并进入人工审核；部分成功时在同一事务创建成功订单的 pipeline 与失败配对的 review，并复用既有审核去重键。
- 官网异常与物流停滞改为稳定任务生命周期：同订单同运单只保留当前故障、更新最新观测和错误、保留人工认领人；恢复、删除订单、清空或停用全部包裹时关闭历史 active/dead 任务，之后真实复发可生成新事件。
- 每日对账和管理员私信新增 pipeline 待执行/终止、OCR、人工审核、官网异常与物流停滞数量，官网抓取异常和物流停滞分开统计。
- 本地验证：`python -m pytest -q` 为 111 passed；`python -m compileall -q .`、`bash -n deploy/entrypoint.sh`、`git diff --check` 通过；独立复审无剩余 Critical/Important。
- 生产库备份副本演练：变更前 tracking_failure 活动项 166，连续两次 refresh 后稳定为 21；订单数保持 144，`PRAGMA integrity_check=ok`。线上切换证据将在发布后补记。
- PR #8 已合并为 `dcbe640` 并发布。生产镜像 manifest 为 `3156364752ae…`，容器 `healthy`、重启数 0、共享内存 1 GiB、数据库 144 单且 `integrity_check=ok`；切换前备份 `/app/backups/logistics-backup-20260914-154704.zip`，回滚容器 `logistics-track-rollback-stability-20260914` 保留。
- 发布后清理并审计重试 3 个由旧容器遗留租约的 pipeline 任务，增量管线完整跑通：官网结果回填 16 / 失败 0、运营任务刷新、通知、备份均完成；云表格仍因企业身份未绑定返回 rc=1，但不再拖垮核心管线。

## 2026-09-11：采用评审 + 线上数据与管线修复

- 评审结论：线上镜像 `f45df371` 与 master `bde441e` 一致，SQLite 台账 146 单已迁移、完整性 `ok`，新版整体可靠，采用并继续演进。
- 数据清理：台账中两条 OCR 误读幽灵订单（XSD260801141252 / XSD26080903141532，单号缺位）连同 `shipments.json` 旧源与 `ups_results` 残留一并清除，台账 146→144；切换前备份 `logistics-backup-20260911-preswitch.zip` 与 `manual-cleanup-20260911.db` 保留。
- FedEx 增加页面 DOM 兜底：`/track/v2/shipments` 被 Akamai 403 时，从渲染页文本识别官网状态词（最早命中优先），官网返回“找不到该运单”仍走 not_found。仍缺一条可确认的真实 FedEx 运单做端到端验收。
- 修复绑定版本语义：无 `binding_version` 的历史订单（track 侧传 0、台账侧合成 1）不再误判 `stale binding`，回填从 4/28 恢复到 28/28；`package-update` 退出码与 `track-update` 对齐，终态/停滞等良性 no-op 不再拉红 `wire_results`。
- 已知待办：`docs +sheet-set-cells` 报“当前会话未绑定企业身份”（本机与容器一致），`sync_sheet` 每日 rc=1 属平台绑定问题，待企业成员身份绑定恢复后自行转绿。
- 发布：`cdcf302`(DOM 兜底) → `da7ac52`(版本容忍) → `cf490b4`(退出码) 依次构建推送，生产切到 `cf490b4` 对应镜像，`healthy`、重启数 0、台账 144 单、`integrity_check=ok`、`wire_results` applied 28 / failed 0。回滚镜像保留：`rollback-dom1-20260911`、`rollback-binding-20260911`。

## 2026-09-10：承运商、可靠性与运营工作台

- 可视化后台新增 `admin/operator/viewer` 三档权限和账号管理页；密码使用带随机盐的 PBKDF2 哈希，CSRF 使用独立随机密钥，写操作以登录账号审计，账号变更与审计原子提交。只读账号不显示操作入口；`admin` 是由 `ADMIN_TOKEN` 管理的应急账号，重启时支持密码轮换且不可停用。
- 后台新增可视化登录与退出：浏览器使用随机 HttpOnly/SameSite 会话 Cookie，角色变更、停用或密码更新会立即使旧会话失效；程序调用继续兼容 Basic Auth。
- 生产健康检查新增公开 `/healthz` SQLite 快速校验，并统一使用 `WATCHER_STALE_MIN`；本地备份按 `BACKUP_RETENTION_DAYS` 自动清理过期归档。
- FedEx 已按 DHL 模式改为有头 Chromium 打开官网详情页并拦截官方 JSON；无需 API 凭据，支持独立出口和新浏览器重试。服务器现有两个代理访问 FedEx 追踪接口均被 Akamai 403。
- UPS 增加独立浏览器会话重试；候选镜像在服务器通过真实脱敏样本返回 `ok=true / 签收`。
- 重复预报保留绑定版本与历史，冲突进入审核；所有状态与配对改为单订单 SQLite 事务更新，旧全量快照不能覆盖新字段。
- 通知回执绑定状态事件；发送前进入 `unknown`，进程崩溃或超时不自动重复发送。群与私聊统一由持久通知队列处理，同名/离职缓存失效并支持稳定人员 ID。
- 消息/OCR 完成与后继任务创建置于同一事务；失败原件在结案前不清理。
- 新增认证运营工作台：订单列表/详情、补录人员、增加包裹、换单、OCR 原件与订单精确关联及查看、综合异常待办、重试/认领/结案审计、通知中心和可下钻日报。真实 Chromium 页面验收通过且控制台无错误。
- 新增可配置停滞提醒，逐包裹识别官网抓取失败和成功结果过期，再判断物流未动；缺失观测时间时按 `N/A` 阻断停滞结论。新增多包裹、换单历史和“部分签收”聚合。
- 本地证据：`python -m pytest -q` 为 92 passed；`python -m compileall -q .`、`git diff --check` 与 `bash -n deploy/entrypoint.sh` 通过。
- 服务器候选证据：镜像 `logistics-track:candidate-20260910-v2` 构建成功；生产库事务备份副本 `PRAGMA integrity_check=ok`，迁移后 146 个订单；鉴权日报 API 返回同一分母；真实 UPS 运单经服务器代理返回官网“签收”。
- 承运商服务器实测：两条真实 UPS 脱敏运单与一条真实 DHL 脱敏运单均返回 `ok=true / 签收`。FedEx 官网访客 OAuth 成功，但两个出口的 `/track/v2/shipments` 均明确返回 HTTP 403；台账中两条仅按长度推断的 12 位号码，官网均返回“找不到该运单”。FedEx 端到端业务验收仍需可用出口和一条已确认的真实 FedEx 运单。
- 生产已切换到合并版本 `099782d` 对应镜像；FedEx 页面模式候选能把台账疑似号码识别为“官网找不到”，容器重启后保持 `healthy`，数据库仍为 146 个订单且完整性 `ok`，鉴权管理 API 返回 146 个订单。管理端口仅绑定服务器 `127.0.0.1:18080`；切换前备份、旧镜像和停止的回滚容器已保留。停滞阈值未配置时按规则报告 `N/A`，不生成事实性停滞判断。
- 运营工作台版本已通过 PR #4 合并为 `8be3394` 并发布。服务器镜像 `sha256:d113821c…` 为 `healthy`、重启数 0；数据库 146 个订单、完整性 `ok`，鉴权日报分母为 146，数据过期阈值显示 `N/A`。切换前一致性备份为 `/app/backups/logistics-backup-20260910-181601.zip`，旧服务保留为 `logistics-track-rollback-workbench-20260910`。
- 可视化权限版本已通过 PR #5 合并为 `aa87518` 并发布。服务器镜像 `sha256:bdcb82fa…` 为 `healthy`、重启数 0；生产验证 `admin` 角色可用、密码哈希未泄露、权限 API 不返回哈希，数据库仍为 146 个订单且完整性 `ok`。切换前备份为 `/app/backups/logistics-backup-20260910-183334.zip`，旧服务保留为 `logistics-track-rollback-rbac-20260910`。
- 可视化登录会话已通过 PR #6 合并为 `c7d9e8b` 并发布。服务器镜像 `sha256:aaeb3f3c…` 为 `healthy`、重启数 0；生产登录、会话 Cookie、退出验证通过，数据库仍为 146 个订单且完整性 `ok`。切换前备份为 `/app/backups/logistics-backup-20260910-220619.zip`，旧服务保留为 `logistics-track-rollback-sessions-20260910`。
- 健康检查与备份保留版本已通过 PR #7 合并为 `97ecfa0` 并发布。服务器镜像 `sha256:f45df371…` 为 `healthy`、重启数 0；`/healthz`、生产备份、146 单和数据库完整性均通过。切换前备份为 `/app/backups/logistics-backup-20260910-222406.zip`，旧服务保留为 `logistics-track-rollback-health-20260910`。清理仅限本项目过期容器和镜像后，主机磁盘占用从 88% 降至 86%。

## 2026-09-08：可靠性与服务器发布准备

- 收件按消息 ID 持久去重并完整分页；失败消息、OCR 和管线任务支持租约、退避与死信。
- 业务真值迁移到 `data/shipments.db`；旧 JSON 幂等导入；并发订单更新不互相覆盖。
- 配对冲突进入人工审核；运单版本、观测时间、终态规则和未知承运商状态均为失败关闭。
- 销售订单需唯一精确命中；同名人员不自动私聊。
- 群通知和私聊按事件分别持久化，发送失败不写成功标记。
- 备份使用 SQLite 在线快照、完整性检查和 SHA-256 清单；已提供校验恢复命令。
- 容器心跳、定时成功标记、持久队列接管、全局管线互斥和固定 Xray 版本已实现。
- 本地证据：`python -m pytest -q` 为 27 passed；`bash -n deploy/entrypoint.sh` 与全部 Python 编译检查通过。服务器镜像构建、迁移、健康观察和回滚演练待执行。
- 服务器候选 `fafe966` 已完成隔离迁移与恢复演练：生产副本 139 条，迁移和恢复前后数量一致，SQLite 完整性均为 `ok`。首次 V 盘上传暴露企业成员身份缺失，已改为持久服务器卷作为默认备份；远端上传改为显式启用。
- 服务器生产版本 `9307767` 已运行：Docker `healthy`、重启数 0、连续两轮 watcher 心跳正常、错误日志筛查为空。生产库 139 条、`integrity_check=ok`、持久任务积压 0。
- 生产备份已写入 `logistics-backups` 卷并通过 SHA-256；从该备份恢复到空卷后仍为 139 条且完整性为 `ok`。旧镜像 `e35fe19d9729` 和切换前容器均保留，可直接回滚。
- V 盘上传实测失败原因为“当前会话没有有效的企业成员身份”。该项不阻塞服务器本地持久备份，但不满足异地容灾；修复企业成员身份后设置 `BACKUP_UPLOAD=1` 再验收。
