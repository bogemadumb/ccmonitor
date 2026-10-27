#!/usr/bin/env python3
"""Дебаг-харнес для collector.py — прогоняет сценарии сбоя и edge-cases.
Главный инвариант: collector НИКОГДА не падает и всегда отдаёт валидный JSON
(от этого зависит приложение). Запуск: python3 test_collector.py"""
import importlib.util, os, json, tempfile, subprocess, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("collector", os.path.join(HERE, "collector.py"))
col = importlib.util.module_from_spec(spec); spec.loader.exec_module(col)

# файлы настроек/состояния — во временную папку, чтобы реальные настройки не влияли на тесты
_ISO = tempfile.mkdtemp()
col.SETTINGS_PATH = os.path.join(_ISO, "settings.json")
col.ACCESS_STATE = os.path.join(_ISO, "access-state.json")
col.PENDING_PATH = os.path.join(_ISO, ".oauth-pending.json")
col.USAGE_DIR = _ISO

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
    col._fetch_limits = lambda *_: {"ok": False, "error": "http_429"}
    r = col.get_limits()
    assert r["ok"] is True and r["stale"] is True and r["last_error"] == "http_429", r
check("limits: 429 при протухшем кэше → отдаём последнее известное (stale)", t_limits_stale_cache_on_error)

def t_limits_no_cache_no_network():
    col.LIMITS_CACHE = tmp_cache(None)  # кэша нет
    col._fetch_limits = lambda *_: {"ok": False, "error": "network", "detail": "boom"}
    r = col.get_limits()
    assert r["ok"] is False and r["error"] == "network", r
check("limits: нет кэша + нет сети → ok:false, без падения", t_limits_no_cache_no_network)

def t_limits_corrupt_cache():
    col.LIMITS_CACHE = tmp_cache("{ это не json ")  # битый кэш
    col._fetch_limits = lambda *_: {"ok": False, "error": "http_429"}
    r = col.get_limits()
    assert r["ok"] is False, r  # битый кэш игнорируется, ошибка отдаётся как есть
check("limits: битый JSON в кэше → не падает", t_limits_corrupt_cache)

def t_limits_success_writes_cache():
    col.LIMITS_CACHE = tmp_cache(None)
    col._fetch_limits = lambda *_: {"ok": True, "five_hour": {"used_percentage": 10, "resets_at_utc": "2026-07-16T01:30:00+00:00"},
                                 "seven_day": None, "weekly_scoped": {}}
    r = col.get_limits()
    assert r["ok"] is True and r["stale"] is False, r
    assert os.path.exists(col.LIMITS_CACHE), "кэш должен записаться"
check("limits: успех → кэш записывается", t_limits_success_writes_cache)

# ---------- ЛИМИТЫ: троттлинг по Retry-After (регресс: 429-петля 01.08.2026) ----------
def _stale_cache(pct=42, resets="2026-07-16T01:30:00+00:00"):
    return tmp_cache(json.dumps({
        "_fetched_at": col.time.time() - 9999,
        "data": {"ok": True, "five_hour": {"used_percentage": pct, "resets_at_utc": resets},
                 "seven_day": None, "weekly_scoped": {}}}))

def t_retry_after_saved_and_honored():
    col.LIMITS_CACHE = _stale_cache()
    calls = []
    col._fetch_limits = lambda *_: (calls.append(1), {"ok": False, "error": "http_429", "retry_after": 1296})[1]
    col.get_limits()
    saved = json.load(open(col.LIMITS_CACHE))
    assert saved["_retry_until"] > col.time.time() + 1200, saved
    assert saved["data"]["ok"] is True, "последний успешный кэш обязан пережить 429"
    col.get_limits()  # второй заход — сервер трогать нельзя
    assert len(calls) == 1, f"запросов к API: {len(calls)}, ожидался 1"
check("limits: 429 с Retry-After → пауза сохраняется, повторный запрос не уходит", t_retry_after_saved_and_honored)

def t_backoff_without_header():
    col.LIMITS_CACHE = _stale_cache()
    col._fetch_limits = lambda *_: {"ok": False, "error": "http_429", "retry_after": None}
    prev = 0
    for step in col.BACKOFF_STEPS:
        json.dump({**json.load(open(col.LIMITS_CACHE)), "_retry_until": 0}, open(col.LIMITS_CACHE, "w"))
        col.get_limits()
        pause = json.load(open(col.LIMITS_CACHE))["_retry_until"] - col.time.time()
        assert pause >= prev, f"backoff не растёт: {pause} после {prev}"
        prev = pause
    assert prev >= col.BACKOFF_STEPS[-1] - 5, prev
check("limits: 429 без Retry-After → backoff нарастает до потолка", t_backoff_without_header)

def t_success_clears_throttle():
    col.LIMITS_CACHE = tmp_cache(json.dumps({
        "_fetched_at": col.time.time() - 9999, "_retry_until": 0, "_fail_count": 3,
        "_last_error": "http_429",
        "data": {"ok": True, "five_hour": {"used_percentage": 1}, "seven_day": None, "weekly_scoped": {}}}))
    col._fetch_limits = lambda *_: {"ok": True, "five_hour": {"used_percentage": 7, "resets_at_utc": "2026-07-16T01:30:00+00:00"},
                                 "seven_day": None, "weekly_scoped": {}}
    r = col.get_limits()
    saved = json.load(open(col.LIMITS_CACHE))
    assert r["stale"] is False and saved["_fail_count"] == 0 and saved["_retry_until"] == 0, saved
check("limits: успех сбрасывает паузу и счётчик неудач", t_success_clears_throttle)

def t_network_error_no_pause():
    # нет интернета — сервер не при чём, запирать опрос на полчаса незачем
    col.LIMITS_CACHE = _stale_cache()
    col._fetch_limits = lambda *_: {"ok": False, "error": "network", "detail": "boom"}
    col.get_limits()
    assert json.load(open(col.LIMITS_CACHE))["_retry_until"] == 0
check("limits: нет сети → пауза не назначается", t_network_error_no_pause)

def t_expired_window_shows_dash():
    col.LIMITS_CACHE = _stale_cache(resets="2020-01-01T00:00:00+00:00")  # окно давно прошло
    col._fetch_limits = lambda *_: {"ok": False, "error": "http_429", "retry_after": 60}
    r = col.get_limits()
    assert r["five_hour"]["resets_in_short"] == "—", r["five_hour"]
check("limits: протухший кэш с истёкшим окном → прочерк, а не фальшивый 0:00", t_expired_window_shows_dash)

def t_parse_retry_after():
    assert col._parse_retry_after("1296") == 1296
    assert col._parse_retry_after(None) is None
    assert col._parse_retry_after("мусор") is None
    import email.utils, datetime as dt
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=600)
    assert 590 <= col._parse_retry_after(email.utils.format_datetime(future)) <= 610
check("Retry-After: секунды, HTTP-дата, мусор", t_parse_retry_after)

# ---------- window(): edge-cases внутри _fetch_limits через фейковый ответ ----------
def t_window_missing_seven_day():
    # прогон _fetch_limits логики окна через прямой вызов get_limits с успехом, seven_day=None
    col.LIMITS_CACHE = tmp_cache(None)
    col._fetch_limits = lambda *_: {"ok": True,
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
    # iteration тут НЕПОЛНАЯ (50 из 100) — значит верхний уровень агрегат, берём только его
    assert len(recs) == 1, f"неполная iteration не должна добавляться сверх основной: {len(recs)}"
    assert col.sum_tokens(recs)["output"] == 100, col.sum_tokens(recs)
check("tokens: неполная iteration не двоит основную запись", t_tokens_iterations_added)

def t_tokens_iterations_no_double_count():
    """Главный случай на практике: iteration ДУБЛИРУЕТ основную запись один в один.
    Раньше считались обе, и все цифры токенов были ровно вдвое больше настоящих."""
    now = col.time.strftime("%Y-%m-%dT%H:%M:%S", col.time.gmtime())
    dup = {"model": "claude-opus-4-8", "output_tokens": 300, "cache_read_input_tokens": 50000}
    col.PROJECTS = with_projects({"proj/a.jsonl": [
        {"type": "assistant", "requestId": "r1", "timestamp": now + "Z",
         "message": {"id": "m1", "model": "claude-opus-4-8", "usage": {
             "output_tokens": 300, "cache_read_input_tokens": 50000,
             "iterations": [dup]}}},
    ]})
    recs = col.expand_iterations(col.dedupe(list(col.iter_usage_entries(recent_hours=6))))
    agg = col.sum_tokens(recs)
    assert agg["output"] == 300, f"вывод удвоен: {agg['output']}"
    assert agg["cache_read"] == 50000, f"кэш удвоен: {agg['cache_read']}"
check("tokens: iteration-дубль не удваивает счёт", t_tokens_iterations_no_double_count)

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

# ---------- МУЛЬТИАККАУНТ ----------
def t_account_token_fresh_no_refresh():
    """Живой access не должен дёргать сеть — иначе refresh ротируется впустую."""
    col._keychain_read = lambda s: {"access_token": "AAA", "expires_at": col.time.time() + 3600}
    col._oauth_refresh = lambda rt: (_ for _ in ()).throw(AssertionError("рефреш не нужен"))
    assert col.account_token("svc") == "AAA"
check("accounts: живой access → сеть не дёргается", t_account_token_fresh_no_refresh)

def t_account_token_refreshes_and_persists():
    """refresh_token РОТИРУЕТСЯ: новый обязан быть записан обратно, иначе цепочка порвётся."""
    saved = {}
    col._keychain_read = lambda s: {"access_token": "OLD", "expires_at": 0, "refresh_token": "R1"}
    col._oauth_refresh = lambda rt: {"access_token": "NEW", "refresh_token": "R2",
                                     "expires_in": 28800, "refresh_token_expires_in": 2419200}
    col._keychain_write = lambda svc, payload: saved.update(payload) or True
    assert col.account_token("svc") == "NEW"
    assert saved.get("refresh_token") == "R2", "новый refresh не сохранён"
    assert saved.get("expires_at", 0) > col.time.time(), "не проставлен абсолютный expires_at"
check("accounts: истёкший access → refresh + новый ключ сохранён", t_account_token_refreshes_and_persists)

def t_account_token_dead_refresh():
    col._keychain_read = lambda s: {"expires_at": 0, "refresh_token": "R",
                                    "refresh_expires_at": col.time.time() - 1}
    col._oauth_refresh = lambda rt: (_ for _ in ()).throw(AssertionError("не должен ходить"))
    try:
        col.account_token("svc"); assert False, "ожидалась ошибка relogin_needed"
    except RuntimeError as e:
        assert "relogin" in str(e), e
check("accounts: протухший refresh → relogin_needed, без запроса", t_account_token_dead_refresh)

def t_accounts_separate_caches():
    """У каждой подписки свой кэш: квота usage-endpoint считается по аккаунту."""
    c1, c2 = tmp_cache(), tmp_cache()
    col._fetch_limits = lambda *_: {"ok": True, "five_hour": {"used_percentage": 11,
                                    "resets_at_utc": "2026-07-16T01:30:00+00:00"},
                                    "seven_day": None, "weekly_scoped": {}}
    col.get_limits(c1, lambda: "t1")
    col._fetch_limits = lambda *_: {"ok": True, "five_hour": {"used_percentage": 77,
                                    "resets_at_utc": "2026-07-16T01:30:00+00:00"},
                                    "seven_day": None, "weekly_scoped": {}}
    col.get_limits(c2, lambda: "t2")
    a = json.load(open(c1))["data"]["five_hour"]["used_percentage"]
    b = json.load(open(c2))["data"]["five_hour"]["used_percentage"]
    assert (a, b) == (11, 77), (a, b)
check("accounts: у каждой подписки свой кэш, не смешиваются", t_accounts_separate_caches)

def t_accounts_fallback_without_creds():
    """Своих кредов нет — работаем по-старому, на активном токене Claude Code."""
    col._keychain_read = lambda s: None
    col.get_limits = lambda *a, **k: {"ok": True, "five_hour": {"used_percentage": 5}}
    col.get_plan = lambda: {"label": "Max"}
    col.active_email = lambda: "me@example.com"
    accs = col.collect_accounts()
    assert len(accs) == 1 and accs[0]["active"] is True, accs
check("accounts: нет своих кредов → fallback на активный аккаунт", t_accounts_fallback_without_creds)

def t_accounts_active_flag_and_order():
    col._keychain_read = lambda s: ({"access_token": "x", "expires_at": col.time.time() + 999,
                                     "account": {"email_address": "second@b.com"}, "label": "Max 20x"}
                                    if s == "CCMonitor-account-1" else
                                    {"access_token": "y", "expires_at": col.time.time() + 999,
                                     "account": {"email_address": "me@a.com"}, "label": "Max"}
                                    if s == "CCMonitor-account-2" else None)
    col.get_limits = lambda *a, **k: {"ok": True, "five_hour": {"used_percentage": 1}}
    col.active_email = lambda: "ME@A.COM"       # регистр не должен мешать
    accs = col.collect_accounts()
    assert len(accs) == 2, accs
    assert accs[0]["email"] == "me@a.com" and accs[0]["active"] is True, "активный не первый"
    assert accs[1]["active"] is False, accs[1]
check("accounts: активный определяется по почте и идёт первым", t_accounts_active_flag_and_order)

# ---------- CODEX ----------
def t_codex_window_countdown():
    """resets_at у Codex — абсолютный epoch; отсчёт считаем от него, а не храним замороженным."""
    w = col._codex_window({"used_percent": 12.0, "window_minutes": 300,
                           "resets_at": int(col.time.time()) + 3720})
    assert w["used_percentage"] == 12.0
    # усечение дробной части секунды допустимо, час с небольшим — нет
    assert 3715 <= w["resets_in_seconds"] <= 3720, w["resets_in_seconds"]
    assert w["resets_in_short"] in ("1:01", "1:02"), w["resets_in_short"]
check("codex: обратный отсчёт от абсолютного resets_at", t_codex_window_countdown)

def t_codex_past_reset_no_negative():
    w = col._codex_window({"used_percent": 3, "resets_at": int(col.time.time()) - 5000})
    assert w["resets_in_seconds"] == 0, w
check("codex: просроченное окно → ноль, а не отрицательное время", t_codex_past_reset_no_negative)

def t_codex_no_snapshot():
    col.CODEX_SNAPSHOT = "/nonexistent/codex-usage.json"
    r = col.get_codex()
    assert r["ok"] is False and r["error"] == "no_snapshot", r
check("codex: нет среза → no_snapshot, без падения", t_codex_no_snapshot)

def t_codex_snapshot_not_ok():
    col.CODEX_SNAPSHOT = tmp_cache(json.dumps({"ok": False, "error": "no_sessions"}))
    r = col.get_codex()
    assert r["ok"] is False and r["error"] == "no_sessions", r
check("codex: срез без данных → ошибка пробрасывается", t_codex_snapshot_not_ok)

def t_codex_age_from_measured_at():
    """Файл обновляется каждые 5 минут, но цифры меняются только когда Codex работал —
    возраст обязан считаться по measured_at, иначе снимок выглядит свежим всегда."""
    old = col.datetime.datetime.now(col.datetime.timezone.utc) - col.datetime.timedelta(hours=3)
    col.CODEX_SNAPSHOT = tmp_cache(json.dumps({
        "ok": True, "plan": "plus", "host": "mac",
        "measured_at": old.isoformat().replace("+00:00", "Z"),
        "five_hour": {"used_percent": 9.0, "resets_at": int(col.time.time()) + 600},
        "weekly": {"used_percent": 2.0, "resets_at": int(col.time.time()) + 60000}}))
    r = col.get_codex()
    assert r["ok"] and r["label"] == "Codex Plus", r
    assert 10700 < r["age_seconds"] < 11500, r["age_seconds"]
    assert r["five_hour"]["used_percentage"] == 9.0
check("codex: возраст данных считается от measured_at", t_codex_age_from_measured_at)

def t_codex_garbage_file():
    col.CODEX_SNAPSHOT = tmp_cache("{не json")
    r = col.get_codex()
    assert r["ok"] is False, r
check("codex: битый файл → не падает", t_codex_garbage_file)

# ---------- ЛОКАЛЬНЫЙ СБОРЩИК CODEX: codex-usage-local.py ----------
_cul_spec = importlib.util.spec_from_file_location("codexlocal", os.path.join(HERE, "codex-usage-local.py"))
cul = importlib.util.module_from_spec(_cul_spec); _cul_spec.loader.exec_module(cul)


def tc_event(ts, primary_pct, primary_reset, secondary_pct=1.0, plan="plus"):
    """Событие token_count в том виде, в каком его пишет Codex в журнал сессии."""
    return {"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"total_tokens": 100}},
        "rate_limits": {
            "primary": {"used_percent": primary_pct, "window_minutes": 300, "resets_at": primary_reset},
            "secondary": {"used_percent": secondary_pct, "window_minutes": 10080,
                          "resets_at": primary_reset + 500000},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "plan_type": plan, "rate_limit_reached_type": None}}}


def write_rollout(dirpath, name, records):
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False))
            f.write("\n")
    return path


def sessions_dir():
    d = tempfile.mkdtemp()
    cul.SESSIONS_DIR = os.path.join(d, "sessions")
    return cul.SESSIONS_DIR


def t_cul_takes_last_event():
    root = sessions_dir()
    now = int(cul.time.time())
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-a.jsonl", [
        tc_event("2026-08-31T09:00:00.000Z", 2.0, now + 3600),
        tc_event("2026-08-31T09:40:00.000Z", 7.0, now + 3600),
    ])
    r = cul.collect_sessions()
    assert r["ok"] and r["plan"] == "plus", r
    assert r["five_hour"]["used_percent"] == 7.0, r
    assert r["measured_at"].startswith("2026-08-31T09:40"), r["measured_at"]
check("codex-local: берётся последний token_count в журнале", t_cul_takes_last_event)

def t_cul_newest_across_files():
    root = sessions_dir()
    now = int(cul.time.time())
    old = write_rollout(os.path.join(root, "2026", "08", "30"), "rollout-old.jsonl",
                        [tc_event("2026-08-31T11:00:00.000Z", 33.0, now + 3600)])
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-new.jsonl",
                  [tc_event("2026-08-31T08:00:00.000Z", 4.0, now + 3600)])
    os.utime(old, (1, 1))  # у свежего события файл нарочно старый по mtime
    r = cul.collect_sessions()
    assert r["five_hour"]["used_percent"] == 33.0, r
check("codex-local: победитель — по времени события, а не по mtime файла", t_cul_newest_across_files)

def t_cul_rolled_over():
    root = sessions_dir()
    now = int(cul.time.time())
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-a.jsonl",
                  [tc_event("2026-08-31T09:00:00.000Z", 2.0, now - 5000)])
    r = cul.collect_sessions()
    assert r["five_hour"]["rolled_over"] is True, r
    assert r["five_hour"]["used_percent"] == 0, r
    assert r["five_hour"]["resets_at"] is None, r
check("codex-local: закрытое окно → ноль и rolled_over, а не старый процент", t_cul_rolled_over)

def t_cul_garbage_lines():
    root = sessions_dir()
    now = int(cul.time.time())
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-a.jsonl", [
        tc_event("2026-08-31T09:00:00.000Z", 5.0, now + 3600),
        '{"rate_limits": обрубок',
        '{"type":"event_msg","payload":{"type":"token_count","rate_limits":{}}}',
    ])
    r = cul.collect_sessions()
    assert r["ok"] and r["five_hour"]["used_percent"] == 5.0, r
check("codex-local: битые строки и пустой rate_limits пропускаются", t_cul_garbage_lines)

def t_cul_truncated_tail():
    root = sessions_dir()
    now = int(cul.time.time())
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-a.jsonl", [
        tc_event("2026-08-31T09:00:00.000Z", 1.0, now + 3600),
        tc_event("2026-08-31T09:30:00.000Z", 6.0, now + 3600),
    ])
    saved = cul.TAIL_BYTES
    try:
        cul.TAIL_BYTES = 400   # хвост режет первую строку посередине
        r = cul.collect_sessions()
    finally:
        cul.TAIL_BYTES = saved
    assert r["ok"] and r["five_hour"]["used_percent"] == 6.0, r
check("codex-local: обрезанная строка в хвосте не ломает разбор", t_cul_truncated_tail)

def t_cul_no_sessions():
    cul.SESSIONS_DIR = "/nonexistent/codex/sessions"
    assert cul.collect_sessions() == {"ok": False, "host": "mac",
                             "collected_at": cul.collect_sessions()["collected_at"], "error": "no_sessions"}
    root = sessions_dir()
    os.makedirs(root, exist_ok=True)
    assert cul.collect_sessions()["error"] == "no_sessions"
check("codex-local: нет журналов → no_sessions, без падения", t_cul_no_sessions)

def t_cul_writes_snapshot_collector_reads():
    root = sessions_dir()
    now = int(cul.time.time())
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-a.jsonl",
                  [tc_event("2026-08-31T09:00:00.000Z", 8.0, now + 3600)])
    dst = tmp_cache("{}")
    cul.DST = dst
    saved = cul.codex_binary
    cul.codex_binary = lambda: None
    try:
        cul.main()
    finally:
        cul.codex_binary = saved
    col.CODEX_SNAPSHOT = dst
    r = col.get_codex()
    assert r["ok"] and r["host"] == "mac" and r["label"] == "Codex Plus", r
    assert r["five_hour"]["used_percentage"] == 8.0 and r["five_hour"]["resets_hm"], r
    assert r["source"] == "sessions" and r["app_server_error"], r
check("codex-local: срез пишется и читается collector'ом без правок", t_cul_writes_snapshot_collector_reads)

# ---------- app-server: подставной бинарник вместо настоящего Codex ----------
import stat, textwrap, time as _time
real_codex_binary = cul.codex_binary

def fake_codex(body):
    """Исполняемый скрипт-«codex»: поведение — в body (Python)."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "codex")
    with open(p, "w") as f:
        f.write("#!" + sys.executable + "\nimport sys, json, time\n" + textwrap.dedent(body))
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return p

RPC_HEAD = """
def out(o):
    print(json.dumps(o), flush=True)
for line in sys.stdin:
    if json.loads(line).get("id") == 2:
        break
"""

def rl_response(primary_reset, secondary_reset, **extra):
    r = {"ordinaryUsageAllowed": True,
         "rateLimits": {"limitId": "codex",
                        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": primary_reset},
                        "secondary": {"usedPercent": 65, "windowDurationMins": 10080, "resetsAt": secondary_reset},
                        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                        "spendControlReached": False, "planType": "plus", "rateLimitReachedType": None},
         "rateLimitsByLimitId": {}}
    r.update(extra)
    return r

def fake_ok(result, notifications=0):
    return fake_codex(RPC_HEAD + f"""
for i in range({notifications}):
    out({{"method": "account/updated", "params": {{"n": i}}}})
out({{"id": 1, "result": {{}}}})
out({{"id": 2, "result": json.loads({json.dumps(json.dumps(result))})}})
time.sleep(30)
""")

def t_as_normal():
    now = int(cul.time.time())
    cul.codex_binary = lambda b=fake_ok(rl_response(now + 3600, now + 90000)): b
    t0 = _time.time()
    r = cul.collect()
    assert _time.time() - t0 < 10, "ответ получен, но ждали таймаут"
    assert r["ok"] and r["source"] == "app-server" and r["plan"] == "plus", r
    assert r["weekly"]["used_percent"] == 65 and r["weekly"]["window_minutes"] == 10080, r
    assert r["five_hour"]["resets_at"] == now + 3600 and r["five_hour"]["rolled_over"] is False, r
    assert r["credits"] == {"has_credits": False, "unlimited": False, "balance": "0"}, r
    assert r["limit_reached"] is None and "error" not in r, r
    m = col.datetime.datetime.fromisoformat(r["measured_at"].replace("Z", "+00:00")).timestamp()
    assert abs(m - now) < 10, r["measured_at"]
check("app-server: нормальный ответ → срез в прежней схеме", t_as_normal)

def t_as_notifications_first():
    now = int(cul.time.time())
    cul.codex_binary = lambda b=fake_ok(rl_response(now + 3600, now + 90000), notifications=5): b
    r = cul.collect()
    assert r["ok"] and r["source"] == "app-server" and r["weekly"]["used_percent"] == 65, r
check("app-server: уведомления до ответа пропускаются", t_as_notifications_first)

def t_as_error_response_fallback():
    root = sessions_dir()
    now = int(cul.time.time())
    write_rollout(os.path.join(root, "2026", "08", "31"), "rollout-a.jsonl",
                  [tc_event("2026-08-31T09:00:00.000Z", 8.0, now + 3600)])
    cul.codex_binary = lambda b=fake_codex(RPC_HEAD + 'out({"id": 2, "error": {"code": -32000, "message": "not signed in"}})\n'): b
    r = cul.collect()
    assert r["ok"] and r["source"] == "sessions", r
    assert "not signed in" in r["error"], r
    assert r["five_hour"]["used_percent"] == 8.0, r
check("app-server: ответ с ошибкой → журналы сессий и причина в error", t_as_error_response_fallback)

def t_as_timeout_kills_child():
    sessions_dir()
    marker = os.path.join(tempfile.mkdtemp(), "pid")
    cul.codex_binary = lambda b=fake_codex(f"""
import os
open({marker!r}, "w").write(str(os.getpid()))
time.sleep(300)
"""): b
    orig = cul.app_server_rate_limits
    cul.app_server_rate_limits = lambda binary: orig(binary, 1.5)
    try:
        t0 = _time.time()
        r = cul.collect()
    finally:
        cul.app_server_rate_limits = orig
    assert _time.time() - t0 < 10, "таймаут не сработал"
    assert r["ok"] is False and r["error"] == "no_sessions" and r["app_server_error"] == "timeout", r
    pid = int(open(marker).read())
    _time.sleep(0.3)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "дочерний процесс остался жить"
check("app-server: таймаут → процесс убит, фолбэк без падения", t_as_timeout_kills_child)

def t_as_child_exits_early():
    sessions_dir()
    cul.codex_binary = lambda b=fake_codex("sys.exit(3)\n"): b
    r = cul.collect()
    assert r["ok"] is False and r["app_server_error"], r
check("app-server: процесс умер без ответа → фолбэк", t_as_child_exits_early)

def t_as_no_binary():
    sessions_dir()
    cul.codex_binary = lambda: None
    r = cul.collect()
    assert r["ok"] is False and "не найден" in r["app_server_error"], r
check("app-server: нет бинарника → фолбэк", t_as_no_binary)

def t_as_past_resets_at():
    now = int(cul.time.time())
    cul.codex_binary = lambda b=fake_ok(rl_response(now - 100, now - 200)): b
    r = cul.collect()
    assert r["source"] == "app-server", r
    for k in ("five_hour", "weekly"):
        assert r[k]["rolled_over"] is True and r[k]["used_percent"] == 0 and r[k]["resets_at"] is None, r[k]
check("app-server: resetsAt в прошлом → ноль и rolled_over", t_as_past_resets_at)

def t_as_reset_credits():
    now = int(cul.time.time())
    credits = {"availableCount": 3, "credits": [
        {"id": "a", "status": "available", "expiresAt": now + 9000},
        {"id": "b", "status": "available", "expiresAt": now + 5000},
        {"id": "c", "status": "used", "expiresAt": now + 100},
        {"id": "d", "status": "available", "expiresAt": now + 7000}]}
    cul.codex_binary = lambda b=fake_ok(rl_response(now + 3600, now + 90000, rateLimitResetCredits=credits)): b
    assert cul.collect()["reset_credits"] == {"available": 3, "nearest_expiry": now + 5000}
    empty = {"availableCount": 0, "credits": []}
    cul.codex_binary = lambda b=fake_ok(rl_response(now + 3600, now + 90000, rateLimitResetCredits=empty)): b
    assert cul.collect()["reset_credits"] == {"available": 0, "nearest_expiry": None}
    cul.codex_binary = lambda b=fake_ok(rl_response(now + 3600, now + 90000)): b
    assert cul.collect()["reset_credits"] is None
check("app-server: reset_credits — число и ближайший срок только среди available", t_as_reset_credits)

def t_as_limit_reached():
    now = int(cul.time.time())
    rr = rl_response(now + 3600, now + 90000)
    rr["rateLimits"]["rateLimitReachedType"] = "primary"
    cul.codex_binary = lambda b=fake_ok(rr): b
    assert cul.collect()["limit_reached"] == "primary"
    rr["rateLimits"]["rateLimitReachedType"] = None
    rr["rateLimits"]["spendControlReached"] = True
    cul.codex_binary = lambda b=fake_ok(rr): b
    assert cul.collect()["limit_reached"] == "spend_control"
check("app-server: limit_reached из rateLimitReachedType / spendControlReached", t_as_limit_reached)

def t_as_binary_version_order():
    d = tempfile.mkdtemp()
    for v in ("26.917.62051", "26.930.21537", "26.928.31416", "26.99.1"):
        bd = os.path.join(d, f"openai.chatgpt-{v}-darwin-arm64", "bin", "macos-aarch64")
        os.makedirs(bd)
        p = os.path.join(bd, "codex"); open(p, "w").write("x"); os.chmod(p, 0o755)
    saved = cul.VSCODE_EXT_GLOB
    cul.VSCODE_EXT_GLOB = os.path.join(d, "openai.chatgpt-*-darwin-*", "bin", "macos-*", "codex")
    try:
        assert "26.930.21537" in real_codex_binary(), real_codex_binary()
    finally:
        cul.VSCODE_EXT_GLOB = saved
check("app-server: бинарник — новейшая версия расширения, по числам, не по строке", t_as_binary_version_order)

def t_as_snapshot_to_collector():
    now = int(cul.time.time())
    credits = {"availableCount": 2, "credits": [{"status": "available", "expiresAt": now + 5000}]}
    cul.codex_binary = lambda b=fake_ok(rl_response(now + 3600, now + 90000, rateLimitResetCredits=credits)): b
    dst = tmp_cache("{}")
    cul.DST = dst
    cul.main()
    col.CODEX_SNAPSHOT = dst
    r = col.get_codex()
    assert r["ok"] and r["label"] == "Codex Plus" and r["source"] == "app-server", r
    assert r["seven_day"]["used_percentage"] == 65 and r["age_seconds"] < 30, r
    assert r["reset_credits"] == {"available": 2, "nearest_expiry": now + 5000}, r
    assert r["app_server_error"] is None, r
check("app-server: collector видит свежий срез (age ~0), reset_credits и source проходят", t_as_snapshot_to_collector)

# ---------- НАСТРОЙКИ, ВХОД, «НЕТ ДОСТУПА» (на свежей копии модуля: выше тесты подменяют функции навсегда) ----------
def fresh():
    sp = importlib.util.spec_from_file_location("collector_fresh", os.path.join(HERE, "collector.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    d = tempfile.mkdtemp()
    m.USAGE_DIR = d
    m.SETTINGS_PATH = os.path.join(d, "settings.json")
    m.ACCESS_STATE = os.path.join(d, "access-state.json")
    m.PENDING_PATH = os.path.join(d, ".oauth-pending.json")
    m.LIMITS_CACHE = os.path.join(d, "limits-cache.json")
    m._mirror_active_account = lambda cur: None     # иначе тест пишет живые креды Claude Code в настоящий Keychain
    return m, d

def t_settings_defaults_when_missing():
    m, d = fresh()
    s = m.load_settings()
    assert s == m.DEFAULT_SETTINGS and s is not m.DEFAULT_SETTINGS, s
    assert s["general"]["poll_minutes"] == 5 and s["accounts"]["hidden"] == []
check("settings: нет файла → значения по умолчанию", t_settings_defaults_when_missing)

def t_settings_partial_merge_and_garbage():
    m, d = fresh()
    json.dump({"general": {"poll_minutes": 10, "color_warn": "много"}, "accounts": {"hidden": ["CCMonitor-account-2"]},
               "левый_ключ": 1, "menubar": "не объект"}, open(m.SETTINGS_PATH, "w"))
    s = m.load_settings()
    assert s["general"]["poll_minutes"] == 10, s
    assert s["general"]["color_warn"] == 70, "значение не того типа → умолчание"
    assert s["accounts"]["hidden"] == ["CCMonitor-account-2"] and s["accounts"]["order"] == [], s
    assert s["menubar"] == m.DEFAULT_SETTINGS["menubar"], s
    open(m.SETTINGS_PATH, "w").write("{ битый")
    assert m.load_settings() == m.DEFAULT_SETTINGS
    json.dump({"general": {"show_cost": 1}}, open(m.SETTINGS_PATH, "w"))
    assert m.load_settings()["general"]["show_cost"] is True, "int вместо bool — умолчание"
check("settings: частичный файл, чужие ключи, битый JSON, типы", t_settings_partial_merge_and_garbage)

def t_poll_ttl_minimum_5_minutes():
    m, d = fresh()
    assert m.LIMITS_TTL == 300
    for minutes, want in ((10, 600), (15, 900), (1, 300), (0, 300), (5, 300), ("x", 300)):
        s = m.load_settings(); s["general"]["poll_minutes"] = minutes
        m.apply_settings(s)
        assert m.LIMITS_TTL == want, (minutes, m.LIMITS_TTL)
check("settings: poll_minutes → LIMITS_TTL, не меньше 5 минут", t_poll_ttl_minimum_5_minutes)

def t_poll_ttl_respected_by_get_limits():
    m, d = fresh()
    cache = os.path.join(d, "c.json")
    json.dump({"_fetched_at": m.time.time() - 400,
               "data": {"ok": True, "five_hour": {"used_percentage": 9}, "seven_day": None}}, open(cache, "w"))
    calls = []
    m._fetch_limits = lambda *_: calls.append(1) or {"ok": True, "five_hour": {"used_percentage": 20}, "seven_day": None}
    s = m.load_settings(); s["general"]["poll_minutes"] = 10; m.apply_settings(s)
    r = m.get_limits(cache, lambda: "t")
    assert not calls and r["stale"] is False and r["five_hour"]["used_percentage"] == 9, "400с < 10 мин — запроса быть не должно"
    s["general"]["poll_minutes"] = 5; m.apply_settings(s)
    m.get_limits(cache, lambda: "t")
    assert calls, "400с > 5 мин — запрос обязан уйти"
check("settings: кэш 400с при опросе 10 мин не обновляется, при 5 мин — обновляется", t_poll_ttl_respected_by_get_limits)

def _creds(email, **extra):
    return {"access_token": "x", "expires_at": 9_999_999_999, "account": {"email_address": email}, **extra}

def t_hidden_account_not_polled():
    m, d = fresh()
    store = {"CCMonitor-account-1": _creds("a@x.com", label="Max 20x"),
             "CCMonitor-account-2": _creds("b@x.com", label="Max", refresh_expires_at=1792955077)}
    m._keychain_read = lambda s: store.get(s)
    polled = []
    m.get_limits = lambda cache=None, tg=None: polled.append(cache) or {"ok": True, "five_hour": {"used_percentage": 1}}
    m.active_email = lambda: "a@x.com"
    s = m.load_settings(); s["accounts"]["hidden"] = ["CCMonitor-account-2"]
    accs = m.collect_accounts(s)
    assert len(polled) == 1 and polled[0].endswith("limits-1.json"), polled
    b = next(a for a in accs if a["id"] == "CCMonitor-account-2")
    assert b["hidden"] is True and b["limits"]["error"] == "hidden" and b["refresh_expires_local"] == "25.10", b
    assert next(a for a in accs if a["id"] == "CCMonitor-account-1")["hidden"] is False
check("hidden: скрытая подписка не опрашивается, но остаётся в выдаче (для окна настроек)", t_hidden_account_not_polled)

def t_hidden_active_still_polled():
    m, d = fresh()
    m._keychain_read = lambda s: _creds("a@x.com") if s == "CCMonitor-account-1" else None
    polled = []
    m.get_limits = lambda cache=None, tg=None: polled.append(cache) or {"ok": True, "five_hour": {"used_percentage": 1}}
    m.active_email = lambda: "A@x.com"
    s = m.load_settings(); s["accounts"]["hidden"] = ["CCMonitor-account-1"]
    accs = m.collect_accounts(s)
    assert len(polled) == 1 and accs[0]["active"] and accs[0]["hidden"] is False, accs
check("hidden: активную подписку скрыть нельзя — статусбару она нужна", t_hidden_active_still_polled)

def t_extended_account_slots():
    m, d = fresh()
    assert m.ACCOUNT_SERVICES[0] == "CCMonitor-account-1" and m.ACCOUNT_SERVICES[-1] == "CCMonitor-account-9"
    assert len(m.ACCOUNT_SERVICES) == 9
check("accounts: ищем CCMonitor-account-1…9", t_extended_account_slots)

def t_access_state_tracking():
    m, d = fresh()
    st = {}
    assert m.update_access(st, "k", {"ok": False, "error": "relogin_needed"}, 1000) == 1000
    assert m.update_access(st, "k", {"ok": False, "error": "relogin_needed"}, 5000) == 1000, "первое время не перезаписывается"
    assert m.update_access(st, "k", {"ok": False, "error": "network"}, 6000) == 1000, "сеть доступа не восстанавливает"
    assert m.update_access(st, "k", {"ok": True, "stale": True, "last_error": "http_429"}, 7000) == 1000
    assert m.update_access(st, "k", {"ok": True, "stale": False}, 8000) is None and st == {}, "живые данные сбрасывают отметку"
    assert m.update_access(st, "q", {"ok": True, "stale": True, "last_error": "token_expired"}, 9000) == 9000, "кэш с 401"
    assert m.update_access(st, "z", {"ok": False, "error": "http_500"}, 9000) is None
check("access: отметка «нет доступа с» ставится один раз и снимается живыми данными", t_access_state_tracking)

def t_access_autohide_after_3_days_in_accounts():
    m, d = fresh()
    now = m.time.time()
    json.dump({"CCMonitor-account-1": int(now - 4 * 86400), "CCMonitor-account-2": int(now - 2 * 86400)},
              open(m.ACCESS_STATE, "w"))
    m._keychain_read = lambda s: (_creds("a@x.com") if s == "CCMonitor-account-1" else
                                  _creds("b@x.com") if s == "CCMonitor-account-2" else None)
    m.get_limits = lambda cache=None, tg=None: {"ok": False, "error": "relogin_needed"}
    m.active_email = lambda: "other@x.com"
    accs = {a["id"]: a for a in m.collect_accounts()}
    assert accs["CCMonitor-account-1"]["auto_hidden"] is True, accs["CCMonitor-account-1"]
    assert accs["CCMonitor-account-2"]["auto_hidden"] is False
    since = accs["CCMonitor-account-1"]["no_access_since"]
    assert accs["CCMonitor-account-1"]["no_access_since_local"] == m.datetime.datetime.fromtimestamp(since).strftime("%d.%m")
    assert json.load(open(m.ACCESS_STATE))["CCMonitor-account-1"] == since, "метка не должна сдвигаться"
check("access: без доступа > 3 дней → auto_hidden, дата в выдаче, метка стабильна", t_access_autohide_after_3_days_in_accounts)

def t_access_recovers():
    m, d = fresh()
    json.dump({"CCMonitor-account-1": 100}, open(m.ACCESS_STATE, "w"))
    m._keychain_read = lambda s: _creds("a@x.com") if s == "CCMonitor-account-1" else None
    m.get_limits = lambda cache=None, tg=None: {"ok": True, "stale": False, "five_hour": {"used_percentage": 1}}
    m.active_email = lambda: "a@x.com"
    a = m.collect_accounts()[0]
    assert a["no_access_since"] is None and a["auto_hidden"] is False
    assert json.load(open(m.ACCESS_STATE)) == {}
check("access: после успешного опроса отметка стирается", t_access_recovers)

def t_codex_window_has_absolute_reset():
    m, d = fresh()
    w = m._codex_window({"used_percent": 3, "resets_at": 1_790_991_986, "window_minutes": 300})
    assert w["resets_at_utc"] == "2026-10-03T01:46:26+00:00", w
    assert m._codex_window({"used_percent": 0, "resets_at": None})["resets_at_utc"] is None
check("codex: окно несёт абсолютный resets_at_utc (ключ для «один раз за окно»)", t_codex_window_has_absolute_reset)

# ---- вход
def t_login_url_pkce_and_pending_mode():
    m, d = fresh()
    url = m.login_url()
    u = m.urllib.parse.urlparse(url); q = m.urllib.parse.parse_qs(u.query)
    assert url.startswith("https://claude.com/cai/oauth/authorize?") and q["client_id"] == [m.OAUTH_CLIENT_ID], url
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["user:inference user:profile"], q
    assert q["redirect_uri"] == [m.OAUTH_REDIRECT] and q["response_type"] == ["code"], q
    pend = json.load(open(m.PENDING_PATH))
    chal = m.base64.urlsafe_b64encode(m.hashlib.sha256(pend["verifier"].encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"] == [chal] and q["state"] == [pend["state"]], "challenge = S256(verifier)"
    assert oct(os.stat(m.PENDING_PATH).st_mode & 0o777) == "0o600", oct(os.stat(m.PENDING_PATH).st_mode)
    assert "verifier" not in url, "секрет PKCE не должен попасть в ссылку"
    m.login_url()
    assert json.load(open(m.PENDING_PATH))["verifier"] != pend["verifier"], "новая ссылка — новая пара"
check("login: ссылка с PKCE S256, pending сохранён с правами 600", t_login_url_pkce_and_pending_mode)

def _login_env(m, store=None):
    store = {} if store is None else store
    wrote = {}
    m._keychain_read = lambda s: store.get(s)
    m._keychain_write = lambda s, p: wrote.__setitem__(s, p) or store.__setitem__(s, p) or True
    m.account_tier = lambda svc, tok: "default_claude_max_20x"
    return store, wrote

def t_login_code_exchange_and_first_free_slot():
    m, d = fresh()
    store, wrote = _login_env(m, {"CCMonitor-account-2": _creds("other@x.com")})
    m.login_url(); pend = json.load(open(m.PENDING_PATH))
    seen = {}
    def fake_exchange(code, state, verifier):
        seen.update(code=code, state=state, verifier=verifier)
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 28800,
                "refresh_token_expires_in": 2419200, "account": {"email_address": "New@x.com"}}
    m._oauth_exchange = fake_exchange
    r = m.login_code(f'  "CODE123#{pend["state"]}"\n', "auto")
    assert seen == {"code": "CODE123", "state": pend["state"], "verifier": pend["verifier"]}, seen
    assert r == {"ok": True, "service": "CCMonitor-account-1", "email": "New@x.com",
                 "label": "Max 20x", "tier": "default_claude_max_20x"}, r
    w = wrote["CCMonitor-account-1"]
    assert w["refresh_token"] == "RT" and w["expires_at"] > m.time.time() and w["refresh_expires_at"] > m.time.time(), w
    assert not os.path.exists(m.PENDING_PATH), "pending стирается после обмена"
check("login: код из «код#state» → обмен с verifier, запись в первый свободный слот", t_login_code_exchange_and_first_free_slot)

def t_login_code_relogin_and_same_email():
    m, d = fresh()
    store, wrote = _login_env(m, {"CCMonitor-account-1": _creds("a@x.com"), "CCMonitor-account-2": _creds("b@x.com")})
    open(os.path.join(d, "limits-2.json"), "w").write("{}")
    json.dump({"CCMonitor-account-2": 5}, open(m.ACCESS_STATE, "w"))
    m._oauth_exchange = lambda c, s, v: {"access_token": "AT", "account": {"email_address": "x@x.com"}}
    m.login_url()
    r = m.login_code("C#" + json.load(open(m.PENDING_PATH))["state"], "CCMonitor-account-2")
    assert r["service"] == "CCMonitor-account-2" and "CCMonitor-account-2" in wrote and "CCMonitor-account-1" not in wrote
    assert not os.path.exists(os.path.join(d, "limits-2.json")), "кэш старого входа стирается"
    assert json.load(open(m.ACCESS_STATE)) == {}, "отметка «нет доступа» стирается"
    # тот же e-mail, что уже заведён — не плодим дубль
    m._oauth_exchange = lambda c, s, v: {"access_token": "AT", "account": {"email_address": "A@X.com"}}
    m.login_url()
    r = m.login_code("C2", "auto")
    assert r["service"] == "CCMonitor-account-1", r
check("login: перевход в выбранный сервис; тот же e-mail при auto — обновляет слот, без дубля", t_login_code_relogin_and_same_email)

def t_login_code_errors():
    m, d = fresh()
    _login_env(m, {})
    m._oauth_exchange = lambda *a: (_ for _ in ()).throw(AssertionError("до сети дойти не должно"))
    def err(pasted, target="auto"):
        try:
            m.login_code(pasted, target); return "no error"
        except m.LoginError as e:
            return str(e)
    assert err("abc") == "no_pending"
    m.login_url()
    assert err("abc", "../../etc") == "bad_service"
    assert err("abc", "CCMonitor-account-10") == "bad_service"
    assert err("   ") == "bad_code"
    assert err("abc#чужой-state") == "state_mismatch"
    p = json.load(open(m.PENDING_PATH)); p["created_at"] -= 4000
    json.dump(p, open(m.PENDING_PATH, "w"))
    assert err("abc") == "pending_expired"
    m.login_url()
    m._oauth_exchange = lambda *a: (_ for _ in ()).throw(m.LoginError("exchange_failed: HTTP 400 invalid_grant"))
    assert err("abc").startswith("exchange_failed")
    assert os.path.exists(m.PENDING_PATH), "при отказе обмена pending остаётся для повторной попытки"
    m._oauth_exchange = lambda *a: {"access_token": "AT", "account": {"email_address": "n@x.com"}}
    m._keychain_read = lambda s: _creds("e%s@x.com" % s[-1])        # все девять слотов заняты
    assert err("abc") == "no_free_slot"
    m._keychain_read = lambda s: None
    m._keychain_write = lambda s, p: False
    assert err("abc") == "keychain_failed"
check("login: ошибки — нет pending, чужой state, просрочен, отказ обмена, нет слота, Keychain", t_login_code_errors)

def t_login_code_accepts_full_url():
    m, d = fresh()
    store, wrote = _login_env(m)
    m.login_url(); st = json.load(open(m.PENDING_PATH))["state"]
    got = {}
    m._oauth_exchange = lambda c, s, v: got.update(c=c, s=s) or {"access_token": "AT", "account": {"email_address": "n@x.com"}}
    m.login_code(f"https://platform.claude.com/oauth/code/callback?code=ZZZ&state={st}", "auto")
    assert got == {"c": "ZZZ", "s": st}, got
check("login: вставленная целиком ссылка тоже разбирается", t_login_code_accepts_full_url)

def t_delete_account():
    m, d = fresh()
    calls = []
    class R: returncode = 0
    n = {"i": 0}
    def fake_run(cmd, **kw):
        calls.append(cmd); n["i"] += 1
        r = R(); r.returncode = 0 if n["i"] < 2 else 44
        return r
    real_run, m.subprocess.run = m.subprocess.run, fake_run
    open(os.path.join(d, "limits-3.json"), "w").write("{}")
    json.dump({"CCMonitor-account-3": 1, "CCMonitor-account-2": 2}, open(m.ACCESS_STATE, "w"))
    try:
        assert m.delete_account("CCMonitor-account-3") == {"ok": True, "service": "CCMonitor-account-3"}
    finally:
        m.subprocess.run = real_run
    assert all(c[:2] == ["security", "delete-generic-password"] and c[-1] == "CCMonitor-account-3" for c in calls), calls
    assert len(calls) == 2, "повторяем до «не найдено» — на случай дублей"
    assert not os.path.exists(os.path.join(d, "limits-3.json"))
    assert json.load(open(m.ACCESS_STATE)) == {"CCMonitor-account-2": 2}
    calls.clear()
    m.subprocess.run = fake_run
    try:
        for bad in ("Claude Code-credentials", "CCMonitor-account-0", "CCMonitor-account-1; rm", ""):
            try:
                m.delete_account(bad); assert False, bad
            except m.LoginError:
                pass
    finally:
        m.subprocess.run = real_run
    assert calls == [], "чужие сервисы (в т.ч. Claude Code-credentials) стирать нельзя"
check("delete: стирает только CCMonitor-account-N (Keychain + кэш + отметка); чужое отвергает", t_delete_account)

def t_cli_modes_subprocess():
    home = tempfile.mkdtemp()
    env = {**os.environ, "HOME": home}
    r = subprocess.run(["/usr/bin/python3", os.path.join(HERE, "collector.py"), "--login-url"],
                       capture_output=True, env=env, timeout=30)
    assert r.returncode == 0 and r.stdout.decode().startswith("https://claude.com/cai/oauth/authorize?"), r
    pend = os.path.join(home, ".claude", "usage-monitor", ".oauth-pending.json")
    assert oct(os.stat(pend).st_mode & 0o777) == "0o600"
    r = subprocess.run(["/usr/bin/python3", os.path.join(HERE, "collector.py"), "--login-code", "../x"],
                       input=b"secret-code#state", capture_output=True, env=env, timeout=30)
    assert r.returncode == 1 and json.loads(r.stdout) == {"ok": False, "error": "bad_service"}, r.stdout
    r = subprocess.run(["/usr/bin/python3", os.path.join(HERE, "collector.py"), "--login-code", "auto"],
                       input=b"abc#" + b"x" * 8, capture_output=True, env=env, timeout=30)
    assert json.loads(r.stdout)["error"] == "state_mismatch", r.stdout
    r = subprocess.run(["/usr/bin/python3", os.path.join(HERE, "collector.py"), "--delete", "nope"],
                       capture_output=True, env=env, timeout=30)
    assert r.returncode == 1 and json.loads(r.stdout)["error"] == "bad_service"
check("cli: --login-url / --login-code (stdin) / --delete через настоящий процесс, HOME во временной папке", t_cli_modes_subprocess)

def t_main_output_shape_with_settings():
    home = tempfile.mkdtemp()
    um = os.path.join(home, ".claude", "usage-monitor"); os.makedirs(um)
    json.dump({"general": {"poll_minutes": 15}, "accounts": {"hidden": ["CCMonitor-account-2"]}}, open(os.path.join(um, "settings.json"), "w"))
    r = subprocess.run(["/usr/bin/python3", os.path.join(HERE, "collector.py")],
                       capture_output=True, env={**os.environ, "HOME": home}, timeout=60)
    d = json.loads(r.stdout)
    assert set(d) >= {"generated_at", "plan", "limits", "tokens", "accounts", "codex"}, list(d)
    for a in d["accounts"]:
        assert set(a) >= {"id", "email", "label", "active", "hidden", "limits", "no_access_since", "auto_hidden",
                          "refresh_expires_at", "refresh_expires_local"}, a
check("e2e: форма выдачи со всеми новыми полями аккаунтов", t_main_output_shape_with_settings)

def t_mirror_keeps_switched_away_account():
    store = {}
    cc = {"accessToken": "A20", "refreshToken": "R20", "expiresAt": 1791220688703,
          "refreshTokenExpiresAt": 1793000000000, "scopes": ["user:profile", "user:inference"],
          "rateLimitTier": "default_claude_max_20x"}
    col._keychain_read = lambda s: json.loads(json.dumps(store[s])) if s in store else None
    col._keychain_write = lambda svc, payload: store.__setitem__(svc, json.loads(json.dumps(payload))) or True
    col.read_credentials = lambda: cc
    store["CCMonitor-account-2"] = {"access_token": "A5", "refresh_token": "R5",
                                    "account": {"email_address": "five@x.com"}}
    col._mirror_active_account("twenty@x.com")
    mirror = store["CCMonitor-account-1"]
    assert mirror["account"]["email_address"] == "twenty@x.com" and mirror["mirror"] is True, mirror
    assert mirror["refresh_token"] == "R20" and mirror["expires_at"] == 1791220688, mirror
    assert "mirror" not in store["CCMonitor-account-2"], "чужой слот не трогаем"
    cc.update(accessToken="A20b", refreshToken="R20b")
    col._mirror_active_account("twenty@x.com")
    assert store["CCMonitor-account-1"]["refresh_token"] == "R20b", "зеркало идёт за ротацией"
    cc.update(accessToken="A5c", refreshToken="R5c")
    col._mirror_active_account("five@x.com")
    s1 = store["CCMonitor-account-1"]
    assert "mirror" not in s1 and s1["refresh_token"] == "R20b", s1
    assert store["CCMonitor-account-2"]["refresh_token"] == "R5", "свою цепочку (не зеркало) не перезаписываем"
check("зеркало: смена аккаунта в Claude Code не теряет прежнюю подписку", t_mirror_keeps_switched_away_account)

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
