#!/usr/bin/env python3
"""A/B-харнесс оптимизации локальной LLM под grounded code-citation.

Гоняет вопросы против backend-варианта (свой URL/env-конфиг), пишет метрики в
opt_runs.jsonl построчно (резюмируемо по ключу label|qid|rep). Отдельный от
compare_local_vs_cloud.py — тут сравниваем НЕ системы, а КОНФИГИ одной локальной
системы (до/после оптимизации).

Режимы:
  run     --url URL --label NAME [--subset dev|full] [--reps N]
  summary                      — таблица метрик по всем label в opt_runs.jsonl

Dev-подмножество = 4 in-domain, что абстейнили в baseline (q1,2,3,8) + 2 off-topic
(q13,14, регресс-страж: должны остаться abstained). Судью тут не зовём — на dev
достаточно abstained / valid-sources / quotesDropped / latency; faithfulness
финалистов оценивает compare_local_vs_cloud.py на полном прогоне.
"""
import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
RUNS = HERE / "opt_runs.jsonl"
JUDG = HERE / "opt_judgements.jsonl"
DEV_INDOMAIN = {1, 2, 3, 8}   # baseline-абстейны — цель: заставить ответить
DEV_OFFTOPIC = {13, 14}       # регресс-страж: обязаны остаться abstained

# Слепой судья faithfulness — облачный DeepSeek через webchat no-RAG (:8000), тот
# же арбитр, что судил baseline (сравнимость до/после). Судью не видно, чей ответ.
CLOUD_URL = "http://127.0.0.1:8000"
OPT_LABEL = "local-optimized"   # метка полного прогона победителя


def ask(url: str, messages: list[dict], temperature: float) -> tuple[dict, float]:
    body = json.dumps({
        "messages": messages, "useRag": True, "improvedRag": True,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        f"{url}/chat", data=body, headers={"Content-Type": "application/json"}
    )
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=320) as resp:
        data = json.load(resp)
    return data, time.perf_counter() - t


def done_keys() -> set[str]:
    if not RUNS.exists():
        return set()
    return {json.loads(l)["key"] for l in RUNS.open() if l.strip()}


def run(url: str, label: str, subset: str, reps: int) -> None:
    questions = json.loads((HERE / "questions.json").read_text())
    if subset == "dev":
        questions = [q for q in questions if q["id"] in DEV_INDOMAIN | DEV_OFFTOPIC]
    seen = done_keys()
    for q in questions:
        offtopic = bool(q.get("offtopic"))
        for rep in range(reps):
            key = f"{label}|{q['id']}|{rep}"
            if key in seen:
                continue
            messages = q.get("context", []) + [{"role": "user", "content": q["question"]}]
            try:
                d, dt = ask(url, messages, 0.0)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                print(f"[{label} q{q['id']} r{rep}] ОШИБКА: {e}")
                continue
            meta = d.get("ragMeta", {})
            row = {
                "key": key, "label": label, "qid": q["id"], "rep": rep,
                "type": "offtopic" if offtopic else "indomain",
                "question": q["question"],
                "expected_sources": q.get("expected_sources", []),
                "latency_s": round(dt, 1),
                "abstained": bool(meta.get("abstained")),
                "n_sources": len(d.get("sources", [])),
                "quotesDropped": meta.get("quotesDropped", 0),
                "jsonParseFailed": bool(meta.get("jsonParseFailed")),
                "reply": d.get("reply", ""),
                "sources": [{"file": s["file"], "quote": s.get("quote")}
                            for s in d.get("sources", [])],
            }
            with RUNS.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tag = "off" if offtopic else "in "
            print(f"[{label} {tag} q{q['id']} r{rep}] {dt:5.0f}s "
                  f"abstain={row['abstained']} src={row['n_sources']} "
                  f"qDrop={row['quotesDropped']} jsonFail={row['jsonParseFailed']}")


def summary() -> None:
    if not RUNS.exists():
        print("нет opt_runs.jsonl")
        return
    rows = [json.loads(l) for l in RUNS.open() if l.strip()]
    labels = sorted({r["label"] for r in rows}, key=lambda x: [r["label"] for r in rows].index(x))
    print(f"\n{'label':<18} {'in-ans':>7} {'in-abs':>7} {'qDrop':>6} {'jsonF':>6} "
          f"{'off-abs':>8} {'lat_med':>8} {'lat_max':>8}")
    print("-" * 74)
    for lab in labels:
        rl = [r for r in rows if r["label"] == lab]
        ind = [r for r in rl if r["type"] == "indomain"]
        off = [r for r in rl if r["type"] == "offtopic"]
        ans = sum(1 for r in ind if not r["abstained"] and r["n_sources"] > 0)
        abs_ = sum(1 for r in ind if r["abstained"])
        qdrop = statistics.mean([r["quotesDropped"] for r in ind]) if ind else 0
        jf = sum(1 for r in ind if r["jsonParseFailed"])
        offabs = f"{sum(1 for r in off if r['abstained'])}/{len(off)}"
        lats = [r["latency_s"] for r in ind]
        lmed = statistics.median(lats) if lats else 0
        lmax = max(lats) if lats else 0
        print(f"{lab:<18} {ans:>7} {abs_:>7} {qdrop:>6.2f} {jf:>6} "
              f"{offabs:>8} {lmed:>7.0f}s {lmax:>7.0f}s")
    print("\nin-ans = отвечено in-domain (с источником) | in-abs = абстейнов | "
          "qDrop = ср. quotesDropped | off-abs = off-topic абстейнов (должно N/N)")


def judge_answer(question: str, answer: str, quotes: list[str]) -> tuple[bool | None, str]:
    """Слепой судья faithfulness: следует ли ответ из цитат? Через облачный
    DeepSeek (:8000, no-RAG), не зная, чья система. Возвращает (grounded|None, reason)."""
    if not quotes:
        return None, "нет цитат"
    quoted = "\n".join(f"[{i}] {q}" for i, q in enumerate(quotes, 1))
    prompt = (
        "Ты — строгий проверяющий фактической опоры ответа на цитаты. Дан вопрос, "
        "ответ ассистента и цитаты из источников. Реши, следует ли фактическое "
        "содержание ответа из этих цитат (подтверждается ими, не выдумано). Язык ответа "
        "и цитат может отличаться — оценивай смысл.\n"
        'Ответь СТРОГО одним JSON: {"grounded": true|false, "reason": "<кратко>"}.\n\n'
        f"Вопрос: {question}\n\nОтвет: {answer}\n\nЦитаты:\n{quoted}"
    )
    body = json.dumps({"messages": [{"role": "user", "content": prompt}],
                       "useRag": False, "improvedRag": False, "temperature": 0}).encode()
    req = urllib.request.Request(f"{CLOUD_URL}/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            reply = json.load(resp)["reply"]
        m = re.search(r"\{.*\}", reply, re.S)
        data = json.loads(m.group(0)) if m else {}
        return bool(data.get("grounded")), str(data.get("reason", "")).strip()
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError, TimeoutError) as e:
        return None, f"ошибка судьи: {e}"


def judge() -> None:
    """Судим отвеченные in-domain строки победителя (label=local-optimized)."""
    done = {json.loads(l)["key"] for l in JUDG.open()} if JUDG.exists() else set()
    rows = [json.loads(l) for l in RUNS.open() if l.strip()]
    n = 0
    for r in rows:
        if r["label"] != OPT_LABEL or r["type"] != "indomain":
            continue
        if r["key"] in done or r["abstained"] or r["n_sources"] == 0:
            continue
        quotes = [s["quote"] for s in r["sources"] if s.get("quote")]
        g, reason = judge_answer(r["question"], r["reply"], quotes)
        with JUDG.open("a") as f:
            f.write(json.dumps({"key": r["key"], "grounded": g, "reason": reason},
                               ensure_ascii=False) + "\n")
        n += 1
        print(f"[judge {r['key']}] grounded={g} — {reason[:60]}")
    print(f"Готово. judged={n}.")


def _metrics(indomain: list[dict], offtopic: list[dict], judg: dict) -> dict:
    """Метрики набора: отвечено/абстейн/grounded%/quotesDropped/jsonFail/латентность."""
    answered = [r for r in indomain if not r["abstained"] and r["n_sources"] > 0]
    judged = [(r, judg.get(r["key"], {}).get("grounded")) for r in answered]
    gt = sum(1 for _, g in judged if g is True)
    gd = sum(1 for _, g in judged if g is not None)
    lats = [r["latency_s"] for r in indomain]
    lats_s = sorted(lats)
    p95 = lats_s[min(len(lats_s) - 1, int(0.95 * len(lats_s)))] if lats_s else 0
    return {
        "n": len(indomain),
        "answered": len(answered),
        "abstained": sum(1 for r in indomain if r["abstained"]),
        "grounded": f"{gt}/{gd}" + (f" ({round(100*gt/gd)}%)" if gd else ""),
        "qdrop": round(statistics.mean([r["quotesDropped"] for r in indomain]), 2) if indomain else 0,
        "jsonfail": sum(1 for r in indomain if r["jsonParseFailed"]),
        "lat_p50": round(statistics.median(lats)) if lats else 0,
        "lat_p95": round(p95),
        "off_abs": f"{sum(1 for r in offtopic if r['abstained'])}/{len(offtopic)}",
    }


def report() -> None:
    """REPORT-optimization.md: baseline (raw_runs local t0 + judgements) vs optimized."""
    raw = [json.loads(l) for l in (HERE / "raw_runs.jsonl").open() if l.strip()]
    base_j = {json.loads(l)["key"]: json.loads(l)
              for l in (HERE / "judgements.jsonl").open() if l.strip()}
    opt = [json.loads(l) for l in RUNS.open() if l.strip() and json.loads(l)["label"] == OPT_LABEL]
    opt_j = ({json.loads(l)["key"]: json.loads(l) for l in JUDG.open() if l.strip()}
             if JUDG.exists() else {})

    # baseline (raw_runs) — иная схема: abstained/quotesDropped внутри ragMeta.
    # Нормализуем к плоскому виду, который ждёт _metrics.
    def norm(r: dict) -> dict:
        m = r.get("ragMeta", {})
        return {**r, "abstained": bool(m.get("abstained")),
                "n_sources": len(r.get("sources", [])),
                "quotesDropped": m.get("quotesDropped", 0),
                "jsonParseFailed": bool(m.get("jsonParseFailed"))}

    b_in = [norm(r) for r in raw if r["system"] == "local" and r["temperature"] == 0 and r["type"] == "indomain"]
    b_off = [norm(r) for r in raw if r["system"] == "local" and r["temperature"] == 0 and r["type"] == "offtopic"]
    o_in = [r for r in opt if r["type"] == "indomain"]
    o_off = [r for r in opt if r["type"] == "offtopic"]
    B = _metrics(b_in, b_off, base_j)
    O = _metrics(o_in, o_off, opt_j)

    def row(name, key, unit=""):
        return f"| {name} | {B[key]}{unit} | {O[key]}{unit} |"

    lines = [
        "# Оптимизация локальной LLM под задачу — отчёт до/после",
        "",
        "Кейс: grounded-ответ с дословными цитатами по коду compose-samples (12 in-domain + "
        "5 off-topic, temp=0). **До** — baseline `qwen2.5:3b` + исходный промпт + k=8/ctx=4096. "
        "**После** — оптимизированный конфиг (см. ниже). Faithfulness судит один слепой "
        "арбитр DeepSeek для обеих версий.",
        "",
        "## Итоговый оптимальный конфиг",
        "- **Модель:** `qwen2.5-coder:3b` (Q4_K_M) — code-tuned, идеальное дословное цитирование.",
        "- **Промпт:** `copyfirst` — «сначала СКОПИРУЙ строку символ-в-символ → потом ответ» + 1 few-shot Kotlin-цитата.",
        "- **Параметры:** `RAG_K_AFTER=6`, `num_ctx=3072`, `num_predict=384`, `temperature=0`, `seed=42`.",
        "",
        "## Качество / скорость (до → после)",
        "",
        "| Метрика | До (baseline) | После (optimized) |",
        "|---|---|---|",
        row("In-domain отвечено (из 12)", "answered"),
        row("In-domain абстейнов", "abstained"),
        row("Grounded (слепой судья)", "grounded"),
        row("quotesDropped (среднее)", "qdrop"),
        row("jsonParseFailed", "jsonfail"),
        row("Off-topic abstain (регресс-страж)", "off_abs"),
        row("Латентность p50", "lat_p50", "с"),
        row("Латентность p95", "lat_p95", "с"),
        "",
        "## Вклад рычагов (dev-подмножество, 4 baseline-абстейна + 2 off-topic)",
        "",
        "| Вариант | Рычаг | Отвечено | quotesDropped |",
        "|---|---|---|---|",
        "| V0 baseline | — | 1/3* | 1.00 |",
        "| V1 | +copy-first промпт | 3/3* | 0.67 |",
        "| V2 | +k=4/ctx=2048 (лёгкие) | 2/4 | 0.25 |",
        "| V3 | +coder+умеренные | 3/4 | **0.00** |",
        "| V4 | qwen+умеренные (изоляция модели) | 3/4 | 1.00 |",
        "",
        "\\* q8 у V0/V1 не завершился (таймаут при k=8/ctx=4096). copy-first — решающий рычаг "
        "(1→3 отвечено); coder при равных параметрах даёт quotesDropped 0.00 vs 1.00 у qwen "
        "(идеальные дословные цитаты) и быстрее; агрессивное урезание k навредило (V2).",
        "",
        "## Ресурсы",
        "- qwen2.5-coder:3b Q4_K_M ~1.93 ГБ; `num_ctx=3072` (ниже baseline 4096) → меньше "
        "давление на RAM (VPS 3.8 ГБ + 4 ГБ swap; baseline при 4096 упирался в OOM).",
        "- Модель на CPU: латентность в секундах-десятках-секунд (см. таблицу).",
        "",
        "Результат: **оптимизированная локальная LLM под задачу** — деплой на https://llm.jorchik.com.",
    ]
    (HERE / "REPORT-optimization.md").write_text("\n".join(lines))
    print("Записан REPORT-optimization.md")
    print(f"  before: отвечено {B['answered']}/{B['n']}, grounded {B['grounded']}, qDrop {B['qdrop']}, p50 {B['lat_p50']}с")
    print(f"  after:  отвечено {O['answered']}/{O['n']}, grounded {O['grounded']}, qDrop {O['qdrop']}, p50 {O['lat_p50']}с")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--subset", choices=["dev", "full"], default="dev")
    r.add_argument("--reps", type=int, default=1)
    sub.add_parser("summary")
    sub.add_parser("judge")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.url, a.label, a.subset, a.reps)
    elif a.cmd == "judge":
        judge()
    elif a.cmd == "report":
        report()
    else:
        summary()


if __name__ == "__main__":
    main()
