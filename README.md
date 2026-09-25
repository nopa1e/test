# AIOps 故障诊断流水线

面向 CCF AIOps 故障诊断场景的两阶段实现：

1. **无监督异常检测阶段**
   - 遍历 `workspace/data/*/*/processed/*.csv`；
   - 将 node / interface / routing / scrape / FRR / NetFlow / traffic-flow 指标聚合为
     5 分钟粒度的“点”（网络元素 + 时间窗）；
   - 对指标矩阵做 `IsolationForest`；
   - 按“所有数据都是正常样本”的假设训练 VAE 自编码器，取重构误差；
   - 融合 IF 异常分数、VAE 重构误差，给最正常的一批点打上正常伪标签；
   - 将伪标签置信度、IF 分数、VAE 分数作为点特征；
   - 用 trace 结构（如果存在）或拓扑/NetFlow 结构构建点级图；
   - 训练一个轻量 GCN，对每个点输出 `normal_score`；
   - `normal_score` 升序排序，第一名即最异常点。

2. **LLM + MCP 根因分析阶段**
   - 将异常点、节点排名、特征证据、拓扑边输入 LLM；
   - LLM 每发现一个异常点，可以通过 MCP 工具查询：
     - `get_dependency`：直接父/子依赖；
     - `get_trace_upstream`：反向依赖/trace 上游；
     - `get_trace_downstream`：下游传播链；
     - `get_node_anomaly`：节点正常分和异常点；
     - `get_anomaly_points`：全局最异常点；
     - `get_metric_evidence`：指标级证据；
     - `get_frr_events`：FRR Syslog 事件；
   - 如果子节点异常大，但其父服务/上游节点异常更强，根因优先归因到上游；
   - 最终输出严格合法 JSON，`fault_category` 必须落在官方封闭枚举中。

## 目录结构

```text
aiops_diagnosis/
├─ aiops/
│  ├─ config.py          # 配置
│  ├─ dataset.py         # 数据集发现与 CSV 读取
│  ├─ features.py        # 指标聚合 / 点特征矩阵
│  ├─ anomaly.py         # IsolationForest + VAE
│  ├─ graph.py           # 实体图、点级图、trace 图
│  ├─ gnn.py             # 轻量 GCN 正常分学习
│  ├─ pipeline.py        # 主流水线
│  ├─ taxonomy.py        # 官方故障类别封闭枚举
│  ├─ mcp_server.py      # MCP 工具服务
│  └─ llm_diagnoser.py   # 第二阶段 LLM 诊断
├─ run_pipeline.py       # 训练并输出预测
├─ run_mcp_server.py     # 启动 MCP stdio 服务
├─ run_llm_diagnosis.py  # 对已生成 artifacts 重新做 LLM 诊断
└─ requirements.txt
```

## 安装

```bash
cd D:\代码查看\dsh\aiops_diagnosis
python -m pip install -r requirements.txt
```

## 运行全量流水线

```bash
python run_pipeline.py ^
  --workspace C:\Users\Cyber\Downloads\workspace ^
  --output-dir outputs ^
  --vae-epochs 40 ^
  --gnn-epochs 120
```

只跑某个区域：

```bash
python run_pipeline.py --workspace C:\Users\Cyber\Downloads\workspace --output-dir outputs --dataset shenyang
```

输出目录：

```text
outputs/
├─ <dataset_name>/
│  ├─ point_scores.csv.gz
│  ├─ node_scores.csv
│  ├─ topology.json
│  ├─ evidence.json
│  ├─ dataset_meta.json
│  ├─ vae_model.pt
│  ├─ gnn_model.pt
│  └─ prediction.json
└─ _all/
   ├─ predictions.jsonl
   └─ run_summary.json
```

`point_scores.csv.gz` 中：

- 按 `final_normal_score` 升序排列；
- 第一行就是最异常点；
- `point_rank=1` 即异常分数最低 / 最异常；
- `top_feature_json` 保存该点最偏离正常分布的顶部特征。

## MCP 服务

MCP 服务默认读取 `outputs/` artifacts：

```bash
python run_mcp_server.py --output-dir outputs --workspace C:\Users\Cyber\Downloads\workspace
```

它会以 stdio transport 暴露以下工具：

```text
list_datasets
list_network_elements
get_dependency
get_trace_upstream
get_trace_downstream
get_node_anomaly
get_anomaly_points
get_metric_evidence
get_frr_events
```

## 第二阶段 LLM 诊断

支持 Ollama：

```bash
python run_llm_diagnosis.py ^
  --workspace C:\Users\Cyber\Downloads\workspace ^
  --output-dir outputs ^
  --dataset shenyang_20260819040000_20260902040000 ^
  --llm-backend ollama ^
  --llm-model qwen2.5:7b
```

也支持 OpenAI 兼容接口：

```bash
python run_pipeline.py ^
  --workspace C:\Users\Cyber\Downloads\workspace ^
  --output-dir outputs ^
  --llm-backend openai_compatible ^
  --llm-model Qwen/Qwen3-8B ^
  --llm-base-url http://localhost:8000
```

最终 `prediction.json` 严格为：

```json
{
  "prediction_id": "pred_000001",
  "start_time": "2026-08-05T10:15:20.000+00:00",
  "end_time": "2026-08-05T10:18:45.000+00:00",
  "root_cause_top5": [
    {"rank": 1, "network_element_id": "shenyang-br-1"}
  ],
  "fault_category": {
    "major_category": "link",
    "sub_category": "delay"
  }
}
```

## 关于 trace 数据

当前 workspace 的 `processed` 中没有 `*trace*.csv` 时，流水线自动回退为：

- 实体拓扑图 + 时间边 + NetFlow 观测边；
- 每个点仍然是“网络元素 + 5 分钟窗口”；
- MCP 的 `get_trace_upstream` 会沿反向拓扑边寻找父节点/上游节点。

如果后续提供 trace/span CSV，只要包含类似列：

```text
span_id, parent_span_id, service_name, start_time, duration, ...
```

`graph.build_trace_point_table()` 就会把 span 作为点、parent-child 作为边，
后续 GNN 和 MCP 上游追踪逻辑不变。

## 分类闭集合

`aiops/taxonomy.py` 严格实现官方枚举：

```text
link: delay | rate_limit | loss
firewall: acl_drop | rate_limit | port_block | cpu_pressure | default_route_error | rule_order_error
resource: cpu_pressure | memory_pressure | disk_io_pressure | disk_space_low | process_pressure | softirq_pressure
routing: blackhole | bgp_session_down | bgp_route_flap | wrong_static_route | ospf6_neighbor_down | ospf6_cost_anomaly | wrong_default_route
service: dns_down | dns_wrong_record | web_5xx | web_slow | auth_timeout | auth_error
```

`pipeline.py` 和 `llm_diagnoser.py` 在输出前都会执行
`is_valid_category(major, sub)` 校验；LLM 返回非法类别时会回退到
规则分类器，并在 `root_cause_top5` 中重新用合法 `network_element_id` 填充。
