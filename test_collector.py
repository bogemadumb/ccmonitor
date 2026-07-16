#!/usr/bin/env python3
"""Дебаг-харнес для collector.py — прогоняет сценарии сбоя и edge-cases.
Главный инвариант: collector НИКОГДА не падает и всегда отдаёт валидный JSON
(от этого зависит приложение). Запуск: python3 test_collector.py"""
import importlib.util, os, json, tempfile, subprocess, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("collector", os.path.join(HERE, "collector.py"))
col = importlib.util.module_from_spec(spec); spec.loader.exec_module(col)

results = []
def check(name, fn):
    try:
        fn(); results.append((name, "PASS", ""))
    except AssertionError as e:
        results.append((name, "FAIL", f"assert: {e}"))
    except Exception as e:
        results.append((name, "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

def tmp_cache(content=None):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    if content is None:
        os.remove(path)
    else:
        with open(path, "w") as f:
            f.write(content)
    return path

# ---------- ЛИМИТЫ: get_limits при разных состояниях ----------
def t_limits_fresh_cache():
    col.LIMITS_CACHE = tmp_cache(json.dumps({
        "_fetched_at": col.time.time(),
        "data": {"ok": True, "five_hour": {"used_percentage": 50, "resets_at_utc": "2026-07-16T01:30:00+00:00"},
                 "seven_day": None, "weekly_scoped": {}}}))
    r = col.get_limits()
    assert r["ok"] is True and r["stale"] is False, r
check("limits: свежий кэш → отдаётся без запроса", t_limits_fresh_cache)

def t_limits_stale_cache_on_error():
    col.LIMITS_CACHE = tmp_cache(json.dumps({
        "_fetched_at": col.time.time() - 9999,  # протух
        "data": {"ok": True, "five_hour": {"used_percentage": 42}, "seven_day": None, "weekly_scoped": {}}}))
    col._fetch_limits = lambda: {"ok": False, "error": "http_429"}
    r = col.get_limits()
    assert r["ok"] is True and r["stale"] is True and r["last_error"] == "http_429", r
check("limits: 429 при протухшем кэше → отдаём последнее известное (stale)", t_limits_stale_cache_on_error)

def t_limits_no_cache_no_network():
    col.LIMITS_CACHE = tmp_cache(None)  # кэша нет
    col._fetch_limits = lambda: {"ok": False, "error": "network", "detail": "boom"}
    r = col.get_limits()
    assert r["ok"] is False and r["error"] == "network", r
check("limits: нет кэша + нет сети → ok:false, без падения", t_limits_no_cache_no_network)

def t_limits_corrupt_cache():
    col.LIMITS_CACHE = tmp_cache("{ это не json ")  # битый кэш
    col._fetch_limits = lambda: {"ok": False, "error": "http_429"}
    r = col.get_limits()
    assert r["ok"] is False, r  # битый кэш игнорируется, ошибка отдаётся как есть
check("limits: битый JSON в кэше → не падает", t_limits_corrupt_cache)

def t_limits_success_writes_cache():
    col.LIMITS_CACHE = tmp_cache(None)
    col._fetch_limits = lambda: {"ok": True, "five_hour": {"used_percentage": 10, "resets_at_utc": "2026-07-16T01:30:00+00:00"},
                                 "seven_day": None, "weekly_scoped": {}}
    r = col.get_limits()
    assert r["ok"] is True and r["stale"] is False, r
    assert os.path.exists(col.LIMITS_CACHE), "кэш должен записаться"
check("limits: успех → кэш записывается", t_limits_success_writes_cache)

# ---------- window(): edge-cases внутри _fetch_limits через фейковый ответ ----------
def t_window_missing_seven_day():
    # прогон _fetch_limits логики окна через прямой вызов get_limits с успехом, seven_day=None
    col.LIMITS_CACHE = tmp_cache(None)
    col._fetch_limits = lambda: {"ok": True,
        "five_hour": {"used_percentage": 80, "resets_at_utc": "2026-07-16T01:30:00.5+00:00",
                      "resets_in_seconds": 100, "resets_hm": "01:30", "resets_full": "16.07 01:30",
                      "resets_in_human": "1 ч", "resets_in_short": "1:00"},
        "seven_day": None, "weekly_scoped": {}}
    r = col.get_limits()
    assert r["seven_day"] is None and r["five_hour"]["used_percentage"] == 80, r
check("limits: seven_day отсутствует → five_hour всё равно работает", t_window_missing_seven_day)

# ---------- ТОКЕНЫ: get_tokens при разных JSONL ----------
def with_projects(files: dict):
    """files: {relative_path: [lines...]}. Возвращает временный PROJECTS dir."""
    d = tempfile.mkdtemp()
    for rel, lines in files.items():
        full = os.path.join(d, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            for ln in lines:
                f.write((ln if isinstance(ln, str) else json.dumps(ln)) + "\n")
    return d

def t_tokens_no_files():
    col.PROJECTS = tempfile.mkdtemp()  # пусто
    r = col.get_tokens(None)
    assert r["ok"] is True and r["session_5h"] is None, r
check("tokens: нет файлов → ok, session_5h None", t_tokens_no_files)

def t_tokens_empty_and_garbage():
    col.PROJECTS = with_projects({"proj/a.jsonl": ["", "не json", "{}", "{\"type\":\"user\"}"]})
    r = col.get_tokens(None)
    assert r["ok"] is True, r  # битые строки пропускаются
check("tokens: пустые/битые строки JSONL → не падает", t_tokens_empty_and_garbage)

def t_tokens_synthetic_skipped():
    now = col.time.strftime("%Y-%m-%dT%H:%M:%S", col.time.gmtime())
    col.PROJECTS = with_projects({"proj/a.jsonl": [
        {"type": "assistant", "requestId": "r1", "timestamp": now + "Z",
         "message": {"id": "m1", "model": "<synthetic>", "usage": {"input_tokens": 999}}},
    ]})
    r = col.get_tokens(None)
    # только synthetic → нет валидных → session_5h None
    assert r["session_5h"] is None, r
check("tokens: <synthetic> исключается", t_tokens_synthetic_skipped)

def t_tokens_dedup_keepmax():
    now = col.time.strftime("%Y-%m-%dT%H:%M:%S", col.time.gmtime())
    ts = now + "Z"
    # два дубля одного (mid, req): плейсхолдер (1 токен) и полный (5000). Должен остаться полный.
    col.PROJECTS = with_projects({"proj/a.jsonl": [
        {"type": "assistant", "requestId": "r1", "timestamp": ts,
         "message": {"id": "m1", "model": "claude-opus-4-8", "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "assistant", "requestId": "r1", "timestamp": ts,
         "message": {"id": "m1", "model": "claude-opus-4-8", "usage": {"input_tokens": 5000, "output_tokens": 200}}},
    ]})
    entries = list(col.iter_usage_entries(recent_hours=6))
    ded = col.dedupe(entries)
    assert len(ded) == 1, f"дубль не схлопнут: {len(ded)}"
    assert col.usage_total(ded[0]["usage"]) == 5200, "должна остаться запись с макс. суммой"
check("tokens: дедуп по (id,req) оставляет макс-токен запись", t_tokens_dedup_keepmax)

def t_tokens_iterations_added():
    now = col.time.strftime("%Y-%m-%dT%H:%M:%S", col.time.gmtime())
    col.PROJECTS = with_projects({"proj/a.jsonl": [
        {"type": "assistant", "requestId": "r1", "timestamp": now + "Z",
         "message": {"id": "m1", "model": "claude-opus-4-8", "usage": {
             "output_tokens": 100,
             "iterations": [{"type": "advisor_message", "model": "claude-haiku-4-5", "output_tokens": 50}]}}},
    ]})
    ded = col.dedupe(list(col.iter_usage_entries(recent_hours=6)))
    recs = col.expand_iterations(ded)
    assert len(recs) == 2, f"iteration должен стать отдельной записью: {len(recs)}"
check("tokens: iterations учитываются отдельной записью", t_tokens_iterations_added)

def t_tokens_window_from_limits():
    # окно берётся из resets_at лимитов
    now_s = col.time.time()
    reset = col.datetime.datetime.fromtimestamp(now_s + 3600, col.datetime.timezone.utc).isoformat()
    inside = col.datetime.datetime.fromtimestamp(now_s - 600, col.datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    outside = col.datetime.datetime.fromtimestamp(now_s - 6*3600, col.datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    col.PROJECTS = with_projects({"proj/a.jsonl": [
        {"type": "assistant", "requestId": "rin", "timestamp": inside,
         "message": {"id": "min", "model": "claude-opus-4-8", "usage": {"output_tokens": 100}}},
        {"type": "assistant", "requestId": "rout", "timestamp": outside,
         "message": {"id": "mout", "model": "claude-opus-4-8", "usage": {"output_tokens": 999999}}},
    ]})
    limits = {"ok": True, "five_hour": {"resets_at_utc": reset}}
    r = col.get_tokens(limits)
    # только запись внутри окна учтена
    assert r["session_5h"]["tokens"]["output"] == 100, r["session_5h"]["tokens"]
check("tokens: окно 5ч из resets_at — вне окна отсекается", t_tokens_window_from_limits)

def t_price_unknown_model():
    p = col.price_for("совсем-неизвестная-модель")
    assert p == col.PRICING["opus-4-8"], "неизвестная модель → дефолт opus-4-8"
check("price: неизвестная модель → дефолт без падения", t_price_unknown_model)

def t_sum_empty_usage():
    agg = col.sum_tokens([{"model": "claude-opus-4-8", "ts": 0, "usage": {}}])
    assert agg["total"] == 0 and agg["cost_usd"] == 0.0, agg
check("sum: пустой usage → нули, без падения", t_sum_empty_usage)

# ---------- END-TO-END: реальный subprocess collector.py → валидный JSON ----------
def t_subprocess_valid_json():
    out = subprocess.run(["/usr/bin/python3", os.path.join(HERE, "collector.py")],
                         capture_output=True, timeout=30)
    assert out.returncode == 0, f"exit {out.returncode}: {out.stderr.decode()[:500]}"
    data = json.loads(out.stdout)  # бросит если не JSON
    assert "limits" in data and "tokens" in data, "нет ключей limits/tokens"
check("e2e: collector.py subprocess → exit 0 + валидный JSON", t_subprocess_valid_json)

# ---------- отчёт ----------
print("\n=== ДЕБАГ collector.py ===")
npass = sum(1 for _, s, _ in results if s == "PASS")
for name, status, detail in results:
    mark = "OK " if status == "PASS" else "FAIL"
    print(f"[{mark}] {name}")
    if detail:
        print("       " + detail.split(chr(10))[0])
print(f"\nИтог: {npass}/{len(results)} прошло")
sys.exit(0 if npass == len(results) else 1)
