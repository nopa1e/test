# AIOps 实验 A / B / C / D / E 说明

本文介绍五个实验的目的、作用、当前实现方式和已有结果。

---

## 总体目标

五个实验都围绕同一个问题：

```text
如何把点级异常检测，提升为事件级/故障级根因诊断，
并在 LLM 阶段完成异常验证、根因排序和故障分类。
```

统一输出：

```text
predictions_high_conf.jsonl
predictions_low_conf.jsonl
llm_anomaly_scores.jsonl
```

统一高/低置信规则：

```text
anomaly_probability >= 0.5
  -> predictions_high_conf.jsonl

anomaly_probability < 0.5
  -> predictions_low_conf.jsonl
```

默认只提交高置信文件。  
除非用户明确声明，否则低置信文件不提交，以节省评测次数。

---

## 实验 A：算法伪标签 + incident-level LLM

### 目的

验证一个基础框架：

```text
第一阶段无监督异常检测
  ↓
算法伪标签
  ↓
GNN / RCA-GNN
  ↓
incident-level LLM 验证
```

### 作用

- 作为后续 B/C/D/E 的对比基础
- 验证最终 LLM 阶段是否有效
- 验证 incident-level 输出是否比 dataset-level 更合理

### 当前实现

```text
run_experiment_a_incident_llm.py
```

### 已有结果

高置信：

```text
总分: 27.31627817492457
AD:   15.302579544787587
RCA:   9.753424657534245
Major: 1.8835616438356164
Minor: 0.37671232876712324
```

低置信：

```text
总分: 1.4676369863013696
AD:   0.9128424657534246
RCA:  0.5205479452054794
Major: 0.03424657534246575
Minor: 0.0
```

---

## 实验 B：LLM Teacher + RCA GNN Student + Task A/B

### 目的

验证完整的 LLM 蒸馏和事件级 LLM 流程：

```text
LLM Teacher
  ↓
RCA GNN Student
  ↓
Task A incident correlation
  ↓
Task B RCA verification
  ↓
incident-level high/low prediction
```

### 作用

- 测试 LLM Teacher 是否能提升 GNN Student
- 测试 Task A merge/split 是否能处理长时间故障
- 测试 Task B 是否能提升根因 Top5 和故障分类

### 当前实现

```text
run_ablation.py --experiments full \
  --llm-backend openai_compatible \
  --llm-model deepseek-r1-14b
```

内部涉及：

```text
aiops/llm_teacher.py
aiops/rca_gnn.py
aiops/llm_tasks.py
aiops/pipeline_ext.py
```

### 已有结果

历史 dataset-level 提交：

```text
总分: 0.3260273972602739
AD:   0.3260273972602739
RCA:  0.0
Major: 0.0
Minor: 0.0
```

注意：

```text
该分数是 dataset-level 结果，不是正式 incident-level 结果。
B 的 incident-level 版本目前应在自跑自用链中重新生成。
```

---

## 实验 C：去掉 GNN，直接将依赖关系交给 LLM

### 目的

验证：

```text
如果不训练 GNN，
直接把无监督异常结果和拓扑依赖关系交给 LLM，
LLM 能否完成依赖关系推理和根因判断。
```

### 作用

- 作为 GNN 消融实验
- 判断 GNN 是否真的比显式依赖关系更有效
- 提高可解释性：上下游、时间先后、传播链直接可见

### 正确流程

```text
C 自己的 IF + VAE
  ↓
C 自己的 Episode
  ↓
C 自己的 Incident Candidate
  ↓
去掉 GNN / RCA-GNN
  ↓
无监督异常结果 + 拓扑依赖关系
  ↓
LLM 检查上下游、时间先后、传播链
  ↓
根因 Top5 + 故障类别 + anomaly_probability
```

### 当前实现

```text
run_experiment_c_dependency_llm.py
```

注意：

```text
C 必须跑自己的 IF/VAE 和 incident。
不能使用 B 的 artifacts。
```

### 已有结果

曾有一个非严格版本使用 B 的 artifacts：

```text
C high:
  总分: 27.82337526560874
  AD:   15.960361566978602
  RCA:   9.808219178082192
  Major: 1.8835616438356164
  Minor: 0.17123287671232876

C low:
  总分: 0.3646326276463262
  AD:   0.2824408468244084
  RCA:  0.0821917808219178
  Major: 0.0
  Minor: 0.0
```

说明：

```text
该结果使用了 B 的 incident 集合，
不能作为严格实验 C 的最终结果。
严格版 C 需要使用 C 自己的 IF/VAE 和 incident。
```

---

## 实验 D：第二阶段无监督模型，异常样本降权

### 目的

进一步提纯模型对正常样本形态的学习能力。

核心思想：

```text
第一阶段已经得到异常分数。
第二阶段再训练一个无监督模型，
但是异常样本权重更小，
让模型更多学习正常样本。
```

### 权重设计

```text
weight = max(0, 1 - alpha * first_anomaly_score)
```

例如：

```text
first_anomaly_score = 0.0 -> weight = 1.0
first_anomaly_score = 0.5 -> weight = 0.5
first_anomaly_score = 1.0 -> weight = 0.0
```

### 正确流程

```text
D 自己的 IF + VAE
  ↓
D 自己的 Episode / Incident
  ↓
第二阶段无监督 VAE
  - 异常样本降权
  ↓
第二阶段 anomaly score
  ↓
与 A/C 相同的 incident-level LLM 验证
  ↓
高/低置信输出
```

### 当前实现

```text
run_experiment_d_e_second_stage.py --mode weighted
run_experiment_d_e_llm.py
```

### 当前结果

```text
待自跑自用完整版运行后补充。
旧版 D/E 曾使用 B 的 artifacts，按规则不作为正式结果。
```

---

## 实验 E：第二阶段无监督模型，异常样本直接剔除

### 目的

与 D 类似，但采用硬过滤：

```text
first_anomaly_score > threshold
  -> 样本 weight = 0
  -> 完全不参与第二阶段训练
```

只让正常样本参与第二个无监督模型训练，学习更干净的正常形态。

### 正确流程

```text
E 自己的 IF + VAE
  ↓
E 自己的 Episode / Incident
  ↓
第二阶段无监督 VAE
  - 超过阈值的异常样本直接剔除
  ↓
第二阶段 anomaly score
  ↓
与 A/C 相同的 incident-level LLM 验证
  ↓
高/低置信输出
```

### 当前实现

```text
run_experiment_d_e_second_stage.py --mode threshold
run_experiment_d_e_llm.py
```

当前阈值：

```text
threshold = 0.7
```

### 当前结果

```text
待自跑自用完整版运行后补充。
旧版 D/E 曾使用 B 的 artifacts，按规则不作为正式结果。
```

---

## 结果对比

| 实验 | 是否使用 GNN | 第二阶段无监督 | 当前分数 | 说明 |
|---|---|---|---|---|
| A | 是 | 无 | high 27.3163 / low 1.4676 | 基础版本 |
| B | 是 + Teacher | 无 | dataset-level 0.3260 | incident-level 待重跑 |
| C | 否 | 无 | high 27.8234 / low 0.3646 | 非严格版本，使用了 B artifacts |
| D | 是 | 异常降权 | 待跑 | 自跑自用 |
| E | 是 | 异常剔除 | 待跑 | 自跑自用 |

---

## 当前规则

```text
1. 所有实验必须运行自己的输入和输出。
2. 不能复用其他实验的 artifacts。
3. 高/低置信分流阈值当前为 0.5。
4. 低置信文件默认不提交。
5. 每次提交必须是独立实验、独立结果。
6. LLM 模型权重不进入 GitHub。
```
