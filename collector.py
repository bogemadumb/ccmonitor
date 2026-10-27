#!/usr/bin/env python3
"""
CCMonitor collector — единый источник данных для menu bar приложения.

Отдаёт JSON со всем нужным:
  - limits: официальные лимиты 5ч/7д (utilization %, время сброса) из Anthropic oauth/usage
  - tokens: токены за текущий 5-часовой блок и за активную сессию (из JSONL, с дедупом)

Запуск:  python3 collector.py           -> печатает JSON в stdout
         python3 collector.py --pretty  -> человекочитаемо
         --login-url                    -> ссылка входа Anthropic (PKCE-пара в .oauth-pending.json, 600)
         --login-code <сервис|auto>     -> код из stdin меняется на токены, пишется в Keychain
         --delete <сервис>              -> стирает Keychain-сервис и кэш подписки

Логика данных намеренно вся здесь (Python), чтобы её было легко читать и править.
Swift-приложение просто вызывает этот скрипт и рисует результат.
"""
import subprocess, json, os, re, sys, glob, time, calendar, datetime, copy, base64, hashlib, secrets
import urllib.request, urllib.error, urllib.parse

CLAUDE_DIR = os.path.expanduser("~/.claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BLOCK_HOURS = 5
USAGE_DIR = os.path.join(CLAUDE_DIR, "usage-monitor")
SETTINGS_PATH = os.path.join(USAGE_DIR, "settings.json")
ACCESS_STATE = os.path.join(USAGE_DIR, "access-state.json")
PENDING_PATH = os.path.join(USAGE_DIR, ".oauth-pending.json")

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
def read_credentials():
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        stderr=subprocess.DEVNULL).decode().strip()
    o = json.loads(raw)
    return o.get("claudeAiOauth") or o

def get_token():
    oauth = read_credentials()
    return oauth.get("accessToken") or oauth.get("access_token")

def get_plan():
    """Название тарифа: сначала ~/.claude.json (oauthAccount обновляется при каждом
    запуске Claude Code), Keychain — запасной вариант: его rateLimitTier пишется только
    при логине и после апгрейда подписки остаётся старым."""
    tier = sub = ""
    try:
        acc = json.load(open(os.path.expanduser("~/.claude.json"))).get("oauthAccount") or {}
        tier = (acc.get("userRateLimitTier") or acc.get("organizationRateLimitTier")
                or acc.get("seatTier") or "").lower()
        sub = (acc.get("organizationType") or "").lower()
    except Exception:
        pass
    if not tier:
        try:
            c = read_credentials()
            tier = (c.get("rateLimitTier") or "").lower()
            sub = (c.get("subscriptionType") or "").lower()
        except Exception:
            return {"label": "Claude Code", "tier": None, "subscription": None}
    label = None
    m = re.search(r"max[_-]?(\d+)x", tier)
    if m:
        label = f"Max {m.group(1)}x"
    elif "enterprise" in tier or "enterprise" in sub:
        label = "Enterprise"
    elif "team" in tier or "team" in sub:
        label = "Team"
    elif "max" in tier or "max" in sub:
        label = "Max"
    elif "pro" in tier or "pro" in sub:
        label = "Pro"
    elif "free" in tier or "free" in sub:
        label = "Free"
    return {"label": label or "Claude Code", "tier": tier or None, "subscription": sub or None}

# ---------------------------------------------------------------- НАСТРОЙКИ
# Файл пишет окно настроек приложения; нет файла или ключа — значения по умолчанию.
DEFAULT_SETTINGS = {
    "accounts": {"order": [], "hidden": []},
    "menubar": {"source": "active_claude", "window": "five_hour", "number": "used",
                "show_reset": True, "show_gauge": True, "icon_only": False},
    "notify": {"warn1": 80, "warn2": 95, "warn1_on": True, "warn2_on": True, "exhausted": True,
               "reset": True, "access_lost": False, "accounts": [],
               "windows": ["five_hour", "seven_day"]},
    "general": {"poll_minutes": 5, "color_warn": 70, "color_crit": 90, "show_tokens": True,
                "show_cost": True, "show_renewal": False, "launch_at_login": True},
}
POLL_MIN_SECONDS = 300      # сервер usage режет чаще раза в 5 минут


def _merge_defaults(default, user):
    if not isinstance(user, dict):
        return copy.deepcopy(default)
    out = {}
    for k, dv in default.items():
        uv = user.get(k, dv)
        if isinstance(dv, dict):
            out[k] = _merge_defaults(dv, uv)
        elif isinstance(dv, bool):
            out[k] = uv if isinstance(uv, bool) else dv
        elif isinstance(dv, (int, float)):
            out[k] = uv if isinstance(uv, (int, float)) and not isinstance(uv, bool) else dv
        elif isinstance(dv, str):
            out[k] = uv if isinstance(uv, str) else dv
        elif isinstance(dv, list):
            out[k] = uv if isinstance(uv, list) else copy.deepcopy(dv)
        else:
            out[k] = uv
    return out


def load_settings(path=None):
    try:
        with open(path or SETTINGS_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except Exception:
        user = {}
    return _merge_defaults(DEFAULT_SETTINGS, user)


def poll_ttl(settings):
    try:
        return max(POLL_MIN_SECONDS, int(float(settings["general"]["poll_minutes"]) * 60))
    except Exception:
        return POLL_MIN_SECONDS


def apply_settings(settings):
    global LIMITS_TTL
    LIMITS_TTL = poll_ttl(settings)


# ---- «нет доступа с …»: первый момент, когда увидели relogin_needed / 401
NO_ACCESS_ERRORS = ("relogin_needed", "token_expired", "no_refresh", "account_on_hold")
AUTOHIDE_AFTER = 3 * 86400


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def update_access(state, key, lim, now):
    """Отмечает в state момент потери доступа. Живые данные сбрасывают отметку; сеть и 429
    ничего не говорят о доступе — отметку не трогают. Кэш с устаревшими данными
    тоже может нести ошибку доступа (last_error)."""
    if lim.get("ok") and not lim.get("stale"):
        state.pop(key, None)
        return None
    err = lim.get("error") if not lim.get("ok") else lim.get("last_error")
    if err in NO_ACCESS_ERRORS:
        state.setdefault(key, int(now))
    return state.get(key)


def _ddmm(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m") if ts else None


def _access_fields(state, key, creds, now):
    since = state.get(key)
    rexp = (creds or {}).get("refresh_expires_at")
    return {
        "no_access_since": since,
        "no_access_since_local": _ddmm(since),
        "auto_hidden": bool(since and now - since > AUTOHIDE_AFTER),
        "refresh_expires_at": rexp,
        "refresh_expires_local": _ddmm(rexp),
    }


# ---------------------------------------------------------------- АККАУНТЫ
# Claude Code держит в Keychain креды ТОЛЬКО активного аккаунта: залогинился вторым —
# первый затёрт. Поэтому на каждую подписку заводим собственный OAuth-логин и храним
# его креды отдельным ключом Keychain. Это независимая refresh-цепочка, она не дерётся
# за ротацию с самим Claude Code ни на Маке, ни на сервере.
# `claude setup-token` для этого не годится: у него нет scope user:profile, и
# usage-endpoint отвечает 403 oauth_scope_insufficient (проверено 30.08.2026).
ACCOUNT_SERVICES = [f"CCMonitor-account-{n}" for n in range(1, 10)]
ACCOUNT_RE = re.compile(r"^CCMonitor-account-[1-9]$")
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_UA = "claude-cli/2.1.215 (external, cli)"
REFRESH_MARGIN = 300   # обновляем access заранее, чтобы не ловить 401 на самой границе
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TIER_TTL = 86400       # тариф меняется редко — дёргать профиль чаще раза в сутки незачем


def _keychain_read(service):
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            stderr=subprocess.DEVNULL).decode().strip()
        return json.loads(raw)
    except Exception:
        return None


def _keychain_write(service, payload):
    try:
        subprocess.run(["security", "delete-generic-password", "-s", service],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["security", "add-generic-password", "-s", service,
                        "-a", os.environ.get("USER", "ccmonitor"),
                        "-w", json.dumps(payload), "-U"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _oauth_refresh(refresh_token):
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": refresh_token,
                       "client_id": OAUTH_CLIENT_ID}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": OAUTH_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 400 invalid_grant несёт причину: account_on_hold (аккаунт ограничен Anthropic,
        # claude.ai/restricted), refresh отозван и т.п. Отдаём её наверх, а не глухое no_token.
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        if e.code in (400, 401, 403) and body.get("error"):
            raise RuntimeError(body.get("error_description") or body["error"])
        raise


def account_token(service):
    """Свежий access аккаунта. Access живёт 8 часов, refresh — 28 дней и РОТИРУЕТСЯ:
    новый refresh обязан быть записан обратно, иначе цепочка рвётся."""
    creds = _keychain_read(service)
    if not creds:
        raise RuntimeError("no_creds")
    if creds.get("access_token") and time.time() < (creds.get("expires_at") or 0) - REFRESH_MARGIN:
        return creds["access_token"]
    rt = creds.get("refresh_token")
    if not rt:
        raise RuntimeError("no_refresh")
    if creds.get("refresh_expires_at") and time.time() > creds["refresh_expires_at"]:
        raise RuntimeError("relogin_needed")
    d = _oauth_refresh(rt)
    d["expires_at"] = int(time.time()) + int(d.get("expires_in") or 0)
    if d.get("refresh_token_expires_in"):
        d["refresh_expires_at"] = int(time.time()) + int(d["refresh_token_expires_in"])
    merged = {**creds, **d}
    _keychain_write(service, merged)
    return merged.get("access_token")


def account_tier(service, token):
    """Тариф подписки. В ответе usage его нет, зато отдаёт oauth/profile —
    organization.rate_limit_tier (default_claude_max_20x и т.п.). Кэшируем в кредах."""
    creds = _keychain_read(service) or {}
    if (creds.get("tier") and creds.get("sub_started")
            and time.time() - (creds.get("tier_fetched_at") or 0) < TIER_TTL):
        return creds["tier"]
    req = urllib.request.Request(PROFILE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "CCMonitor/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception:
        return creds.get("tier")          # молча живём на прошлом значении
    org = d.get("organization") or {}
    tier = org.get("rate_limit_tier") or org.get("seat_tier") or org.get("organization_type")
    if tier:
        _keychain_write(service, {**creds, "tier": tier, "tier_fetched_at": int(time.time()),
                                  "sub_started": org.get("subscription_created_at"),
                                  "sub_status": org.get("subscription_status"),
                                  "sub_paused": org.get("subscription_pause")})
    return tier


def next_renewal(created_iso):
    """Дата ближайшего продления подписки.
    ВАЖНО: точной даты окончания оплаченного периода API не отдаёт — её нет ни в
    oauth/profile, ни в oauth/account, а биллинг claude.ai по OAuth-токену не пускает (403).
    Поэтому считаем от дня месяца, в который подписка была создана. Верно для месячной
    подписки, которую не меняли; для годовой — нет. В панели помечено «≈»."""
    if not created_iso:
        return None
    try:
        start = datetime.datetime.fromisoformat(str(created_iso).replace("Z", "+00:00"))
    except Exception:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    y, m = now.year, now.month
    for _ in range(14):
        day = min(start.day, calendar.monthrange(y, m)[1])   # 31-е в феврале → последний день
        cand = start.replace(year=y, month=m, day=day)
        if cand > now:
            local = cand.astimezone()
            left = int((cand - now).total_seconds())
            return {"date_utc": cand.isoformat(),
                    "date_local": local.strftime("%d.%m %H:%M"),
                    "date_short": local.strftime("%d.%m"),
                    "in_seconds": left,
                    "in_human": _human_dur(left)}
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return None


def _account_email(creds):
    acc = creds.get("account")
    if isinstance(acc, dict):
        for k in ("email_address", "email", "emailAddress"):
            if acc.get(k):
                return acc[k]
    return creds.get("email")


def active_org():
    """Организация активного аккаунта — первые 8 знаков идентификатора."""
    try:
        acc = json.load(open(os.path.expanduser("~/.claude.json"))).get("oauthAccount") or {}
        return (acc.get("organizationUuid") or "")[:8] or None
    except Exception:
        return None


def active_email():
    """Кто сейчас залогинен в Claude Code на Маке — по нему подсвечиваем аккаунт
    и его процент уходит в строку статусбара."""
    try:
        acc = json.load(open(os.path.expanduser("~/.claude.json"))).get("oauthAccount") or {}
        return acc.get("emailAddress")
    except Exception:
        return None


def _mirror_active_account(cur):
    """Подписка, в которую сейчас залогинен Claude Code, живёт только в его Keychain и пропадёт
    при следующем /login. Пока она активна, держим в своём слоте зеркало её кредов (флаг mirror);
    после смены аккаунта зеркало становится самостоятельной цепочкой и подписка остаётся в панели."""
    slots = {}
    for svc in ACCOUNT_SERVICES:
        c = _keychain_read(svc)
        if c:
            slots[svc] = c
    for svc, c in slots.items():
        if c.get("mirror") and (_account_email(c) or "").lower() != (cur or "").lower():
            c.pop("mirror", None)
            _keychain_write(svc, c)
    if not cur:
        return
    try:
        o = read_credentials()
    except Exception:
        return
    at, rt = o.get("accessToken"), o.get("refreshToken")
    if not at or not rt:
        return
    own = next((s for s, c in slots.items()
                if (_account_email(c) or "").lower() == cur.lower()), None)
    if own and not slots[own].get("mirror"):
        return
    if own is None:
        own = next((s for s in ACCOUNT_SERVICES if s not in slots), None)
        if own is None:
            return
    prev = slots.get(own) or {}
    snap = {
        **prev,
        "token_type": "Bearer",
        "access_token": at,
        "refresh_token": rt,
        "expires_at": int((o.get("expiresAt") or 0) / 1000),
        "scope": " ".join(o.get("scopes") or []),
        "account": {**(prev.get("account") or {}), "email_address": cur},
        "mirror": True,
    }
    if o.get("refreshTokenExpiresAt"):
        try:
            v = float(o["refreshTokenExpiresAt"])
            snap["refresh_expires_at"] = int(v / 1000 if v > 1e11 else v)
        except (TypeError, ValueError):
            pass
    if o.get("rateLimitTier"):
        snap["tier"] = prev.get("tier") or o["rateLimitTier"]
    if snap != prev:
        _keychain_write(own, snap)


def collect_accounts(settings=None):
    """Лимиты по всем сохранённым подпискам. Если своих кредов нет ни одной —
    работаем по-старому, с активным токеном Claude Code из Keychain.
    Скрытые в настройках подписки не опрашиваются (кроме активной — она нужна статусбару)."""
    settings = settings or load_settings()
    hidden = set(settings["accounts"]["hidden"])
    cur = active_email()
    now = time.time()
    state = _load_json(ACCESS_STATE)
    before = json.dumps(state, sort_keys=True)
    out = []
    try:
        _mirror_active_account(cur)
    except Exception:
        pass
    for i, svc in enumerate(ACCOUNT_SERVICES, 1):
        creds = _keychain_read(svc)
        if not creds:
            continue
        email = _account_email(creds)
        is_active = bool(cur and email and cur.lower() == email.lower())
        if svc in hidden and not is_active:
            out.append({"id": svc, "email": email, "hidden": True, "active": False,
                        "label": creds.get("label") or _plan_label_from_tier(creds.get("tier")),
                        "org": ((creds.get("organization") or {}).get("uuid") or "")[:8] or None,
                        "limits": {"ok": False, "error": "hidden"},
                        **_access_fields(state, svc, creds, now)})
            continue
        cache = os.path.join(USAGE_DIR, f"limits-{i}.json")
        try:
            if creds.get("mirror") and is_active:
                lim = get_limits(cache, get_token)       # живой токен Claude Code, цепочку не трогаем
            else:
                lim = get_limits(cache, lambda s=svc: account_token(s))
        except Exception as e:
            lim = {"ok": False, "error": str(e)}
        update_access(state, svc, lim, now)
        tier = creds.get("tier")
        if (not tier or not creds.get("sub_started")) and lim.get("ok"):
            try:
                tier = account_tier(svc, get_token() if creds.get("mirror") and is_active
                                    else account_token(svc))
            except Exception:
                tier = None
        fresh = _keychain_read(svc) or creds     # после account_tier там уже есть биллинг
        out.append({
            "id": svc,
            "email": email,
            "label": creds.get("label") or _plan_label_from_tier(tier),
            "active": is_active,
            "hidden": False,
            "org": ((creds.get("organization") or {}).get("uuid") or "")[:8] or None,
            "limits": lim,
            "subscription": {
                "started": fresh.get("sub_started"),
                "status": fresh.get("sub_status"),
                "paused": fresh.get("sub_paused"),
                "renewal": next_renewal(fresh.get("sub_started")),
            },
            **_access_fields(state, svc, fresh, now),
        })
    # Активная подписка обязана быть видна всегда. Если своих кредов на неё ещё нет,
    # берём её из Keychain самого Claude Code — иначе в статусбар уйдёт чужой процент.
    if not any(a["active"] for a in out):
        lim = get_limits()
        update_access(state, "keychain-active", lim, now)
        out.append({"id": "keychain-active", "email": cur, "label": get_plan()["label"],
                    "active": True, "hidden": False, "org": active_org(), "limits": lim,
                    **_access_fields(state, "keychain-active", None, now)})
    if json.dumps(state, sort_keys=True) != before:
        _save_json(ACCESS_STATE, state)
    # активный — первым: панель читается сверху, и в статусбар идёт именно он
    out.sort(key=lambda a: (not a["active"], a.get("email") or ""))
    return out


def _plan_label_from_tier(tier):
    tier = (tier or "").lower()
    m = re.search(r"max[_-]?(\d+)x", tier)
    if m:
        return f"Max {m.group(1)}x"
    for word, label in (("enterprise", "Enterprise"), ("team", "Team"),
                        ("max", "Max"), ("pro", "Pro"), ("free", "Free")):
        if word in tier:
            return label
    return "Claude"


LIMITS_CACHE = os.path.join(USAGE_DIR, "limits-cache.json")
LIMITS_TTL = 300  # сек: usage-endpoint строго rate-limited, опрашиваем не чаще раза в 5 мин
# Если сервер ответил 429 без Retry-After — пауза по числу неудач подряд.
BACKOFF_STEPS = (120, 300, 900, 1800)
RETRY_AFTER_MAX = 3600  # потолок доверия к Retry-After сервера

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

def _refresh_countdown(win, stale=False):
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
    if stale and rem == 0:
        # окно из кэша давно истекло, а свежих данных нет — ноль тут был бы враньём
        w["resets_in_human"] = "нет данных"
        w["resets_in_short"] = "—"
    else:
        w["resets_in_human"] = _human_dur(rem)
        w["resets_in_short"] = f"{rem // 3600}:{(rem % 3600) // 60:02d}"
    return w

def _fresh_countdowns(resp):
    if resp.get("ok"):
        stale = bool(resp.get("stale"))
        for key in ("five_hour", "seven_day"):
            if resp.get(key):
                resp = {**resp, key: _refresh_countdown(resp[key], stale)}
    return resp

def _parse_retry_after(raw):
    """Retry-After приходит либо числом секунд, либо HTTP-датой. Отдаём секунды."""
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw)
    try:
        import email.utils
        ts = email.utils.parsedate_to_datetime(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return max(int(ts.timestamp() - time.time()), 0)
    except Exception:
        return None

def _save_cache(cached, path=None, **fields):
    """Пишем кэш, сохраняя последний успешный data/_fetched_at и служебные поля троттлинга."""
    path = path or LIMITS_CACHE
    base = dict(cached) if isinstance(cached, dict) else {}
    base.update(fields)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(base, f, ensure_ascii=False)
    except Exception:
        pass

def get_limits(cache_path=None, token_getter=None):
    """Лимиты одного аккаунта. У каждого аккаунта свой кэш-файл и свой счётчик backoff:
    квота usage-endpoint считается по аккаунту, смешивать их в одном кэше нельзя."""
    cache_path = cache_path or LIMITS_CACHE
    now = time.time()
    cached = None
    try:
        with open(cache_path) as f:
            cached = json.load(f)
    except Exception:
        cached = None
    if not isinstance(cached, dict):      # битый/не-объектный кэш игнорируем (защита от краша)
        cached = None
    cdata = cached.get("data") if cached else None
    cok = isinstance(cdata, dict) and cdata.get("ok")
    fetched_at = (cached or {}).get("_fetched_at", 0)
    retry_until = (cached or {}).get("_retry_until", 0) or 0
    fails = (cached or {}).get("_fail_count", 0) or 0

    def from_cache(err):
        # ошибка — отдаём последний успешный кэш, пересчитав отсчёт
        if cok:
            return _fresh_countdowns({**cdata, "stale": True,
                    "age_seconds": int(now - fetched_at) if fetched_at else None,
                    "last_error": err})
        return {"ok": False, "error": err or "no_data"}

    # свежий кэш — отдаём без запроса к API (троттлинг), пересчитав отсчёт
    if cok and (now - fetched_at) < LIMITS_TTL:
        return _fresh_countdowns({**cdata, "stale": False, "age_seconds": int(now - fetched_at)})

    # сервер попросил не беспокоить (429 + Retry-After) — молчим ровно до указанного момента.
    # Раньше этого не было: опрос шёл своим темпом, квота endpoint'а выедалась впустую,
    # и после ужесточения лимитов (01.08.2026) кэш не обновлялся сутками.
    if now < retry_until:
        return from_cache((cached or {}).get("_last_error") or "http_429")

    # иначе запрашиваем свежее
    result = _fetch_limits(token_getter)
    if result.get("ok"):
        _save_cache(cached, cache_path, _fetched_at=now, data=result,
                    _retry_until=0, _fail_count=0, _last_error=None)
        return _fresh_countdowns({**result, "stale": False, "age_seconds": 0})

    err = result.get("error")
    ra = result.get("retry_after")
    # Паузу назначаем только когда упёрлись в сервер (429/5xx). Нет сети или истёк токен —
    # сервер не при чём, там обычного LIMITS_TTL достаточно.
    if ra or (isinstance(err, str) and err.startswith("http_")):
        fails = int(fails) + 1
        # +5с запаса: попадание ровно в границу окна снова даёт 429
        pause = min(ra, RETRY_AFTER_MAX) + 5 if ra else BACKOFF_STEPS[min(fails, len(BACKOFF_STEPS)) - 1]
        _save_cache(cached, cache_path, _retry_until=now + pause, _fail_count=fails, _last_error=err)
    else:
        _save_cache(cached, cache_path, _retry_until=0, _fail_count=0, _last_error=err)
    return from_cache(err)

def _fetch_limits(token_getter=None):
    try:
        tok = (token_getter or get_token)()
    except Exception as e:
        code = str(e)
        known = code in ("no_creds", "no_refresh", "relogin_needed", "account_on_hold")
        return {"ok": False, "error": code if known else "no_token", "detail": code}
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
        ra = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
        return {"ok": False, "error": "token_expired" if code == 401 else f"http_{code}",
                "retry_after": ra,
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
    """Разворачиваем iterations ВМЕСТО основной записи, а не в дополнение к ней.

    usage верхнего уровня — это АГРЕГАТ итераций, а не отдельная работа: проверено на
    7639 записях подряд — в каждой ровно одна iteration, и её токены совпадают с основной
    запись в ноль. Прежняя версия добавляла и то и другое, из-за чего все цифры токенов
    и стоимость были ровно вдвое больше настоящих (исправлено 30.08.2026).

    Правило устойчиво к обоим случаям: итерации берём вместо основной записи, только
    если они покрывают её целиком (тогда у нас та же сумма, но с точными моделями).
    Если сумма итераций МЕНЬШЕ основной — значит они неполные, а верхний уровень агрегат:
    берём его, чтобы ничего не потерять."""
    def _tot(u):
        return (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
    out = []
    for e in entries:
        u = e["usage"]
        its = u.get("iterations") or []
        if its and sum(_tot(i) for i in its) >= _tot(u):
            for it in its:
                out.append({"model": it.get("model") or e["model"], "ts": e["ts"],
                            "session": e["session"], "usage": it})
        else:
            out.append({"model": e["model"], "ts": e["ts"], "session": e["session"], "usage": u})
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

# ---------------------------------------------------------------- CODEX
# Лимиты Codex собирает codex-usage-local.py: живой запрос к app-server из расширения
# VS Code, а если он не ответил — разбор журналов сессий (~/.codex/sessions). Готовый срез
# лежит рядом — здесь только читаем файл, никакой работы на каждой перерисовке.
# Файл в ~/.claude/usage-monitor кладёт LaunchAgent com.ccmonitor.app.codex раз в 5 минут.
CODEX_SNAPSHOT = os.path.join(USAGE_DIR, "codex-usage.json")


def _codex_window(win):
    """Приводим окно Codex к тому же виду, что окна Claude, чтобы панель рисовала их одинаково.
    resets_at у Codex — абсолютный unix-epoch, отсчёт считаем от него относительно «сейчас».
    rolled_over — окно уже закрылось, а нового расхода не было: процент обнулён,
    времени следующего сброса нет, потому что окно скользящее и начнётся с первого запроса."""
    if not isinstance(win, dict):
        return None
    resets_at = win.get("resets_at")
    rolled = bool(win.get("rolled_over"))
    rem = max(int(resets_at - time.time()), 0) if resets_at else None
    when = datetime.datetime.fromtimestamp(resets_at).astimezone() if resets_at else None
    return {
        "used_percentage": win.get("used_percent"),
        "window_minutes": win.get("window_minutes"),
        "rolled_over": rolled,
        "resets_at_utc": (datetime.datetime.fromtimestamp(resets_at, datetime.timezone.utc).isoformat()
                          if resets_at else None),
        "resets_in_seconds": rem,
        "resets_in_human": _human_dur(rem),
        "resets_in_short": (f"{rem // 3600}:{(rem % 3600) // 60:02d}") if rem is not None else None,
        "resets_hm": when.strftime("%H:%M") if when else None,
        "resets_full": when.strftime("%d.%m %H:%M") if when else None,
    }


def get_codex():
    try:
        d = json.load(open(CODEX_SNAPSHOT))
    except Exception:
        return {"ok": False, "error": "no_snapshot"}
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error") or "no_data",
                "app_server_error": d.get("app_server_error")}
    # Возраст меряем по measured_at, а не по mtime файла: у app-server это момент запроса
    # (свежий всегда), у запасного разбора журналов — время последней активности Codex,
    # цифры там стоят, пока он не работал, и свежесть показывать нельзя.
    age = None
    if d.get("measured_at"):
        try:
            m = datetime.datetime.fromisoformat(d["measured_at"].replace("Z", "+00:00"))
            age = int((datetime.datetime.now(datetime.timezone.utc) - m).total_seconds())
        except Exception:
            age = None
    return {
        "ok": True,
        "host": d.get("host"),
        "label": f"Codex {(d.get('plan') or '').capitalize()}".strip(),
        "five_hour": _codex_window(d.get("five_hour")),
        "seven_day": _codex_window(d.get("weekly")),
        "age_seconds": age,
        "age_human": _human_dur(age),
        "limit_reached": d.get("limit_reached"),
        "source": d.get("source"),
        "reset_credits": d.get("reset_credits"),
        "app_server_error": d.get("error"),
    }


# ---------------------------------------------------------------- ВХОД В ПОДПИСКУ
# PKCE-вход в подписку Claude прямо в collector.py. Код авторизации приходит через stdin — в аргументах его видно в ps.
OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
OAUTH_REDIRECT = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPE = "user:inference user:profile"
PENDING_TTL = 3600


class LoginError(Exception):
    pass


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def login_url():
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    fd = os.open(PENDING_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"verifier": verifier, "state": state, "created_at": int(time.time())}, f)
    os.chmod(PENDING_PATH, 0o600)
    q = urllib.parse.urlencode({
        "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT, "scope": OAUTH_SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    return f"{OAUTH_AUTHORIZE_URL}?{q}"


def _parse_pasted(pasted):
    """Страница показывает «код#state»; на случай вставки целой ссылки разбираем и её."""
    pasted = (pasted or "").strip().strip('"').strip("'").strip()
    if pasted.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        return (q.get("code") or [""])[0], (q.get("state") or [""])[0]
    code, _, state = pasted.partition("#")
    return code.strip(), state.strip()


def _oauth_exchange(code, state, verifier):
    body = json.dumps({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": OAUTH_REDIRECT, "client_id": OAUTH_CLIENT_ID,
                       "code_verifier": verifier, "state": state}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": OAUTH_UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            b = json.loads(e.read())
            why = b.get("error_description") or b.get("error") or ""
        except Exception:
            why = ""
        raise LoginError(f"exchange_failed: HTTP {e.code} {why}".strip())
    except Exception as e:
        raise LoginError(f"network: {e}")


def _service_for(target, email):
    if target != "auto":
        if not ACCOUNT_RE.match(target):
            raise LoginError("bad_service")
        return target
    free = None
    for svc in ACCOUNT_SERVICES:
        cur = _keychain_read(svc)
        if not cur:
            free = free or svc
        elif email and (_account_email(cur) or "").lower() == email.lower():
            return svc                       # тот же аккаунт уже заведён — обновляем его, без дубля
    if not free:
        raise LoginError("no_free_slot")
    return free


def login_code(pasted, target="auto"):
    if target != "auto" and not ACCOUNT_RE.match(target):
        raise LoginError("bad_service")
    pend = _load_json(PENDING_PATH)
    if not pend.get("verifier"):
        raise LoginError("no_pending")
    if time.time() - (pend.get("created_at") or 0) > PENDING_TTL:
        raise LoginError("pending_expired")
    code, state = _parse_pasted(pasted)
    if not code:
        raise LoginError("bad_code")
    if state and state != pend.get("state"):
        raise LoginError("state_mismatch")
    d = _oauth_exchange(code, state or pend["state"], pend["verifier"])
    if not d.get("access_token"):
        raise LoginError("exchange_failed: нет access_token")
    now = int(time.time())
    if d.get("expires_in"):
        d["expires_at"] = now + int(d["expires_in"])
    if d.get("refresh_token_expires_in"):
        d["refresh_expires_at"] = now + int(d["refresh_token_expires_in"])
    email = _account_email(d)
    svc = _service_for(target, email)
    if not _keychain_write(svc, d):
        raise LoginError("keychain_failed")
    try:
        os.remove(PENDING_PATH)
    except OSError:
        pass
    _forget_account(svc)
    tier = account_tier(svc, d["access_token"])
    return {"ok": True, "service": svc, "email": email,
            "label": _plan_label_from_tier(tier), "tier": tier}


def _forget_account(svc):
    """Кэш лимитов и отметка «нет доступа» принадлежат прежнему входу — после смены не нужны."""
    n = svc.rsplit("-", 1)[-1]
    try:
        os.remove(os.path.join(USAGE_DIR, f"limits-{n}.json"))
    except OSError:
        pass
    state = _load_json(ACCESS_STATE)
    if state.pop(svc, None) is not None:
        _save_json(ACCESS_STATE, state)


def delete_account(svc):
    if not ACCOUNT_RE.match(svc or ""):
        raise LoginError("bad_service")
    for _ in range(3):                      # на случай дублей одного сервиса в Keychain
        r = subprocess.run(["security", "delete-generic-password", "-s", svc],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            break
    _forget_account(svc)
    return {"ok": True, "service": svc}


def _argval(argv, name):
    i = argv.index(name)
    return argv[i + 1] if i + 1 < len(argv) else ""


def cli(argv):
    """Режимы входа/удаления. Возвращает код выхода или None — тогда обычный сбор данных."""
    try:
        if "--login-url" in argv:
            print(login_url())
            return 0
        if "--login-code" in argv:
            res = login_code(sys.stdin.read(), _argval(argv, "--login-code") or "auto")
        elif "--delete" in argv:
            res = delete_account(_argval(argv, "--delete"))
        else:
            return None
    except LoginError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- MAIN
def main():
    code = cli(sys.argv[1:])
    if code is not None:
        sys.exit(code)
    settings = load_settings()
    apply_settings(settings)
    accounts = collect_accounts(settings)
    active = next((a for a in accounts if a.get("active")), accounts[0] if accounts else None)
    # limits/plan верхнего уровня — активная подписка: именно она идёт в строку статусбара,
    # и на этих же полях держится совместимость со старой сборкой Swift.
    limits = (active or {}).get("limits") or {"ok": False, "error": "no_accounts"}
    result = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "plan": get_plan(),
        "limits": limits,
        "tokens": get_tokens(limits),
        "accounts": accounts,
        "codex": get_codex(),
    }
    pretty = "--pretty" in sys.argv
    print(json.dumps(result, ensure_ascii=False, indent=2 if pretty else None))

if __name__ == "__main__":
    main()
