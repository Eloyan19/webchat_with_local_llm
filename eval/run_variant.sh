#!/usr/bin/env bash
# Прогон одного варианта оптимизации на dev-подмножестве.
# usage: run_variant.sh LABEL SUBSET [env assignments...]
#   run_variant.sh v1_copyfirst dev PROMPT_VARIANT=copyfirst
#   run_variant.sh local-optimized full PROMPT_VARIANT=copyfirst OLLAMA_MODEL=qwen2.5-coder:3b
# Поднимает backend на :8011 с заданным env, ждёт health, гоняет optimize_eval,
# гасит сервер. Всё в одной команде (self-contained).
set -u
LABEL="$1"; SUBSET="$2"; shift 2
BACK=/root/repos/webchat_with_local_llm/backend
EVAL=/root/repos/webchat_with_local_llm/eval

# гасим прежний :8011, если висит
pkill -f "uvicorn main:app .* --port 8011" 2>/dev/null; sleep 1

echo ">>> [$LABEL] старт backend :8011 с env: $*"
cd "$BACK"
env "$@" .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8011 \
    >/tmp/claude-0/-root-repos-webchat/9c9add39-4b6c-443b-a213-6a29a0dacc90/scratchpad/var_${LABEL}.log 2>&1 &
SRV=$!

# ждём готовности (warm-start грузит модель ~30-60с, особенно смена модели)
ok=0
for i in $(seq 1 90); do
  if curl -sf --max-time 3 http://127.0.0.1:8011/health >/dev/null 2>&1; then ok=1; echo ">>> health ok (~$((i*2))s)"; break; fi
  sleep 2
done
if [ "$ok" != 1 ]; then echo ">>> [$LABEL] backend НЕ поднялся, лог:"; tail -5 /tmp/claude-0/-root-repos-webchat/9c9add39-4b6c-443b-a213-6a29a0dacc90/scratchpad/var_${LABEL}.log; kill $SRV 2>/dev/null; exit 1; fi

cd "$EVAL"
python3 -u optimize_eval.py run --url http://127.0.0.1:8011 --label "$LABEL" --subset "$SUBSET"
echo ">>> [$LABEL] готово, гашу backend"
kill $SRV 2>/dev/null
