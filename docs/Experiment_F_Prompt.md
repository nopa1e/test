# Experiment F — Evidence-Centric RCA Agent（优化版提示词 · v2）

> 本文件是 `Experiment F` 的施工说明，**取代**原先那份泛化 spec，也取代 v1。
> 原 spec 的方法论原则全部保留（Information Gain > Model Complexity、禁止黑盒分数搜索、
> 必须做消融与稳定性分析、必须区分 root cause 与 victim）；
> **数据事实、评分事实、采样粒度、模块优先级、七张表的落点**按本仓库实测重写。

---

## 第 0 节：五条硬事实（先读完再动手）

### 0.1 本项目没有 Trace。绝对不要写 trace_id / span_id / parent_span_id

真实数据只有 7 张表。**Trace 的替代品是 `traffic_flow_metrics`**（主动探针产生的有向调用边），
来源是探针而非 span。请把原 spec 里的 `trace_*` 全部替换为 `flow_*`。

### 0.2 根因单位是 `network_element_id`，不是微服务

单区域 10 个网元，8 个区域共 80 个候选：

```text
br-1, br-2, cr-1, cr-2, fw, traffic-vm, service-vm-1, service-vm-2, service-vm-3, monitor-vm
```

`monitor-vm` 真实数据中已被删除（FAQ Q9），但仍属合法枚举。
这是**网络拓扑**（4 路由 + 4 交换 + 1 防火墙 + 6 业务 VM），不是微服务调用图。
所有 "service" 字样统一改成 `network_element_id` / `node`。

### 0.3 故障类别枚举必须用提交格式

```text
major_category ∈ {link, firewall, resource, routing, service}
                ↑ 是 routing，不是 route
sub_category 用裸名：delay / rate_limit / loss / acl_drop / port_block /
cpu_pressure / memory_pressure / disk_io_pressure / disk_space_low /
process_pressure / softirq_pressure / blackhole / bgp_session_down /
bgp_route_flap / wrong_static_route / ospf6_neighbor_down /
ospf6_cost_anomaly / wrong_default_route / dns_down / dns_wrong_record /
web_5xx / web_slow / auth_timeout / auth_error        （26 个裸名，28 种故障）
```

- 官方赛题包用的是**带前缀**写法（`resource_cpu_high`），但**实测裸名被接受**：
  我们提交裸名拿到 Minor 0.24~0.38 > 0；若服务端按带前缀严格比对，Minor 必然恰好为 0。
  **两种写法都能过，不要改。**
- 类别是**严格字符串相等**，且 `minor = (major正确) and (sub相等)` —— **大类错了子类直接 0**。

### 0.4 【v2 新增】采样粒度一律改成 1 分钟

数据本身的粒度就是 **1 分钟**（`node_metrics` 每网元 20160 行 ÷ 14 天 = 1440/天）。
现在流水线里的 5 分钟是**我们自己 quantize 出来的**，由**一个总开关**控制：

```python
# aiops/config.py:13
bin_minutes: int = 5
```

它被 **7 处 `_prepare_time()`**（`features.py:62/76/92/138/165/206/244`）
和 `graph.py:179` 统一读取。**F 必须把 `bin_minutes` 设为 1。**

#### 代价必须先算清楚

```text
                点数/区域        点数合计(8区域)
5 分钟粒度      36,288          290,304
1 分钟粒度     181,440        1,451,520        ← 5.0 倍
```

而 5 分钟粒度下**已经测到**的 base 阶段耗时：

```text
b_base  3h03m      c_base  1h42m      d_base  4h32m      e_base  5h11m
```

**1 分钟粒度下 base 阶段预计 8~25 小时。** 必须显式处理，否则跑不完：

```text
方案 A（默认，忠实）   --bin-minutes 1，全程 1 分钟
方案 B（加速，需标注） --coarse-pass-minutes 5
        先用 5 分钟做粗检测定位 incident 窗口，再只在这些窗口内以 1 分钟重算
        产物目录必须带 _coarse 后缀，文档必须写明这是近似
```

开发期一律用**单个区域**（`--regions xian`）跑，验证通过再上全域。

#### 1 分钟粒度 ≠ 1 分钟的输出区间

这是最容易做错的一点。**检测与证据用 1 分钟，但最终输出的 `start_time/end_time`
必须是"估计出的故障时长"，不是 1 分钟的 bin。**

原因：AD 的匹配与时间分是这样算的——

```python
# 匹配：Dice >= 0.4
dice = 2 * overlap / (len_pred + len_truth)
# 时间分：只看首尾边界误差
S_AD = 0.7 + 0.3 * max(0, 1 - (|Δ起| + |Δ止|) / 360秒)
```

- 若输出 1 分钟区间，对 10 分钟真值的 Dice 只有 `2×1/11 = 0.18` → **匹配不上，直接 0 分**；
- FAQ 明确单次故障 **1~30 分钟**，官方样例真值是 **8.6 / 9.3 / 13.3 分钟**。

所以规则是：

```text
输出区间 = [估计起点, 估计终点]
  起点 = 该节点最早异常点的前一个正常点（1 分钟精度，不要量化到 5 分钟网格）
  终点 = 该节点恢复到基线的时刻（1 分钟精度）
  约束 = 1 分钟 <= 时长 <= 30 分钟
         观测延展 > 30 分钟 → 按 30 分钟切分（FAQ：不超过 1800 秒）
         观测延展 < 1 分钟  → 扩到 1 分钟
```

**同时必须改 `episode_min_duration_minutes`。** 它才是"预测中位数恰好 5 分钟"的直接原因：

```python
# aiops/config.py:100-101
episode_max_gap_minutes: int = 5
episode_min_duration_minutes: int = 5        ← 强制每个 episode 至少 5 分钟
```

改成：

```yaml
bin_minutes: 1
episode_max_gap_minutes: 1
episode_min_duration_minutes: 1
```

> 这一处改动本身就值得单独测：1 分钟粒度下 episode 的真实延展会**跟着注入故障的真实时长走**
> （8~13 分钟量级），而不是被强制压成 5 分钟。这同时改善 Dice（更容易匹配上）
> 与 `|Δ止|`（从 300~500s 降到几十秒），预期 AD 从 15.2 涨到 19~21。
> **F6 就是这个改动的正式对照**，必须先单独做一遍再叠加其它模块。

### 0.5 【v2 新增】七张表的落点矩阵 —— **每张表都必须被某处读到，一处不落**

这是强制表。生成的代码必须逐格落实；**任何一张表贡献 0 行 → 直接报错退出**。

| # | 表（每区域行数） | 读它的模块 | 读法（强制） | 产出字段 | 验收判据 |
| --- | --- | --- | --- | --- | --- |
| 1 | `node_metrics`（181,352） | `metric_evidence` | 逐 `(node, metric)` 建 1 分钟序列 | `baseline_median / incident_peak / relative_change / change_point / first_anomaly_time` | 9 个网元全覆盖；`change_point` 落在 incident 起点 ±5 分钟内 |
| 2 | `interface_metrics`（1,208,720） | `metric_evidence` + `link_evidence` | 逐 `(node, interface_id, metric)` | `rx/tx_drop_rate_delta`、`rx/tx_error_rate_delta`、`carrier_changes_delta` | **必须能定位到具体 `interface_id`**，不能只到节点级 |
| 3 | `routing_metrics`（5,074,510） | `routing_evidence` | 逐 `(node, metric_name, label)` 取 `baseline → incident` 变化 | `{metric_name, label, baseline, incident, delta, changed_at}` | **15 个 `metric_name` 全部处理**；正常时段 delta≈0 |
| 4 | `scrape_health`（262,080） | `quality_evidence` | `scrape_up / duration / samples / error` 序列 | `采集中断窗口`、`样本数骤降点` | 必须能识别出全区仅 **180 个** `scrape_up=0` 的采样点 |
| 5 | `frr_syslog_events`（91~817） | `log_evidence` | **按消息语义归并**（不是关键词计数） | `{template, node, time, severity, semantic_class}` | 该区域**全部**事件进证据（xian 91 / wuhan 817） |
| 6 | `netflow_5tuple`（35,635,775） | `flow_evidence` + `topology` | 按 `(src_addr, dst_addr, protocol)` 聚合到 1 分钟 | `边的流量曲线`、`first_seen/last_seen`、`变化率` | **必须限量读取**（上限可配），不得全量 load |
| 7 | `traffic_flow_metrics`（52,674） | `flow_evidence` | 逐 `(flow_type, target_domain)` 的 31 列计数器 | `有向边 + error/timeout/latency delta` | 3 条核心区边全覆盖；`elephant` 单独处理 |

#### 每张表"新增了什么信息"（一句话，必须写进汇报）

```text
1 node_metrics        把「节点异常」标量还原成「哪个指标、什么时候、相对基线变了多少」
2 interface_metrics   把故障定位到**具体接口**，并给出链路级丢包/错误/载波变化的直接证据
3 routing_metrics     给出 28 种故障里 7 种 routing 类故障的**直接、可命名证据**
                      （现在只被当"字段存不存在"用 —— 这是全项目最大的浪费）
4 scrape_health       判别「数据缺失」是**真故障**还是**采集问题**；并给出 180 个稀有强信号点
5 frr_syslog_events   给出路由故障的**机制级解释**（哪个 FRR 守护进程在什么时刻报了什么）
6 netflow_5tuple      给出**实际转发关系**与流量起止时刻，是传播证据与精确边界的来源
7 traffic_flow_metrics 给出**有向调用边 + 服务级症状 + 跨区域传播证据**（Trace 替代品）
```

#### 强制产出：`table_usage_report.json`

```json
{
  "node_metrics":        {"rows_read": 181352, "time_range": ["...","..."], "nodes": 9, "fields_used": [...]},
  "interface_metrics":   {"rows_read": 1208720, "interfaces": 34, "fields_used": [...]},
  "routing_metrics":     {"rows_read": 5074510, "metric_names": 15, "metric_names_list": [...]},
  "scrape_health":       {"rows_read": 262080, "scrape_up_zero": 180},
  "frr_syslog_events":   {"rows_read": 91, "programs": ["ospf6d","bgpd"], "templates": 12},
  "netflow_5tuple":      {"rows_read": 2000000, "truncated": true, "edges": 431},
  "traffic_flow_metrics":{"rows_read": 52674, "flow_types": ["web","dns","auth","elephant"], "edges": 25}
}
```

**任何一行 `rows_read == 0` → 抛出异常终止，不允许静默跳过。**

---

## 第 1 节：必须继承的实测结论与纪律

### 1.1 已提交并有分数的四组

| 臂 | 结构 | 分数 |
| --- | --- | --- |
| 旧 A | IF/VAE → RCA-GNN → LLM（读 B 的 rca_scores） | 27.3163 |
| B | LLM Teacher + RCA-GNN Student → incident LLM | **27.6056** |
| 旧 C | IF/VAE → 依赖关系文本 → LLM（用 B 的 artifacts） | 27.8234 |
| 新 C | 同上，严格自跑 | 26.8687 |
| B top292 | 按置信度取前 292 | **7.6335**（−72%） |

**极差 3.4%。** 正确解读：

> 这**不**能证明"GNN 没用"，只能证明**当前所有臂都还没产生有效信号**。
> 两套近乎随机的答案，用什么方法生成都会得到同一个分数。

**证据**：B 与 C 的主导大类完全不同（B: resource 58.4% / routing 9.0%；
C: routing 46.6% / resource 27.1%），Major 分却是 2.19 vs 2.09。
**答案被提示词改写了一大半，分数纹丝不动** —— LLM 的输出是提示词驱动的，不是数据驱动的。

### 1.2 评分公式（来自官方参考评测器源码，已核对）

```text
/202131510121/lyt/AIOPS/aiops-challenge-2026/aiops_challenge_2026/evaluator/{evaluator,matcher,metrics}.py
```

```python
def ad_single(truth, prediction):
    start_delta = abs((prediction["_start"] - truth["_start"]).total_seconds())
    end_delta   = abs((prediction["_end"]   - truth["_end"]).total_seconds())
    time_score  = max(0.0, 1.0 - (start_delta + end_delta) / (2.0 * 180.0))
    return 0.7 + 0.3 * time_score          # ← 不看 Dice

fp = len(predictions) - tp
alpha_fp = 0.7 + 0.3 * tp / len(predictions)          # 值域 [0.70, 1.00]
major = float(pred.major_category == truth.major_category)
minor = float(major and pred.sub_category == truth.sub_category)

Score_AD    = (Σ_{i∈TP} S_AD(i) / N_true) × alpha_fp × 40
Score_RCA   = (Σ S_RCA(i) / N_true) × 40       # rank1..5 → 1.0/0.8/0.6/0.4/0.2
Score_Major = (Σ S_Major(i) / N_true) × 10
Score_Minor = (Σ S_Minor(i) / N_true) × 10
```

匹配用 Dice ≥ 0.4 + 匈牙利算法全局最大权一对一。
**`N_true` 是四个分项共用的固定分母；未匹配的真实故障其 RCA/Major/Minor 全部记 0（漏报门控）。**

### 1.3 反解出的损失预算 —— **F 的优先级来源**

| 可回收 | 环节 | 现状 | 含义 |
| --- | --- | --- | --- |
| **≈ +20** | **RCA 排名** | 匹配上后平均只得 0.31 | 多数匹配上的故障，根因**不在 Top5**，或排第 4~5 位 |
| ≈ +7 | Minor 子类 | 匹配上后正确率 ~5% | 大类对了子类还是错 |
| **≈ +6.5** | **AD 时间分** | `S_AD` 被钉在 **0.70 地板** | `｜Δ起｜+｜Δ止｜ ≥ 360s`，时间分全丢 |
| ≈ +5 | Major 大类 | 匹配上后正确率 ~29% | 5 选 1，随机是 20% |
| ≈ +8 | 召回 | ~0.75 | 剩余部分 |
| ≤ +5.5 | 精确率 `α_fp` | 0.74 | **上限只有 5.5 分，且实际拿不到** |

**减少提交条数最多值 5 分、实际值 0~1 分（top292 掉 72% 即证明）；召回与 RCA 才是主战场。**

### 1.4 已定位的具体缺陷（F 的靶子，均核对过源码）

| # | 位置 | 问题 |
| --- | --- | --- |
| **D1** | `pipeline.py:55 _rank_nodes` | 排序是**纯异常幅度** `0.65*mean + 0.35*p95`，无先验、无拓扑、无 syslog、无时序 |
| **D2** | 实测产物 | rank-1 几乎均匀铺在 **71/80** 网元；`br-*` 占 **48.6%**、`service-vm` 仅 **6%** |
| **D3** | `features.py:_agg_frr` | syslog 被压成 **23 个关键词计数**喂无监督模型，**排序阶段看不到** |
| **D4** | `taxonomy.py` | 类别规则「字段存在即命中」：`has("cpu_usage","load1","load5")` 几乎恒真 |
| **D5** | `routing_metrics` | 15 个路由指标只被当**字段存在性**用，不看变化 |
| **D6** | `propagation.py` | `temporal_precedence_score` 算出来了，但只进 RCA-GNN |
| **D7** | `build_incident_prompt` | 送给 LLM 的 payload 只有 `nodes` + `episodes{anomaly,persistence,growth}`，**无拓扑、无 syslog** |
| **D8** | 实测产物 | 55% 预测塌缩到 `resource/cpu_pressure`；B/C 大类分布迥异却同分 → 类别信号极弱 |
| **D9** | `config.py:13/100/101` | `bin_minutes=5` + `episode_min_duration_minutes=5` 把故障时长**强制压成 5 分钟** |

### 1.5 运行纪律（不可违背）

```text
1. 除非用户明确说"提交"，否则不要提交。每天 5 次额度是稀缺资源。
2. 不得修改正在运行的 bash 脚本（bash 按字节偏移读脚本）——要改就写新文件。
3. 每个实验必须自跑自己的第一阶段，不得复用其他实验的 artifacts。
4. 不得为了 F 大规模改动既有 baseline / A~E 的代码路径。
5. 官方评分只能用于"阶段性验证 / 方案比较 / 关键消融"，禁止作为 loss/reward/objective。
6. 新增模块必须可用 flag 单独关闭。
7. F 用 1 分钟粒度 → F 的 base 产物与 C 的 5 分钟产物**不可互换**，不能拿 C 的 artifacts 充数。
```

---

## 第 2 节：F 要回答的问题（可证伪形式）

```text
F-1  对同一 incident，改变证据集合必须改变结论，且方向可解释。
     （反例：现在改提示词能把 resource 58% 翻成 routing 47%，分数却不变 → 说明是噪声）

F-2  结论必须能被确定性证据预测。
     LLM 给出的 rank-1 / category，与程序化证据（最早异常 / 路由指标跳变 /
     接口丢包 / 过流错误 / scrape 中断）的一致率必须显著高于随机。

F-3  在无 ground truth 条件下，F-1/F-2 必须可用本地指标测出来。
```

**F-1、F-2 是验收标准，F-3 是前提。** 没有 F-3，后面所有工作都是赌。

---

## 第 3 节：分层设计（按 1.3 的可回收分数重排）

```text
F0 = 现基线（IF/VAE + 依赖文本 + LLM，5 分钟）              ← 已有，26.8687
F6 = F0 + 1 分钟粒度 + episode 真实延展                     ← 【先做】打 AD +6.5，且是 D9 的正式对照
F1 = F0 + 本地度量与稳定性框架（第 4 节）                    ← 与 F6 并行，决定后面能否评价
F2 = F0 + 时序先验证据（onset precedence / change point）     ← 打 RCA
F3 = F0 + 路由与接口直接证据（表 2、3）                       ← 打 RCA + Minor + Major
F4 = F0 + 流证据（表 6、7）                                  ← 打 RCA + Major（link/service 类）
F5 = F0 + 候选集约束与对等对比（角色一致性、同角色基线）        ← 打 RCA
F7 = F0 + 矛盾证据与反向方向测试
F8 = F6 + F2 + F3 + F4 + F5 + F7（Full）
```

**顺序理由**：

- **F6 先做**：它只动时间字段与粒度，改动最小、与其它模块正交、且直接把 D9 这个已确认的
  缺陷转成对照。它是唯一「改一处、预期 +6.5 分」的实验。
- **F1 与 F6 并行**：F1 不产生预测，纯 CPU，是后面所有模块的评价前提。
- F2~F5 都是给排序/分类补证据，做完 F1 才知道收益测不测得出来。

RAG 不在主链上（理由见第 7 节）。

---

## 第 4 节：F1 —— 本地度量框架（不消耗评测额度）

### 4.1 稳定性测试

对同一批 incident，在同一份证据上做轻微扰动，重复 N=20：

```text
随机删除 5% metrics 行（三张指标表各自）
随机删除 5% flow 行（traffic_flow + netflow）
随机删除 5% syslog 行
```

统计每节点 `top1/top3/top5 frequency`、`mean_rank`、`rank_std`。

### 4.2 提示词消融（回答 D8 的关键）

对**一个区域**跑三组：

```text
变体 A  原提示词
变体 B  删掉依赖段，其它一字不改
变体 C  内容不变、换问法与顺序
```

量两个数：

```text
1. 三组之间 rank-1 / category 一致率          → 衡量"玄学程度"
2. LLM 输出与确定性证据的吻合率                → 衡量"是否在用数据"
   确定性证据 = 最早异常节点 / 路由指标跳变节点 / 接口丢包节点 / 过流错误边 / scrape 中断点
```

| 一致率 | 与证据吻合率 | 诊断 |
| --- | --- | --- |
| 低 | 低 | 掷骰子 → 先补证据，调提示词无意义 |
| 高 | 低 | 提示词把模型锁死在先验上（如无脑 `cpu_pressure`）→ 证据被淹 |
| 低 | 高 | 模型在用数据、只是对表述敏感 → **此时调提示词才有意义** |

### 4.3 输出

```text
outputs_experiment_f_metrics/
├── stability.json
├── prompt_ablation.json
├── evidence_agreement.json
└── table_usage_report.json
```

---

## 第 5 节：证据模块（每个都要回答"新增了什么信息"）

每个模块必须给出：**输入表 → 输出文件 → 新增信息 → 验收判据**。
答不出"新增了什么"的模块，**删掉，不要为了模块完整而保留**。

### 5.1 `metric_evidence.py`（表 1 + 表 2）

```text
输入  node_metrics / interface_metrics   （1 分钟粒度）
输出  metric_evidence.json
        per (node, interface_id?, metric):
          {baseline_median, baseline_p95, incident_peak, relative_change,
           first_anomaly_time, peak_time, time_to_peak, change_point, severity}

新增信息
  现有的：每个 point 一个 combined_anomaly_score（标量）
  新增的：把标量还原成「哪个指标、什么时候、相对自身基线变了多少」
          + **链路级定位到 interface_id**（现有系统只到节点级）

验收
  9 个网元全覆盖；interface_metrics 必须能输出 interface_id 级结论；
  基线窗口不得包含 incident 本身；对 resource 类故障 change_point 落在 incident ±5 分钟内
```

Change point 用 **CUSUM / 简化 PELT**（自实现，不引重依赖；`ruptures` 未装就用 CUSUM）。
**不要为此安装新包。**

### 5.2 `routing_evidence.py`（表 3）—— **本数据集最有价值的新证据**

15 个 `metric_name` 与故障子类**接近一一对应**：

| metric_name | 对应 sub_category | 判据 |
| --- | --- | --- |
| `bgp_peer_up` → 0 | `bgp_session_down` | peer 计数下降 |
| `bgp_peer_uptime_seconds` 归零/跳变 | `bgp_session_down` | 会话重启 |
| `bgp_peer_prefix_received` / `bgp_peer_prefix_sent` 抖动 | `bgp_route_flap` | 前缀数震荡 |
| `ipv6_route_change_total` 突增 | `bgp_route_flap` | 路由变化率 |
| `ospf6_neighbor_state_code` 变化 | `ospf6_neighbor_down` | 邻居状态机 |
| `ospf6_interface_cost` 变化 | `ospf6_cost_anomaly` | 开销被改 |
| `ipv6_route_exists` < 1 | `blackhole` | 路由消失 |
| `ipv6_default_route_info` / `ipv6_default_route_changed_total` | `wrong_default_route` | 默认路由被改 |
| `ipv6_route_nexthop_info` 变化 | `wrong_static_route` | 下一跳被改 |
| `bgp_command_success` → 0 | 路由守护进程异常 | 采集面证据 |
| `ospf6_interface_enabled`、`bgp_peer_count`、`ipv6_route_count` | 辅助/校验 | 数量级校验 |

```text
输入  routing_metrics_*.csv（长表 metric_name/label/value，1 分钟）
输出  routing_evidence.json
        per (node, metric_name, label): {baseline, incident, delta, changed_at}

新增信息
  D5：taxonomy 现在只做 has("ipv6_route_nexthop_info")（字段存不存在）
  新增的：每个路由指标的「基线值 → 事件值 → 变化时刻」
          → 28 种故障里 7 种 routing 类故障的直接、可命名证据

验收
  **15 个 metric_name 全部处理**（报告里列出清单）；
  正常时段 delta ≈ 0；路由类 incident 时 delta 显著
```

**注意**：这是给 LLM 的**证据**，不是直接输出答案的规则——
赛题禁止「纯规则/脚本推理替代模型」，所以必须是「证据 → LLM 判断」，
不能让脚本自己填 `fault_category`。

### 5.3 `quality_evidence.py`（表 4）—— 【v2 新增】

```text
输入  scrape_health（262,080 行 = node_exporter 9 网元 + routing_exporter 4 路由器）
输出  quality_evidence.json
        {"interrupt_windows": [...], "sample_drop_points": [...], "zero_up_points": [...]}

新增信息
  现有的：完全没有使用
  新增的：① 判别器——别的表缺数据到底是节点真挂了还是采集失败
          ② 稀有强信号——全区只有 180 个 scrape_up=0 采样点（0.07%），
             正对上故障注入窗口；routing_exporter 抓取失败往往意味路由器管理面异常
          ③ scrape_samples 骤降 = 该节点指标系列变少 = 可能有接口/进程消失

验收
  必须报出 scrape_up=0 的总数（期望 180/区域）；能定位到 node 与时刻
```

### 5.4 `log_evidence.py`（表 5）

```text
输入  frr_syslog_events（xian 91 / wuhan 817 / 其余 310~566，全区约 3,481 行）
输出  log_evidence.json
        per event: {template, node, time, severity, program, semantic_class}
        模板化用 Drain / 简化版正则归并（不引重依赖）

新增信息
  D3：现在压成 23 个关键词计数喂无监督模型，排序阶段看不到
  新增的：模板 + 节点 + 精确时刻 + 语义类别（timeout/refused/error/unavailable/flap...），
          作为路由故障的**机制级点缀证据**

验收
  该区域**全部**事件进证据（不得抽样）；至少输出模板数、program 分布
```

**它只有 ~3,500 行，做不了主干**，只能作高置信度点缀：
「这个节点 10:26:57 报了 ospf6d 错误，而它的 `ospf6_neighbor_state_code` 在 10:27 变了」。

### 5.5 `flow_evidence.py`（表 6 + 表 7）—— Trace 的替代品

**表 7 `traffic_flow_metrics`（主动探针）**

实测结构（xian）：源恒为 `fd00:3:40::10`（本区域 traffic-vm），目标只指向三个核心区：

```text
web       → web01.shanghai.aiops.local      18,706 条
dns       → dns.beida.aiops.local           19,960 条
auth      → auth01.wuhan.aiops.local        13,264 条
elephant  → 8 个区域的 elephant0X.*            744 条
```

```text
输出 per edge:
  {source_region, source_ip, target_region, target_domain, flow_type, protocol,
   requests_delta, error_rate_delta, timeout_delta, duration_p95_delta, first_error_time}

新增信息
  **有向的跨区域调用边 + 服务级症状**（这就是原 spec 想要的 trace 证据，来源是探针）
  **跨区域传播证据**：xian 的探针测的是 shanghai/wuhan/beida 的服务，
  所以要判断 xian 自己的服务好不好，得看**其他 7 个区域**的探针数据
  → 8 个区域合起来是一张跨区域故障传播网

验收
  边必须有方向；同一对 (source,target) 的 baseline 不得取自 incident 窗口内；
  **elephant 与大流单独处理**，不与 web/dns/auth 混算
```

**表 6 `netflow_5tuple`（真实转发，35.6M 行/区域）**

```text
输出 per edge:
  {src_addr, dst_addr, protocol, node, interface_id,
   traffic_curve_1min, first_seen, last_seen, change_rate}

新增信息
  实际的转发关系（谁跟谁真的在通信）+ 流量起止精确定时刻
  → 传播证据；first_seen/last_seen 比 1 分钟桶更细，是 F6 边界精化的来源

验收
  **必须限量读取**（`--netflow-max-rows` 可配，默认给一个保守值），
  报告里必须写 `truncated: true/false`；禁止全量 pandas load
```

### 5.6 `temporal_evidence.py`（表 1~7 的汇总）

```text
输出  temporal_evidence.json
        per node: {first_anomaly_time, rank, gap_to_second}

新增信息
  D6：propagation.py 已算 `temporal_precedence_score`，但只进 RCA-GNN
  新增的：把「谁先异常」显式变成排序与提示词的输入，且用 1 分钟精度

验收
  first_anomaly_time 必须来自 1 分钟原始点，不得量化
```

### 5.7 `predictive_evidence.py`

```text
对候选边 A→B：B_t ≈ f(A_{t-k:t})，滞后互相关 + 线性回归残差
输出 normal_predictability / expected_change / actual_change / excess_change

新增信息
  现有的：节点各自异常多少
  新增的：B 的变化有多少是 A 能解释的 → 区分 root cause 与 victim

验收
  必须在**正常时段**拟合、在 incident 时段预测；
  正常时段可预测性 < 0.3 的边直接丢弃，不要硬算
```

**不要上 PCMCI / LiNGAM / RCD**：网元只有 80 个、单区域 10 个，
节点关系是静态拓扑而非学习出的因果图。用滞后互相关 + 残差即可，理由写进文档。

### 5.8 `contradiction.py`

```json
{
  "candidate": "xian-br-1",
  "supporting":   ["ospf6_neighbor_state_code 最早变化", "下挂接口 rx_drop_rate 上升 8.4x"],
  "contradicting": ["node_metrics 无显著变化", "上游 shanghai-cr-1 更早变化"],
  "net_evidence": 0.42
}
```

**`contradicting` 为空必须写明"未发现反驳证据"，不得留空数组。**

### 5.9 候选生成与 `RootScore`

```text
Candidate Generator（程序）→ Top10 → Evidence Filter → Top5 → LLM → 最终排名
```

**LLM 不得自由创造候选**，只能重排给定候选。子分数必须全部落盘：

```json
{"node": "xian-br-1",
 "local_anomaly": 0.81, "temporal_priority": 0.93,
 "outgoing_propagation": 0.91, "incoming_propagation": 0.12,
 "cross_modal_support": 0.88, "predictive_explanation": 0.91,
 "contradiction": 0.08, "root_score": 0.89}
```

权重来源只能是：**理论合理性 → 本地无标签一致性指标 → 公开 benchmark**。
禁止用官方分数搜权重。默认起点（可调，须在文档写明理由）：

```yaml
root_score:
  local_anomaly: 0.20
  temporal_priority: 0.25
  outgoing_propagation: 0.20
  cross_modal_support: 0.20
  predictive_explanation: 0.15
  incoming_propagation: -0.15
  contradiction: -0.15
```

---

## 第 6 节：F6 —— 1 分钟粒度与区间边界（**先做**）

见 0.4。这一节是 F6 的验收清单：

```text
1. bin_minutes 5 → 1
2. episode_min_duration_minutes 5 → 1 ；episode_max_gap_minutes 5 → 1
3. 输出区间 = [估计起点, 估计终点]，1 分钟精度，时长夹在 [1, 30] 分钟
4. 起止都用「前一个正常点 / 恢复到基线的点」，不套固定窗
5. 与已测 26.8687 的对照**只改时间字段与粒度**，
   root_cause_top5 与 fault_category 一字不动 → 保持单变量对照

预期：AD 15.20 → 19~21（S_AD 从地板 0.70 抬到 0.85+）
```

**注意**：F6 用 1 分钟粒度 → 它必须自跑自己的第一阶段，
**不能拿 C 的 5 分钟 artifacts**（纪律 1.5-7）。

---

## 第 7 节：RAG 的位置（收紧）

**结论：RAG 不进主链，只作可选证据，优先级最低。**

1. 原 spec 列的 **RCAEval / MicroRank / BARO / CIRCA 都是微服务 + trace 数据集**，
   与本项目的**网络拓扑 + FRR/BGP/OSPF** 不是同一类问题，迁移价值低。
2. 本项目真正需要的外部知识是「哪种故障会让哪个指标怎么动」——
   **这已经从 `routing_metrics` 的 15 个指标名里直接读出来了**（5.2 节），不需要联网检索。
3. 按 1.3 的损失预算，RAG 打不到任何一个已知的大额失血点。

若仍要做：

```text
允许：通用故障模式 / 通用 RCA 方法 / 不同 case 的参考案例 / 公开论文
禁止：用目标 case 的特征搜索并命中原 case 的 ground truth
      （发现知识库文档与目标 case 高度重合 → discard）
检索：BM25 + Embedding + metadata filtering
查询必须综合 symptoms + metric + log + flow + topology；禁止只用节点名
输出只能是 {"pattern","source","relevance"}，禁止 {"root_cause": "..."}
```

---

## 第 8 节：明确不做的事

```text
✗ 任何 GNN/GAT/GraphSAGE/HGT/TGN/Graph-Transformer → anomaly score → LLM 的换模型方案
✗ trace_id / span_id / parent_span_id（数据里没有）
✗ 用官方黑盒分数做 Optuna / 贝叶斯 / 网格 / 随机 / 进化 / 自动 Prompt / 自动权重搜索
✗ 为了"用了 RAG / Agent / GNN"而引入它们
✗ 大规模重构既有 baseline 与 A~E
✗ 把整个数据集塞进一个超长 Prompt
✗ 让 LLM 自由创造候选根因
✗ 用「字段存在」代替「字段变化」作为证据（D4/D5 的根因）
✗ 输出 1 分钟长度的预测区间（Dice 会掉到 0.18，必然匹配不上）
✗ 让任何一张表 rows_read == 0（必须报错，不许静默跳过）
```

---

## 第 9 节：代码结构与运行方式

```text
aiops_diagnosis/
├── aiops/
│   ├── config.py                    ← 改：bin_minutes / episode_* 默认值（F 专用 profile）
│   └── evidence/                    ← 新增
│       ├── __init__.py
│       ├── metric_evidence.py       ← 表 1 + 表 2
│       ├── routing_evidence.py      ← 表 3
│       ├── quality_evidence.py      ← 表 4   【v2 新增】
│       ├── log_evidence.py          ← 表 5
│       ├── flow_evidence.py         ← 表 6 + 表 7
│       ├── temporal_evidence.py
│       ├── predictive_evidence.py
│       ├── contradiction.py
│       ├── candidate_generator.py
│       ├── root_score.py
│       ├── evidence_graph.py
│       └── table_usage.py           ← 覆盖率自检，产出 table_usage_report.json
├── run_experiment_f_evidence_agent.py      ← 新增，独立入口
├── outputs_experiment_f_*/                 ← 新增产物目录
└── knowledge/                              ← 可选 RAG（默认关闭）
```

**不要为了 F 去改 `aiops/config.py` 的全局默认值**（会污染 A~E）。
F 用自己的 dataclass 覆盖，或命令行传参。

```bash
# 开发期：单区域、1 分钟、全模块
python run_experiment_f_evidence_agent.py \
    --workspace /202131510121/lyt/workspace \
    --regions xian \
    --bin-minutes 1 \
    --output-dir outputs_experiment_f_dev \
    --llm-base-url http://127.0.0.1:8000 --llm-model deepseek-r1-14b \
    --threshold 0.5 --workers 8

# 正式：全域、1 分钟、F 自跑第一阶段
python run_experiment_f_evidence_agent.py \
    --workspace /202131510121/lyt/workspace \
    --regions all \
    --bin-minutes 1 \
    --output-dir outputs_experiment_f_full \
    --llm-base-url http://127.0.0.1:8000 --llm-model deepseek-r1-14b \
    --threshold 0.5 --workers 8 \
    --disable-rag --disable-predictive --disable-contradiction

# 加速档（产物目录必须带 _coarse 后缀，文档必须写明是近似）
python ... --coarse-pass-minutes 5 --output-dir outputs_experiment_f_full_coarse
```

---

## 第 10 节：交付时必须汇报

1. **改了哪些文件**（逐文件）
2. **每个模块的输入 / 输出**（哪张表 → 哪个 json）
3. **完整运行命令**
4. **如何关闭新增模块**（`--disable-*`）
5. **最终 JSONL 的真实示例**（必须真跑出来的，不是手编）
6. **与 baseline 的区别**：F 为什么**不是** `IF/VAE + dependency + LLM`，
   也**不是** `IF/VAE + GNN + LLM`
7. **新增信息清单**（Temporal / Routing / Flow / Quality / Predictive / Directional /
   Contradiction / Cross-modal / External Knowledge），逐条写清"原系统没有、F 新产生"的信息
8. **`table_usage_report.json`**：七张表逐张的 `rows_read` / 时间覆盖 / 用到字段；
   **任何一张为 0 必须解释原因或修复**
9. **1 分钟粒度的实际耗时**：base 阶段实测小时数，以及是否用了 `--coarse-pass-minutes`

**某模块若答不出新增信息，直接删掉，不要留。**

---

## 第 11 节：执行顺序（当前资源约束）

```text
当前状态（2026-09-21 15:00 UTC）
  D 的 LLM 合并 14:50 完成；E 的合并正在跑（输入 4140 条，约 16:15 完成）
  → 这段时间 vLLM 被占用，F 的 LLM 阶段不得抢；但 1 分钟 base 阶段也是 CPU 重活，
    建议等 E 完成后再开，避免和合并抢 CPU

顺序
  Step 1  F6 的配置改动 + 单区域 1 分钟 base（xian）→ 先确认能跑完、耗时多少
  Step 2  F1 度量框架 + 5.1/5.2/5.3/5.4 四个证据模块（纯 CPU）
  Step 3  跑 4.2 提示词消融（单区域，约 10 分钟）→ 拿到"玄学程度"结论
  Step 4  5.5 flow/netflow 证据 + 5.6/5.7 时序与预测证据
  Step 5  F8 全域 1 分钟（预计 8~25h，需过夜）
  Step 6  用剩余额度做一次关键对照提交（需用户明确同意）
```

**每一步都要产出可单独检验的产物文件；不要等到最后才第一次运行。**
