# 实验 A 代码架构图

## 1. 总览

```mermaid
flowchart TD
    subgraph INPUT[输入 artifacts]
        I1[incident_candidates.json]
        I2[rca_scores.csv]
        I3[evidence.json]
        I4[workspace / dataset]
    end

    subgraph RUNNER[run_experiment_a_incident_llm.py]
        R1[discover_datasets]
        R2[加载 incidents / evidence / rca_scores]
        R3[merge_adjacent_incidents]
        R4[构造 fallback_nodes]
        R5[LLMBackend openai_compatible]
        R6[generate_incident_predictions]
        R7[save_routed]
        R8[合并全局 high/low/meta]
    end

    subgraph CORE[共享模块]
        C1[aiops/incident_predictions.py]
        C2[aiops/llm_diagnoser.py]
        C3[aiops/taxonomy.py]
        C4[aiops/dataset.py]
        C5[aiops/config.py]
    end

    subgraph VLLM[14B 推理服务]
        V1[vLLM OpenAI server :8000]
        V2[deepseek-r1-14b]
    end

    subgraph OUTPUT[最终输出]
        O1[predictions_high_conf.jsonl]
        O2[predictions_low_conf.jsonl]
        O3[llm_anomaly_scores.jsonl]
        O4[summary.json]
    end

    I1 --> R2
    I2 --> R2
    I3 --> R2
    I4 --> R1

    R1 --> R2
    R2 --> R3
    R3 --> R6
    R2 --> R4
    R4 --> R6
    R5 --> R6
    R6 --> C1
    C1 --> C2
    C1 --> C3
    C1 --> C4
    R5 --> C5
    C2 --> V1
    V1 --> V2
    R6 --> R7
    R7 --> O1
    R7 --> O2
    R7 --> O3
    R8 --> O4
```

## 2. 单个 incident 的处理流程

```mermaid
flowchart TD
    S1[incident candidate]
    S2[merge_adjacent_incidents]
    S3[_candidate_order]
    S4[build_incident_prompt]
    S5[LLMBackend.chat]
    S6[14B vLLM generations]
    S7[_parse_json_object]
    S8[_normalize_result]
    S9{anomaly_probability >= 0.5 ?}
    S10[predictions_high_conf.jsonl]
    S11[predictions_low_conf.jsonl]
    S12[llm_anomaly_scores.jsonl]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> S7 --> S8 --> S9
    S9 -- 是 --> S10
    S9 -- 否 --> S11
    S8 --> S12
```

## 3. 高/低置信分流

```mermaid
flowchart LR
    A[LLM 输出] --> B{is_anomaly 且 anomaly_probability >= 0.5}
    B -- 是 --> C[high_conf]
    B -- 否 --> D[low_conf]
    A --> E[llm_anomaly_scores.jsonl]
```

## 4. 主要文件职责

| 文件 | 职责 |
|---|---|
| `run_experiment_a_incident_llm.py` | 实验 A 入口，遍历 dataset，加载 artifacts，调用 incident LLM |
| `aiops/incident_predictions.py` | 相邻 incident merge、prompt 构造、LLM 解析、高/低置信路由 |
| `aiops/llm_diagnoser.py` | `LLMBackend`，支持 OpenAI-compatible/vLLM |
| `aiops/taxonomy.py` | 官方封闭故障类别枚举 |
| `aiops/dataset.py` | dataset 发现、官方 network_element_id |
| `aiops/config.py` | LLM 配置、阈值、experiment 配置 |

## 5. 关键输入输出

### 输入

```text
outputs_ablation_full/_ablation/full/<dataset>/
  incident_candidates.json
  rca_scores.csv
  evidence.json
```

### 输出

```text
outputs_experiment_a_incident_llm/
  predictions_high_conf.jsonl
  predictions_low_conf.jsonl
  llm_anomaly_scores.jsonl
  summary.json
```
