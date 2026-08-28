# vLLM-Ascend CI Monitor

[中文](#中文) · [English](#english)

A contributor-owned, bilingual status page for the public CI of [`vllm-project/vllm-ascend`](https://github.com/vllm-project/vllm-ascend).

> GitHub Pages deployment is enabled for this repository.

## 中文

这是一个**不需要 vLLM-Ascend 管理权限**的被动 CI 可用性监控站点。

每小时通过 GitHub Actions 读取上游公开 workflow runs / jobs，将结果保存为 JSON，并在 GitHub Pages 展示：

- 最近 24 个完整小时的 CI 状态；
- 当天已观测可用率、可用/不可用小时、观测覆盖率；
- 具体不可用时间区间；
- 所有已观测 CI workflow/job；
- performance、accuracy、acceptance、benchmark、eval 等概率敏感用例。

### 判定口径

采用刻意严格的规则：

1. **任意实际 CI workflow/job 失败 = 不可用**  
   实际构建/测试 workflow 或 job 的 conclusion 只要不是 `success` / `neutral` / `skipped`，对应小时记为 `Down`。label、stale、PR close cancel、命令处理等仓库管理机器人流程不纳入 CI 健康度。

2. **概率敏感用例出现 = 不可用**  
   以下 job 会标记为 `probabilistic / probability-sensitive`：
   - 名称属于 performance / perf / accuracy / acceptance / benchmark / eval / precision / pass-rate 等类别；或
   - 同一 `head_sha`、同名 job 被实际观察到既 PASS 又 FAIL/timeout。

   一旦属于该类，只要它出现在某小时，**即使这次成功或被 skipped，该小时仍记为 Down**。纯 matrix 生成、setup、prepare、artifact merge/upload/download 等辅助 job 会排除。

3. **证据不足 = Unknown**  
   没有实际 CI 活动，或公开 API 覆盖不完整时，不会冒充 Healthy；`Unknown` 不进入可用率分母。

4. **Degraded 计入不可用时间**  
   长时间跨小时仍未完成的 CI 会显示为 `Degraded`，Dashboard 的不可用小时为 `Down + Degraded`。

### 状态

| 状态 | 含义 |
|---|---|
| Healthy | 该小时有实际 CI 证据，未发现失败或概率敏感用例 |
| Down | 存在失败 workflow/job，或概率敏感 job 出现 |
| Degraded | 有长时间未完成的活跃 CI，但还没有已确认失败 |
| Unknown | 没有足够证据，或 API 覆盖不完整 |

### 完整性处理

GitHub 对带过滤条件的 workflow-runs 查询存在约 1000 条分页上限。采集器会在某个 event/time range 超过阈值时自动把时间范围二分，直到每个切片都可以完整分页，再合并去重。`data/history.json` 会记录每类 event 的 `expected / fetched / complete / slices`，覆盖不完整时不会标绿。

### 运行方式

- GitHub Actions：每小时第 17 分钟执行；
- 首次/规则升级：回填最近 24 小时；
- 正常运行：回看最近 3 小时，修正延迟完成的 job；
- 数据保留：90 天小时历史；
- 默认使用仓库 `GITHUB_TOKEN` 读取公开上游数据；如需单独 token，可添加 `UPSTREAM_GITHUB_TOKEN` Secret。

### GitHub Pages

仓库所有者只需设置一次：

`Settings → Pages → Build and deployment → Source → GitHub Actions`

之后 `Monitor vLLM-Ascend CI` workflow 会自动发布网站。

### 数据文件

- `data/history.json`：小时状态、失败证据、概率敏感命中和采集完整性；
- `data/tests.json`：所有已观测 workflow/job 的 30 天样本与概率敏感判定；
- `data/state.json`：采集去重状态，不发布到网站。

### 局限

这是公开 CI 活动的**被动观测**，不是 vLLM-Ascend 官方 SLA，也无法主动向社区 A2/A3 runner 发探针。故障类别依赖 job/失败 step 名称做启发式分类，但“是否不可用”的核心判断直接使用公开 workflow/job conclusion，不依赖分类准确度。

---

## English

This is a passive CI availability monitor that requires **no vLLM-Ascend administrative access**.

It runs hourly in GitHub Actions, reads public workflow runs/jobs from `vllm-project/vllm-ascend`, stores compact JSON history, and publishes a bilingual GitHub Pages dashboard.

### Strict policy

1. **Any real CI workflow/job failure makes the hour unavailable.** Repository housekeeping automation such as labels, stale handling, PR-close cancellation and command bots is excluded.
2. **Any probability-sensitive job makes the hour unavailable whenever present, even if that occurrence passes or is skipped.** Performance, accuracy, acceptance, benchmark, eval, precision and pass-rate jobs are policy-marked; same-commit PASS/FAIL job flips are also detected. Helper/matrix/artifact jobs are excluded.
3. **Missing or partial evidence is `Unknown`, never `Healthy`.** Unknown hours are excluded from observed availability.
4. **`Degraded` is counted as unavailable time.**

The collector automatically time-slices workflow-run queries when GitHub's filtered pagination approaches its ~1000-result cap, and records per-event completeness in `data/history.json`.

### Operation

- Runs hourly at minute 17.
- First run and rule upgrades backfill the latest 24 hours.
- Normal runs re-check the latest 3 hours.
- Keeps 90 days of hourly history.
- Uses the repository `GITHUB_TOKEN` by default; optional secret: `UPSTREAM_GITHUB_TOKEN`.

### Pages setup

Select once:

`Settings → Pages → Build and deployment → Source → GitHub Actions`

The `Monitor vLLM-Ascend CI` workflow will then publish the site automatically.

### Caveat

This is passive observation of public CI activity, not the official vLLM-Ascend SLA. It cannot actively probe community runners.
