# vLLM-Ascend CI Monitor

[中文](#中文) · [English](#english)

A lightweight, contributor-owned status page for the public CI of
[`vllm-project/vllm-ascend`](https://github.com/vllm-project/vllm-ascend).

## 中文

这是一个**不需要 vLLM-Ascend 管理权限**的被动 CI 可用性监控站点。

它每小时读取 vLLM-Ascend 公开的 GitHub Actions workflow runs / jobs，将结果保存为 JSON，并通过 GitHub Pages 展示最近 24 小时状态、每日可用率、不可用时间区间，以及历史上观察到的概率性/不稳定 CI 用例。

### 当前判定口径

监控采用刻意严格的规则：

1. **任意 CI job 失败即不可用**  
   只要某个已完成 job 的 conclusion 不是 `success` / `neutral` / `skipped`，对应小时记为 `Down`。

2. **概率性/不稳定用例出现即不可用**  
   系统会按 `Workflow :: Job` 保存最近 30 天结果。一旦观察到：
   - 同一 commit 的同名 job 同时出现 PASS 和 FAIL；或
   - 至少 5 个样本中出现至少两次 PASS↔FAIL 状态切换，

   该 job 会被永久标记为 `probabilistic / unstable`。以后只要这个 job 在某小时出现，**即使本次 PASS，该小时仍记为 Down**。

3. **证据不足不算可用**  
   没有 CI 活动，或公开 API 请求预算导致覆盖不完整时，状态为 `Unknown`。`Unknown` 不进入已观测可用率分母。

4. **Degraded 也属于不可用时间**  
   例如跨越完整小时仍未结束的活跃 workflow，会显示为 `Degraded`。Dashboard 的不可用小时包括 `Down + Degraded`。

### 状态

| 状态 | 含义 |
|---|---|
| Healthy | 该小时已观测 CI 全部正常，且没有概率性用例出现 |
| Down | 存在失败 job，或概率性/不稳定 job 出现 |
| Degraded | 存在长时间未完成的活跃 CI，但没有已确认失败 |
| Unknown | 没有足够证据，或 API 覆盖不完整 |

### 运行方式

- GitHub Actions 每小时第 17 分钟运行一次。
- 首次运行尝试观察最近 24 小时；之后每次回看最近 3 小时以修正延迟完成的 job。
- 默认不需要任何 Secret。
- 为避免匿名 GitHub API 的 60 req/hour 限制，单轮保守控制在约 52 次 API 请求。
- 如果未来希望提高采集容量，可以添加仓库 Secret `UPSTREAM_GITHUB_TOKEN`；不是必需项。

### GitHub Pages

仓库中的 workflow 已包含官方 GitHub Pages 部署步骤。仓库所有者需要在：

`Settings → Pages → Build and deployment → Source`

选择 **GitHub Actions**。之后重新运行 `Monitor vLLM-Ascend CI` workflow 即可发布网站。

### 数据文件

- `data/history.json`：最近 90 天的小时状态。
- `data/tests.json`：所有已观察 job 的 30 天样本和概率性判定。
- `data/state.json`：去重状态，仅供采集器使用，不发布到网站。

### 局限

这是基于公开 CI 活动的**被动观测**，不是 vLLM-Ascend 官方 SLA，也无法主动探测社区的 A2/A3 runner。故障原因目前使用 job 名称和失败 step 名称做启发式分类；但“是否可用”的核心判定不依赖故障分类，因此不会因为分类不准而把失败 job 误算成可用。

---

## English

This is a passive CI availability monitor that requires **no vLLM-Ascend administrative access**.

Every hour it reads public GitHub Actions workflow runs/jobs from `vllm-project/vllm-ascend`, stores compact JSON history, and publishes a bilingual GitHub Pages dashboard with the last 24 hours, observed daily availability, unavailable intervals, and historically probabilistic/unstable CI jobs.

### Strict availability policy

1. **Any failed CI job makes the hour unavailable.**
2. **Any appearance of a detected probabilistic/unstable job makes the hour unavailable, even if that particular execution passes.**
3. **Missing or partial evidence is `Unknown`, never `Healthy`.**
4. **`Degraded` time is counted as unavailable time on the dashboard.**

A job is automatically marked probabilistic/unstable when public history shows either mixed outcomes for the same commit, or repeated PASS↔FAIL transitions with enough samples. The flag is sticky by design.

### Operation

- Runs hourly at minute 17 via GitHub Actions.
- First run attempts a 24-hour bootstrap; later runs re-check the last 3 hours.
- No secret is required.
- Anonymous API use is kept under a conservative request budget.
- Optional secret: `UPSTREAM_GITHUB_TOKEN` for higher API capacity.

### Pages setup

In this repository, select:

`Settings → Pages → Build and deployment → Source → GitHub Actions`

Then re-run the `Monitor vLLM-Ascend CI` workflow.

### Caveat

This is passive observation of public CI activity, not the official vLLM-Ascend SLA. It cannot actively probe community runners.
