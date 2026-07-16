#!/usr/bin/env python3
"""
CCMonitor collector — единый источник данных для menu bar приложения.

Отдаёт JSON со всем нужным:
  - limits: официальные лимиты 5ч/7д (utilization %, время сброса) из Anthropic oauth/usage
  - tokens: токены за текущий 5-часовой блок и за активную сессию (из JSONL, с дедупом)

Запуск:  python3 collector.py           -> печатает JSON в stdout
         python3 collector.py --pretty  -> человекочитаемо

Логика данных намеренно вся здесь (Python), чтобы её было легко читать и править.
Swift-приложение просто вызывает этот скрипт и рисует результат.
"""
import subprocess, json, os, sys, glob, time, datetime, urllib.request, urllib.error

CLAUDE_DIR = os.path.expanduser("~/.claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BLOCK_HOURS = 5

# Цены за 1М токенов (USD), сверено со справочником Claude API на 07.2026.
# Стоимость справочная: при активной подписке эти суммы не списываются.
# Множители кэша: запись 5м = 1.25x input, запись 1ч = 2x input, чтение = 0.1x input.
# Ключ подбирается по вхождению подстроки в id модели.
PRICING = {
    "opus-4-8":  {"in": 5.0,  "out": 25.0, "cw5m": 6.25, "cw1h": 10.0, "cr": 0.50},
    "opus-4-7":  {"in": 5.0,  "out": 25.0, "cw5m": 6.25, "cw1h": 10.0, "cr": 0.50},
    "opus-4-6":  {"in": 5.0,  "out": 25.0, "cw5m": 6.25, "cw1h": 10.0, "cr": 0.50},
    "opus-4-5":  {"in": 5.0,  "out": 25.0, "cw5m": 6.25, "cw1h": 10.0, "cr": 0.50},
    "opus-4":    {"in": 15.0, "out": 75.0, "cw5m": 18.75,"cw1h": 30.0, "cr": 1.50},
    "sonnet":    {"in": 3.0,  "out": 15.0, "cw5m": 3.75, "cw1h": 6.0,  "cr": 0.30},
    "haiku":     {"in": 1.0,  "out": 5.0,  "cw5m": 1.25, "cw1h": 2.0,  "cr": 0.10},
    "fable":     {"in": 10.0, "out": 50.0, "cw5m": 12.50,"cw1h": 20.0, "cr": 1.00},
}
def price_for(model):
    m = (model or "").lower()
    for key in PRICING:
        if key in m:
            return PRICING[key]
    return PRICING["opus-4-8"]  # разумный дефолт

# ---------------------------------------------------------------- ЛИМИТЫ
def get_token():
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        stderr=subprocess.DEVNULL).decode().strip()
    oauth = json.loads(raw).get("claudeAiOauth") or json.loads(raw)
    return oauth.get("accessToken") or oauth.get("access_token")

LIMITS_CACHE = os.path.join(CLAUDE_DIR, "usage-monitor", "limits-cache.json")
LIMITS_TTL = 120  # сек: usage-endpoint строго rate-limited, опрашиваем не чаще раза в 2 мин

def _human_dur(s):
    if s is None:
        return None
    s = max(s, 0)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d} дн {h} ч"
    if h:
        return f"{h} ч {m:02d} мин"
    return f"{m} мин"

def _refresh_countdown(win):
    # Пересчитываем обратный отсчёт от АБСОЛЮТНОГО resets_at_utc относительно «сейчас»,
    # чтобы даже устаревший кэш показывал верное время до сброса (а не замороженное).
    if not isinstance(win, dict) or not win.get("resets_at_utc"):
        return win
    try:
        reset = datetime.datetime.fromisoformat(win["resets_at_utc"])
    except Exception:
        return win
    rem = max(int((reset - datetime.datetime.now(datetime.timezone.utc)).total_seconds()), 0)
    w = dict(win)
    w["resets_in_seconds"] = rem
    w["resets_in_human"] = _human_dur(rem)
    w["resets_in_short"] = f"{rem // 3600}:{(rem % 3600) // 60:02d}"
    return w

def _fresh_countdowns(resp):
    if resp.get("ok"):
        for key in ("five_hour", "seven_day"):
            if resp.get(key):
                resp = {**resp, key: _refresh_countdown(resp[key])}
    return resp

def get_limits():
    now = time.time()
    cached = None
    try:
        with open(LIMITS_CACHE) as f:
            cached = json.load(f)
    except Exception:
        cached = None
    if not isinstance(cached, dict):      # битый/не-объектный кэш игнорируем (защита от краша)
        cached = None
    cdata = cached.get("data") if cached else None
    cok = isinstance(cdata, dict) and cdata.get("ok")

    # свежий кэш — отдаём без запроса к API (троттлинг), пересчитав отсчёт
    if cok and (now - cached.get("_fetched_at", 0)) < LIMITS_TTL:
        return _fresh_countdowns({**cdata, "stale": False, "age_seconds": int(now - cached.get("_fetched_at", now))})
    # иначе запрашиваем свежее
    result = _fetch_limits()
    if result.get("ok"):
        try:
            os.makedirs(os.path.dirname(LIMITS_CACHE), exist_ok=True)
            with open(LIMITS_CACHE, "w") as f:
                json.dump({"_fetched_at": now, "data": result}, f, ensure_ascii=False)
        except Exception:
            pass
        return _fresh_countdowns({**result, "stale": False, "age_seconds": 0})
    # ошибка (429/сеть) — отдаём последний успешный кэш, пересчитав отсчёт
    if cok:
        return _fresh_countdowns({**cdata, "stale": True,
                "age_seconds": int(now - cached.get("_fetched_at", now)),
                "last_error": result.get("error")})
    return result

def _fetch_limits():
    try:
        tok = get_token()
    except Exception as e:
        return {"ok": False, "error": "no_token", "detail": str(e)}
    if not tok:
        return {"ok": False, "error": "no_token"}
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "CCMonitor/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        code = e.code
        return {"ok": False, "error": "token_expired" if code == 401 else f"http_{code}",
                "detail": "открой Claude Code, чтобы обновить токен" if code == 401 else ""}
    except Exception as e:
        return {"ok": False, "error": "network", "detail": str(e)}

    now = datetime.datetime.now(datetime.timezone.utc)
    def window(o):
        if not o:
            return None
        reset = datetime.datetime.fromisoformat(o["resets_at"]) if o.get("resets_at") else None
        rem = int((reset - now).total_seconds()) if reset else None
        rl = reset.astimezone() if reset else None
        return {
            "used_percentage": o.get("utilization"),
            "resets_at_utc": o.get("resets_at"),
            "resets_at_local": rl.isoformat() if rl else None,
            "resets_in_seconds": max(rem, 0) if rem is not None else None,
            "resets_hm": rl.strftime("%H:%M") if rl else None,       # "01:30"
            "resets_full": rl.strftime("%d.%m %H:%M") if rl else None,  # "20.07 19:00"
            "resets_in_human": _human_dur(rem),                      # "4 ч 02 мин"
            "resets_in_short": (f"{max(rem,0)//3600}:{(max(rem,0)%3600)//60:02d}"
                                if rem is not None else None),        # "4:02" для строки статусбара
        }
    # разбивка недельного лимита по моделям, если есть
    scoped = {}
    for lim in d.get("limits", []):
        if lim.get("kind") == "weekly_scoped" and lim.get("scope"):
            mdl = (lim["scope"].get("model") or {}).get("display_name")
            if mdl:
                scoped[mdl] = lim.get("percent")
    return {
        "ok": True,
        "five_hour": window(d.get("five_hour")),
        "seven_day": window(d.get("seven_day")),
        "weekly_scoped": scoped,
    }

# ---------------------------------------------------------------- ТОКЕНЫ (JSONL)
def floor_to_hour(ts_ms):
    ms_per_hour = 3600 * 1000
    return (ts_ms // ms_per_hour) * ms_per_hour

def parse_ts(iso):
    # '2026-07-15T18:16:16.987Z' -> epoch ms
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def iter_usage_entries(recent_hours=None):
    """Идём по JSONL всех проектов. Если recent_hours задан — только файлы,
    изменённые за это окно (для скорости в реалтайме)."""
    cutoff = time.time() - recent_hours * 3600 if recent_hours else None
    for pf in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if cutoff and os.path.getmtime(pf) < cutoff:
            continue
        try:
            with open(pf) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    msg = o.get("message") or {}
                    u = msg.get("usage")
                    if not u:
                        continue
                    model = msg.get("model")
                    if model == "<synthetic>":
                        continue
                    ts = parse_ts(o.get("timestamp", ""))
                    if ts is None:
                        continue
                    yield {
                        "mid": msg.get("id"),
                        "req": o.get("requestId"),
                        "sidechain": bool(o.get("isSidechain")),
                        "speed": u.get("speed") is not None,
                        "session": o.get("sessionId"),
                        "model": model,
                        "ts": ts,
                        "usage": u,
                    }
        except Exception:
            continue

def usage_total(u):
    return ((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0))

def dedupe(entries):
    """Дедуп по (message.id, requestId), оставляя запись с макс. суммой токенов
    (обходит баг стриминг-плейсхолдеров). Затем схлопывание sidechain-повторов
    по message.id, предпочитая не-sidechain."""
    best = {}
    for e in entries:
        if not e["mid"]:
            best[id(e)] = e  # без id не дедупим
            continue
        key = (e["mid"], e["req"])
        cur = best.get(key)
        if cur is None:
            best[key] = e; continue
        ct, et = usage_total(e["usage"]), usage_total(cur["usage"])
        if e["sidechain"] != cur["sidechain"]:
            if cur["sidechain"]:
                best[key] = e
        elif ct != et:
            if ct > et:
                best[key] = e
        elif e["speed"] and not cur["speed"]:
            best[key] = e
    # вторичный проход: sidechain replays с тем же mid, но разным req
    by_mid = {}
    for e in best.values():
        mid = e["mid"]
        if not mid:
            by_mid[id(e)] = e; continue
        cur = by_mid.get(mid)
        if cur is None or (cur["sidechain"] and not e["sidechain"]):
            by_mid[mid] = e
    return list(by_mid.values())

def expand_iterations(entries):
    """Каждый iteration (advisor и т.п.) — отдельная запись со своей моделью."""
    out = []
    for e in entries:
        u = e["usage"]
        out.append({"model": e["model"], "ts": e["ts"], "session": e["session"], "usage": u})
        for it in (u.get("iterations") or []):
            # у iteration свои токены и модель; тот же timestamp/сессия
            out.append({"model": it.get("model") or e["model"], "ts": e["ts"],
                        "session": e["session"], "usage": it})
    return out

def sum_tokens(records):
    agg = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "cost_usd": 0.0}
    by_model = {}
    for r in records:
        u = r["usage"]
        i = u.get("input_tokens") or 0
        o = u.get("output_tokens") or 0
        cc = u.get("cache_creation_input_tokens") or 0
        cr = u.get("cache_read_input_tokens") or 0
        # split cache_creation на 5m/1h для корректной цены
        ccd = u.get("cache_creation") or {}
        cc5 = ccd.get("ephemeral_5m_input_tokens")
        cc1 = ccd.get("ephemeral_1h_input_tokens")
        if cc5 is None and cc1 is None:
            cc5, cc1 = cc, 0
        else:
            cc5, cc1 = cc5 or 0, cc1 or 0
        p = price_for(r["model"])
        cost = (i * p["in"] + o * p["out"] + cc5 * p["cw5m"]
                + cc1 * p["cw1h"] + cr * p["cr"]) / 1_000_000
        agg["input"] += i; agg["output"] += o
        agg["cache_creation"] += cc; agg["cache_read"] += cr
        agg["cost_usd"] += cost
        m = by_model.setdefault(r["model"], {"input": 0, "output": 0, "cache_creation": 0,
                                             "cache_read": 0, "cost_usd": 0.0})
        m["input"] += i; m["output"] += o; m["cache_creation"] += cc
        m["cache_read"] += cr; m["cost_usd"] += cost
    agg["total"] = agg["input"] + agg["output"] + agg["cache_creation"] + agg["cache_read"]
    agg["by_model"] = by_model
    return agg

def get_tokens(limits=None):
    """Токены за текущее 5-часовое окно (= «сессия» в терминах Claude Code),
    суммарно по ВСЕМ чатам. Границы окна берём из серверного resets_at, если
    лимиты доступны (точное совпадение с баром), иначе реконструируем."""
    ms5 = BLOCK_HOURS * 3600 * 1000
    entries = list(iter_usage_entries(recent_hours=BLOCK_HOURS + 1))
    if not entries:
        return {"ok": True, "session_5h": None, "note": "нет активности за 5 часов"}
    records = expand_iterations(dedupe(entries))
    records.sort(key=lambda r: r["ts"])

    win_start = win_end = None
    fh = (limits or {}).get("five_hour") if (limits or {}).get("ok") else None
    if fh and fh.get("resets_at_utc"):
        try:
            reset = datetime.datetime.fromisoformat(fh["resets_at_utc"])
            win_end = int(reset.timestamp() * 1000)
            win_start = win_end - ms5
        except Exception:
            win_start = None
    if win_start is None:
        # fallback: активный блок — реконструкция от последней непрерывной активности
        bs = floor_to_hour(records[0]["ts"]); last = records[0]["ts"]
        for r in records[1:]:
            if r["ts"] - bs > ms5 or r["ts"] - last > ms5:
                bs = floor_to_hour(r["ts"])
            last = r["ts"]
        win_start, win_end = bs, bs + ms5

    in_win = [r for r in records if win_start <= r["ts"] < win_end]
    return {
        "ok": True,
        "session_5h": {
            "window_start_local": datetime.datetime.fromtimestamp(win_start/1000).astimezone().strftime("%H:%M"),
            "count": len(in_win),
            "tokens": sum_tokens(in_win),
        },
    }

# ---------------------------------------------------------------- MAIN
def main():
    limits = get_limits()
    result = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "limits": limits,
        "tokens": get_tokens(limits),
    }
    pretty = "--pretty" in sys.argv
    print(json.dumps(result, ensure_ascii=False, indent=2 if pretty else None))

if __name__ == "__main__":
    main()
