#!/usr/bin/env python3
"""Сравнение двух RAG-систем на одном корпусе вопросов: облачная (DeepSeek,
backend :8000) vs локальная (Ollama qwen2.5:3b, backend :8010). Обе отдают
один и тот же контракт `POST /chat` (см. webchat/backend/main.py и
webchat_with_local_llm/backend/main.py): {messages, useRag, improvedRag,
temperature} -> {reply, sources:[{file,section,score,rerank_score?,chunk_id?,
quote?}], rewrittenQuery, ragMeta}.

Три подкоманды (по образцу webchat/eval/compare.py):
  run    — прогон матрицы {cloud,local} x {temperature 0,1}, пишет каждый
           вызов сразу в raw_runs.jsonl (резюмируемо по ключу).
  judge  — единый слепой судья (DeepSeek через :8000, useRag=false) оценивает
           faithfulness уже собранных ответов -> judgements.jsonl.
  report — считает метрики из jsonl (без повторной генерации) и пишет
           REPORT-local-vs-cloud.md.

Локальный grounded-вызов на 3B CPU занимает 70-120с — HTTP timeout 320с.
Полный прогон (без --smoke) долгий (~1.5-2ч) и запускается отдельно, не отсюда.
Только стандартная библиотека (urllib, как в образце compare.py).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
QUESTIONS_PATH = HERE / "questions.json"
RAW_PATH = HERE / "raw_runs.jsonl"
JUDGEMENTS_PATH = HERE / "judgements.jsonl"
REPORT_PATH = HERE / "REPORT-local-vs-cloud.md"

# Базовые URL систем — оба backend зовём напрямую, в обход nginx-токена.
SYSTEMS = {
    "cloud": os.getenv("CLOUD_URL", "http://127.0.0.1:8000"),
    "local": os.getenv("LOCAL_URL", "http://127.0.0.1:8010"),
}
# Судья — всегда облачный backend (DeepSeek), один и тот же арбитр для обеих
# систем, иначе перекос: qwen сам себя не судит.
JUDGE_URL = SYSTEMS["cloud"]

TIMEOUT_S = 320  # локальный 3B на CPU в grounded-режиме может отвечать 70-120с

REPEATS_T0 = int(os.getenv("REPEATS_T0", "1"))
REPEATS_T1 = int(os.getenv("REPEATS_T1", "3"))


# ------------------------------------------------------------------ HTTP ---

def call_chat(base_url: str, messages: list[dict], use_rag: bool,
              improved: bool, temperature: float) -> dict:
    """POST /chat на конкретный backend. Бросает исключение вызывающему —
    там оно превращается в error-запись, не роняя весь прогон."""
    body = json.dumps({
        "messages": messages,
        "useRag": use_rag, "improvedRag": improved,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


# ------------------------------------------------------------- resumable ---

def load_done_keys(path: Path) -> set[str]:
    """Ключи уже сделанных строк jsonl — для пропуска при повторном запуске
    прерванного прогона. Битые строки (например, оборванные при Ctrl-C
    посреди записи) молча игнорируются — тот key будет перегенерирован."""
    done: set[str] = set()
    if not path.exists():
        return done
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(json.loads(line)["key"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


# ------------------------------------------------------------------- run ---

def load_questions() -> tuple[list[dict], list[dict]]:
    questions = json.loads(QUESTIONS_PATH.read_text())
    indomain = [q for q in questions if not q.get("offtopic")]
    offtopic = [q for q in questions if q.get("offtopic")]
    return indomain, offtopic


def run_one(system: str, temperature: int, q: dict, rep: int,
            qtype: str, done: set[str]) -> None:
    key = f"{system}|{temperature}|{q['id']}|{rep}"
    if key in done:
        return
    messages = q.get("context", []) + [{"role": "user", "content": q["question"]}]
    base_url = SYSTEMS[system]
    t0 = time.perf_counter()
    try:
        r = call_chat(base_url, messages, True, True, temperature)
        latency = time.perf_counter() - t0
        sources = [
            {"file": s.get("file"), "section": s.get("section"), "quote": s.get("quote")}
            for s in r.get("sources", [])
        ]
        meta = r.get("ragMeta", {})
        record = {
            "key": key, "system": system, "temperature": temperature,
            "qid": q["id"], "rep": rep, "type": qtype,
            "question": q["question"], "expected_sources": q.get("expected_sources", []),
            "latency_s": round(latency, 2), "reply": r.get("reply"),
            "sources": sources, "ragMeta": meta,
        }
        abstained = bool(meta.get("abstained"))
        print(f"[{system} t{temperature} q{q['id']} r{rep}] "
              f"{latency:.1f}s sources={len(sources)} abstained={abstained}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, ConnectionError) as e:
        latency = time.perf_counter() - t0
        record = {
            "key": key, "system": system, "temperature": temperature,
            "qid": q["id"], "rep": rep, "type": qtype,
            "question": q["question"], "expected_sources": q.get("expected_sources", []),
            "latency_s": round(latency, 2), "reply": None,
            "sources": [], "ragMeta": {"error": str(e)},
        }
        print(f"[{system} t{temperature} q{q['id']} r{rep}] ERROR after {latency:.1f}s: {e}")
    append_jsonl(RAW_PATH, record)
    done.add(key)


def cmd_run(args: argparse.Namespace) -> None:
    smoke = args.smoke or os.getenv("SMOKE") == "1"
    indomain, offtopic = load_questions()

    systems = [args.system] if args.system != "both" else ["cloud", "local"]
    temps = [args.temp] if args.temp is not None else [0, 1]

    if smoke:
        indomain = indomain[:2]
        offtopic = offtopic[:1]
        temps = [0]
        repeats = {0: 1}
        print("== SMOKE: 2 in-domain + 1 off-topic, temp=0, N=1 ==")
    else:
        repeats = {0: REPEATS_T0, 1: REPEATS_T1}

    done = load_done_keys(RAW_PATH)
    questions = [(q, "indomain") for q in indomain] + [(q, "offtopic") for q in offtopic]

    for system in systems:
        for temperature in temps:
            n = repeats[temperature]
            for q, qtype in questions:
                for rep in range(n):
                    run_one(system, temperature, q, rep, qtype, done)

    print(f"\nDone. raw_runs.jsonl: {len(load_done_keys(RAW_PATH))} строк.")


# ----------------------------------------------------------------- judge ---

def judge(question: str, answer: str, quotes: list[str]) -> tuple[bool | None, str]:
    """Слепой судья: не знает, чья это система. Логика — как в webchat/eval/compare.py."""
    if not quotes:
        return None, "нет цитат для проверки"
    quoted = "\n".join(f"[{i}] {q}" for i, q in enumerate(quotes, 1))
    prompt = (
        "Ты — строгий проверяющий фактической опоры ответа на цитаты. Дан вопрос, "
        "ответ ассистента и цитаты из источников, на которые он опирался. Реши, "
        "следует ли фактическое содержание ответа из этих цитат (подтверждается ими, "
        "не выдумано и не противоречит им). Язык ответа и цитат может отличаться — "
        "оценивай смысл, а не язык.\n"
        'Ответь СТРОГО одним JSON-объектом: {"grounded": true|false, "reason": "<кратко по-русски>"}.\n\n'
        f"Вопрос: {question}\n\nОтвет ассистента: {answer}\n\nЦитаты:\n{quoted}"
    )
    try:
        r = call_chat(JUDGE_URL, [{"role": "user", "content": prompt}], False, False, 0)
        reply = r["reply"]
        m = re.search(r"\{.*\}", reply, re.S)
        data = json.loads(m.group(0)) if m else {}
        return bool(data.get("grounded")), str(data.get("reason", "")).strip()
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            KeyError, ValueError) as e:
        return None, f"ошибка судьи: {e}"


def cmd_judge(_args: argparse.Namespace) -> None:
    if not RAW_PATH.exists():
        print("raw_runs.jsonl не найден — сначала `run`.")
        return
    raw = [json.loads(line) for line in RAW_PATH.read_text().splitlines() if line.strip()]
    done = load_done_keys(JUDGEMENTS_PATH)

    judged = skipped = 0
    for r in raw:
        if r["type"] != "indomain" or r["key"] in done:
            continue
        meta = r.get("ragMeta", {})
        if meta.get("abstained") or meta.get("error"):
            skipped += 1
            continue
        quotes = [s["quote"] for s in r.get("sources", []) if s.get("quote")]
        if not quotes:
            skipped += 1
            continue
        grounded, reason = judge(r["question"], r["reply"] or "", quotes)
        append_jsonl(JUDGEMENTS_PATH, {"key": r["key"], "grounded": grounded, "reason": reason})
        done.add(r["key"])
        judged += 1
        print(f"[judge {r['key']}] grounded={grounded} — {reason[:60]}")

    print(f"\nDone. judged={judged}, skipped(abstain/no-quotes/error)={skipped}, "
          f"already-done={len(done) - judged}.")


# ---------------------------------------------------------------- report ---

def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def hit(expected: list[str], sources: list[dict]) -> bool:
    files = [s.get("file") for s in sources]
    return any(exp in files for exp in expected)


def cmd_report(_args: argparse.Namespace) -> None:
    if not RAW_PATH.exists():
        print("raw_runs.jsonl не найден — сначала `run`.")
        return
    raw = [json.loads(line) for line in RAW_PATH.read_text().splitlines() if line.strip()]
    judgements = {}
    if JUDGEMENTS_PATH.exists():
        for line in JUDGEMENTS_PATH.read_text().splitlines():
            if line.strip():
                j = json.loads(line)
                judgements[j["key"]] = j

    cells_order = [("cloud", 0), ("cloud", 1), ("local", 0), ("local", 1)]
    cell_names = {(s, t): f"{s} t{t}" for s, t in cells_order}

    def rows_for(system: str, temperature: int, rtype: str | None = None) -> list[dict]:
        return [r for r in raw if r["system"] == system and r["temperature"] == temperature
                and (rtype is None or r["type"] == rtype)]

    metrics: dict[tuple[str, int], dict] = {}
    for system, temperature in cells_order:
        ind = rows_for(system, temperature, "indomain")
        off = rows_for(system, temperature, "offtopic")
        ok_ind = [r for r in ind if not r.get("ragMeta", {}).get("error")]
        answered = [r for r in ok_ind if not r.get("ragMeta", {}).get("abstained")]

        judged_rows = [(r, judgements.get(r["key"])) for r in answered]
        judged_rows = [(r, j) for r, j in judged_rows if j and j.get("grounded") is not None]
        grounded_n = sum(1 for _, j in judged_rows if j["grounded"] is True)

        sources_present = sum(1 for r in ok_ind if len(r.get("sources", [])) > 0)
        retrieval_hit = sum(1 for r in ok_ind if hit(r.get("expected_sources", []), r.get("sources", [])))

        off_ok = [r for r in off if not r.get("ragMeta", {}).get("error")]
        abstain_correct = sum(1 for r in off_ok if r.get("ragMeta", {}).get("abstained") is True)

        latencies = [r["latency_s"] for r in answered]

        # Стабильность — только temp=1, по группам повторов на один qid.
        stability = {"consistent_rate": None, "quotes_dropped_mean": None,
                     "quotes_dropped_max": None, "json_parse_failed": 0}
        if temperature == 1:
            t1 = rows_for(system, 1, "indomain")
            by_q: dict[int, list[dict]] = {}
            for r in t1:
                by_q.setdefault(r["qid"], []).append(r)
            consistent = 0
            considered = 0
            for _qid, reps in by_q.items():
                oks = [r for r in reps if not r.get("ragMeta", {}).get("error")]
                if not oks:
                    continue
                considered += 1
                decisions = {bool(r.get("ragMeta", {}).get("abstained")) for r in oks}
                if len(decisions) == 1:
                    consistent += 1
            dropped_vals = [r["ragMeta"].get("quotesDropped", 0) for r in t1
                             if not r.get("ragMeta", {}).get("error")
                             and r["ragMeta"].get("quotesDropped") is not None]
            stability["consistent_rate"] = (consistent / considered) if considered else None
            stability["quotes_dropped_mean"] = (statistics.mean(dropped_vals) if dropped_vals else None)
            stability["quotes_dropped_max"] = (max(dropped_vals) if dropped_vals else None)
            stability["json_parse_failed"] = sum(
                1 for r in t1 if r.get("ragMeta", {}).get("jsonParseFailed") is True
            )

        metrics[(system, temperature)] = {
            "n_indomain": len(ind), "n_offtopic": len(off),
            "grounded_rate": (grounded_n / len(judged_rows)) if judged_rows else None,
            "grounded_n": grounded_n, "grounded_denom": len(judged_rows),
            "sources_present_rate": (sources_present / len(ok_ind)) if ok_ind else None,
            "retrieval_hit_rate": (retrieval_hit / len(ok_ind)) if ok_ind else None,
            "abstain_correct_rate": (abstain_correct / len(off_ok)) if off_ok else None,
            "latency_mean": statistics.mean(latencies) if latencies else None,
            "latency_p50": pct(latencies, 0.5),
            "latency_p95": pct(latencies, 0.95),
            **stability,
        }

    write_report(metrics, cell_names, cells_order, raw, judgements)
    print(f"Отчёт записан: {REPORT_PATH}")


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def fmt_s(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}с"


def fmt_num(x: float | None, nd: int = 2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def write_report(metrics: dict, cell_names: dict, cells_order: list,
                  raw: list[dict], judgements: dict) -> None:
    lines = [
        "# Локальная (qwen2.5:3b) vs облачная (DeepSeek) RAG-система — отчёт eval",
        "",
        "Генерируется `compare_local_vs_cloud.py`. Один и тот же корпус вопросов "
        "(compose-samples, 12 in-domain + 5 off-topic) прогнан против двух систем "
        "с одинаковым контрактом `POST /chat` (`useRag=true, improvedRag=true`): "
        "**cloud** — backend на DeepSeek (`:8000`), **local** — backend на Ollama "
        "qwen2.5:3b (`:8010`). Faithfulness судит DeepSeek через `:8000` в no-RAG "
        "режиме — один и тот же слепой арбитр для обеих систем.",
        "",
        "## Сводная таблица",
        "",
        "| Метрика | " + " | ".join(cell_names[c] for c in cells_order) + " |",
        "|---|" + "---|" * len(cells_order),
    ]

    def row(label: str, key: str, fmt) -> str:
        vals = [fmt(metrics[c][key]) for c in cells_order]
        return f"| {label} | " + " | ".join(vals) + " |"

    lines += [
        row("Grounded (судья), доля от отвеченных", "grounded_rate", fmt_pct),
        row("Источники присутствуют", "sources_present_rate", fmt_pct),
        row("Retrieval hit (expected_sources)", "retrieval_hit_rate", fmt_pct),
        row("Off-topic abstain correct", "abstain_correct_rate", fmt_pct),
        row("Latency mean", "latency_mean", fmt_s),
        row("Latency p50", "latency_p50", fmt_s),
        row("Latency p95", "latency_p95", fmt_s),
        row("Стабильность abstain↔answer (t=1)", "consistent_rate", fmt_pct),
        row("quotesDropped mean (t=1)", "quotes_dropped_mean", fmt_num),
        row("quotesDropped max (t=1)", "quotes_dropped_max", lambda x: "—" if x is None else str(int(x))),
        row("jsonParseFailed count (t=1)", "json_parse_failed", lambda x: str(x)),
    ]

    def m(system, temp, key):
        return metrics[(system, temp)][key]

    lines += [
        "",
        "## Выводы",
        "",
        "**Качество (grounded / retrieval / abstain).** "
        f"Cloud: grounded {fmt_pct(m('cloud',0,'grounded_rate'))} (t0) / "
        f"{fmt_pct(m('cloud',1,'grounded_rate'))} (t1), retrieval hit "
        f"{fmt_pct(m('cloud',0,'retrieval_hit_rate'))} (t0). "
        f"Local: grounded {fmt_pct(m('local',0,'grounded_rate'))} (t0) / "
        f"{fmt_pct(m('local',1,'grounded_rate'))} (t1), retrieval hit "
        f"{fmt_pct(m('local',0,'retrieval_hit_rate'))} (t0). "
        f"Off-topic abstain-correct — cloud {fmt_pct(m('cloud',0,'abstain_correct_rate'))}, "
        f"local {fmt_pct(m('local',0,'abstain_correct_rate'))}.",
        "",
        "**Скорость.** "
        f"Cloud latency mean {fmt_s(m('cloud',0,'latency_mean'))} (t0), "
        f"local {fmt_s(m('local',0,'latency_mean'))} (t0) — локальная модель на CPU "
        "ожидаемо на порядок медленнее облачной.",
        "",
        "**Стабильность (temp=1, повторы).** "
        f"Cloud: consistent abstain↔answer {fmt_pct(m('cloud',1,'consistent_rate'))}, "
        f"quotesDropped mean {fmt_num(m('cloud',1,'quotes_dropped_mean'))}, "
        f"jsonParseFailed {m('cloud',1,'json_parse_failed')}. "
        f"Local: consistent {fmt_pct(m('local',1,'consistent_rate'))}, "
        f"quotesDropped mean {fmt_num(m('local',1,'quotes_dropped_mean'))}, "
        f"jsonParseFailed {m('local',1,'json_parse_failed')}.",
        "",
        "## Примеры (вопрос → ответ cloud vs local)",
        "",
    ]

    # 2-3 примера: берём первые indomain qid, где есть хотя бы по одной успешной
    # записи (temp=0, rep=0) у обеих систем.
    by_key = {r["key"]: r for r in raw}
    example_qids = sorted({r["qid"] for r in raw if r["type"] == "indomain"})[:3]
    for qid in example_qids:
        c = by_key.get(f"cloud|0|{qid}|0")
        l = by_key.get(f"local|0|{qid}|0")
        if not c and not l:
            continue
        q_text = (c or l)["question"]
        lines.append(f"### {qid}. {q_text}")
        lines.append("")
        for label, r in (("cloud", c), ("local", l)):
            if not r:
                lines += [f"**{label}**: нет данных", ""]
                continue
            meta = r.get("ragMeta", {})
            src = ", ".join(s.get("file", "") for s in r.get("sources", [])) or "нет"
            lines += [
                f"**{label}** ({r['latency_s']}с, abstained={bool(meta.get('abstained'))})",
                "",
                "> " + str(r.get("reply") or "").replace("\n", "\n> ")[:500],
                "",
                f"_источники: {src}_", "",
            ]
        lines.append("---")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------- argparse ---

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="прогон матрицы cloud/local x temp 0/1")
    p_run.add_argument("--smoke", action="store_true",
                        help="мини-прогон: 2 in-domain + 1 off-topic, temp=0, N=1")
    p_run.add_argument("--system", choices=["cloud", "local", "both"], default="both")
    p_run.add_argument("--temp", type=int, choices=[0, 1], default=None,
                        help="ограничить одной температурой (по умолчанию — обе)")
    p_run.set_defaults(func=cmd_run)

    p_judge = sub.add_parser("judge", help="слепой судья faithfulness по raw_runs.jsonl")
    p_judge.set_defaults(func=cmd_judge)

    p_report = sub.add_parser("report", help="метрики + REPORT-local-vs-cloud.md")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
