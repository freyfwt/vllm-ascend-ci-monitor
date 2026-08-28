# vLLM-Ascend CI Monitor

[中文](#中文) · [English](#english)

A contributor-owned bilingual availability monitor for the public CI of [`vllm-project/vllm-ascend`](https://github.com/vllm-project/vllm-ascend).

## 中文

这个项目衡量的是：**vLLM-Ascend 的 CI 能否可靠地产生结论**，而不是“PR 有没有通过”。

因此现在采用下面的核心原则：

> **PR 代码真的有问题，CI 稳定地把它报红，说明 CI 是可用的。**  
> **只有 CI 因网络、下载、Runner/环境、概率性等原因无法给出可靠结论，才算 CI 不可用。**

### 哪些情况算 CI 不可用

- 依赖/下载链路故障：pip、wget、curl、Maven 等因为 DNS、连接、5xx、传输中断而失败；
- 网络故障：DNS、connection reset/refused、TLS timeout、502/503/504 等；
- Runner / 容器 / NPU 环境故障：Runner offline、Docker daemon 不可用、Pod evicted、磁盘满、设备不可用等；
- `startup_failure`；
- 有明确基础设施证据的超时；
- performance / perf / benchmark / accuracy / acceptance / precision / eval / pass-rate 等概率敏感用例出现；
- 同一 `head_sha`、同名 Job 被观察到既 PASS 又 FAIL/timed_out。

### 哪些情况不算 CI 不可用

- PR 编译/构建错误；
- pytest / unittest / AssertionError / 普通功能回归；
- lint / format / mypy / docs link check 等代码质量检查失败；
- 测试逻辑自身 timeout，但没有网络/Runner/环境故障证据；
- PR 更新造成的 cancelled、`action_required`、`stale` 等非基础设施判决。

这些都是 **CI 正常工作并给出的有效代码判决**。

### Unknown

如果公开证据不足以可靠区分“代码问题”还是“CI 基础设施问题”，该小时会标记为 `Unknown`，不会直接算 `Down`。API 覆盖不完整、只有进行中任务、失败 Job 日志不可用等也可能进入 `Unknown`。`Unknown` 不进入已观测可用率分母。

### 概率敏感用例

名称属于 performance / perf / benchmark / accuracy / acceptance / pass-rate / precision / evaluation / eval 等类别的实际测试 Job，会被策略标记为 probability-sensitive。纯 `generate / prepare / setup / matrix / merge / upload / download / collect` 辅助 Job，以及纯 artifact 操作会排除。

按当前约定：**概率敏感用例只要在该小时存在，即使本次 PASS 或 skipped，该小时仍记为不可用。**

### 网站

- Overview：最近 24 小时状态、当天可用率、不可用区间、概率敏感检查；
- 判定标准 / Policy：以表格列出当前所有判定规则。
- 阻塞分析 / Blocker Analysis：提取 main 分支持续存在的问题，展示关键错误日志、严格 PR 归因、作者/合入者、处理建议和连续 PASS 恢复状态。

GitHub Pages: https://freyfwt.github.io/vllm-ascend-ci-monitor/

### 运行方式

- `Monitor vLLM-Ascend CI`：每小时第 17 分钟采集上游公开 Actions 数据；
- `Deploy CI Monitor Pages`：数据或页面更新后独立发布 GitHub Pages；
- 首次/规则升级回填最近 24 小时，平时回看最近 3 小时；
- 自动对超过 GitHub filtered workflow-runs 约 1000 条分页限制的时间段做切片；
- 保存 90 天小时历史。

### 局限

这是公开 CI 活动的被动观测，不是 vLLM-Ascend 官方 SLA，也无法主动探测社区 A2/A3 runner。故障分类会优先读取失败 Job 的公开日志，但无法取得足够证据时会选择 `Unknown` 而不是猜测。

---

## English

This project measures **whether vLLM-Ascend CI can produce a reliable verdict**, not whether a PR passes.

> **If broken PR code is reliably rejected, CI is working.**  
> **CI is unavailable only when infrastructure or probabilistic behavior prevents a trustworthy verdict.**

### Counts as unavailable

- Dependency/download transport failures caused by DNS, connection failures, 5xx responses, or interrupted transfers;
- Network failures such as DNS, connection reset/refused, TLS timeout, 502/503/504;
- Runner/container/NPU environment failures such as runner offline, Docker daemon unavailable, pod eviction, disk full, or device unavailable;
- `startup_failure`;
- Timeouts with explicit infrastructure evidence;
- Presence of performance / benchmark / accuracy / acceptance / precision / eval / pass-rate probability-sensitive checks;
- The same job on the same `head_sha` producing both PASS and FAIL/timed_out.

### Does not count as unavailable

- PR compile/build errors;
- pytest/unittest/assertion failures and ordinary functional regressions;
- lint/format/mypy/docs-link failures;
- Test logic timing out without network/runner/environment evidence;
- cancellation/non-verdict states such as PR-update cancellation, `action_required`, or `stale`.

These are **valid CI verdicts about the code**.

### Unknown

When public evidence cannot reliably distinguish a code failure from an infrastructure failure, the hour is `Unknown`, not `Down`. Partial API coverage and insufficient completed evidence also produce `Unknown`. Unknown hours are excluded from observed availability.

### Dashboard

The site has an Overview tab and a dedicated Policy tab containing the current decision matrix.

It also has a Blocker Analysis tab for persistent main-branch failures. Issues close only after three consecutive related passes. PR attribution is shown only for high-confidence test regressions: the previous three main executions passed, the signature first appears on one merge commit, and it reproduces afterward.

GitHub Pages: https://freyfwt.github.io/vllm-ascend-ci-monitor/
