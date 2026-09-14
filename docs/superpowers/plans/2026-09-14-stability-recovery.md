# 物流闭环稳定性恢复实现计划

> **面向 AI 代理的工作者：** 当前环境没有 `superpowers:executing-plans`；在当前会话按 TDD 逐任务执行并逐项审查。

**目标：** 阻止非核心同步故障拖死业务管线，把不可自动识别的面单转入人工复核，收敛重复追踪异常，并安全处置线上积压。

**架构：** 保留现有 SQLite 任务队列和单进程编排。核心抓取、回填、备份、通知决定 pipeline 成败；云表格同步只记录告警。OCR 的确定性失败进入现有 review 队列；追踪异常使用稳定键表示当前故障，不按每次观测无限新增。

**技术栈：** Python 标准库、SQLite、pytest、Docker。

---

### 任务 1：隔离非核心云表格故障

**文件：**
- 修改：`auto-track.py`
- 测试：`tests/test_reliability.py`

- [x] **步骤 1：编写失败测试**

```python
def test_sheet_sync_failure_does_not_fail_completed_pipeline(...):
    # 仅 sync_sheet.py 返回 False，其余步骤返回 True。
    assert auto_track.execute(options) == 0
    assert store.task_counts()["pipeline"]["succeeded"] == 1
```

- [x] **步骤 2：确认测试失败**

运行：`python -m pytest -q tests/test_reliability.py -k sheet_sync_failure`

- [x] **步骤 3：最小实现**

将步骤声明为 `(args, description, required)`；`sync_sheet.py` 的 `required=False`，失败仍打印但不改变核心 pipeline 结果。

- [x] **步骤 4：确认定向测试通过并提交**

运行：`python -m pytest -q tests/test_reliability.py -k "sheet_sync_failure or failed_pipeline"`

### 任务 2：OCR 确定性失败进入人工复核

**文件：**
- 修改：`auto-track.py`
- 测试：`tests/test_reliability.py`

- [x] **步骤 1：编写失败测试**

```python
def test_valid_ocr_without_ingested_pair_enters_review(...):
    # OCR 进程成功，但没有完整入单结果。
    assert inbox["status"] == "succeeded"
    assert review["payload"]["source_inbox_id"] == "label-1"
```

- [x] **步骤 2：确认测试失败**

运行：`python -m pytest -q tests/test_reliability.py -k valid_ocr_without_ingested_pair`

- [x] **步骤 3：最小实现**

OCR 返回合法 JSON 且进程成功、但无完整配对时，使用 `complete_inbox_with_task()` 原子完成收件并创建 `review`；超时、进程失败、非法 JSON 继续退避重试，默认最多 3 次。

- [x] **步骤 4：确认定向测试通过并提交**

运行：`python -m pytest -q tests/test_reliability.py -k "ocr_worker or valid_ocr"`

### 任务 3：追踪异常去重并纳入日报告警

**文件：**
- 修改：`storage.py`
- 修改：`operations.py`
- 修改：`reconcile.py`
- 测试：`tests/test_operations.py`
- 测试：`tests/test_reliability.py`

- [x] **步骤 1：编写失败测试**

```python
def test_repeated_tracking_failure_keeps_one_active_task(...):
    refresh_operational_tasks(store, first_time)
    refresh_operational_tasks(store, second_time)
    assert active_tracking_failures == 1
```

- [x] **步骤 2：确认测试失败**

运行：`python -m pytest -q tests/test_operations.py -k repeated_tracking_failure`

- [x] **步骤 3：最小实现**

新增只供运营异常使用的 SQLite upsert：稳定键为 `tracking-failure:{order}:{tracking}`，更新当前错误；恢复后关闭，之后再次失败可重新打开。刷新时关闭同订单不再活跃的历史观测任务。

- [x] **步骤 4：补日报行为测试与实现**

日报和管理员私信明确列出 pipeline 死信、OCR 待处理、人工复核及官网追踪失败数量；保持单行管理员私信。

- [x] **步骤 5：确认定向测试通过并提交**

运行：`python -m pytest -q tests/test_operations.py tests/test_reliability.py`

### 任务 4：生产恢复和验收

**文件：**
- 修改：`README.md`
- 修改：`PROGRESS.md`

- [x] **步骤 1：完整验证**

运行：`python -m pytest -q`、`python -m compileall -q .`、`bash -n deploy/entrypoint.sh`、`git diff --check`。

- [ ] **步骤 2：独立审查、候选镜像和隔离验收**

确认不存在 Critical/Important；候选库验证重复 tracking_failure 只保留一个活动项，云表格失败不使 pipeline 失败，OCR 确定性失败进入 review。

- [ ] **步骤 3：生产备份与切换**

先执行在线备份和 `PRAGMA integrity_check`，保留旧容器，再切换候选镜像并验证 `/healthz`、订单数、重启数和日志。

- [ ] **步骤 4：受控处理积压**

运行一次 `operations.py refresh` 收敛旧 tracking_failure；只重试最新的 pipeline retry/pending。历史 pipeline dead 和 OCR dead 保留在后台人工审核，不批量重发通知。

- [ ] **步骤 5：记录证据**

在 `PROGRESS.md` 写入测试数、镜像、备份、回滚容器、处置前后队列数量和未解决外部依赖。
