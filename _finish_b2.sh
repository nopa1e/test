#!/bin/bash
cd /202531630503/lyt/aiops_diagnosis
LOG=f_logs/finish_b2.log
echo "[$(date -u +%H:%M:%S)] 等待第二批 8 区域完成" > $LOG
for i in $(seq 1 120); do
  n=$(ls llm_rootcause_out/*b2full.json 2>/dev/null | wc -l)
  if [ "$n" -ge 8 ]; then echo "[$(date -u +%H:%M:%S)] 8/8 完成" >> $LOG; break; fi
  sleep 30
done
echo "[$(date -u +%H:%M:%S)] 开始集成 LLM 第二批（净增量，只写回不一致处）" >> $LOG
python3 f_llm_integrate.py \
  --base submissions/submit_ainj90.jsonl \
  --llm-glob "llm_rootcause_out/*_b2full.json" \
  --out submissions/submit_ainj90_llm2.jsonl \
  --mode all >> $LOG 2>&1
echo "[$(date -u +%H:%M:%S)] 集成完毕，开始缺口填充（step_factor=2）" >> $LOG
python3 f_fill_timeline.py \
  --base submissions/submit_ainj90_llm2.jsonl \
  --out submissions/submit_combo.jsonl \
  --gap-dice 0.9 --step-factor 2.0 >> $LOG 2>&1
echo "[$(date -u +%H:%M:%S)] COMBO_DONE" >> $LOG
