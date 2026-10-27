"""Снимает лимиты Codex с локальной машины и кладёт срез туда, откуда его читает CCMonitor.

Codex ищется в расширении VS Code (openai.chatgpt) на этом Маке.

Откуда цифры. Основной источник — бинарник Codex из расширения VS Code: у него есть
JSON-RPC app-server по stdio, метод account/rateLimits/read отдаёт живые проценты окон,
план, кредиты и доступные сбросы лимита. Авторизацию делает сам бинарник — токены мы не
читаем и не обновляем. Запрос идёт в бэкенд OpenAI, поэтому агент запускается раз в 5 минут.

Запасной источник, если app-server не ответил: журналы сессий ~/.codex/sessions/ГГГГ/ММ/ДД/rollout-*.jsonl.
В событиях token_count лежит блок rate_limits; берём самый свежий по всем сессиям. Цифры
оттуда меняются только когда Codex реально работал, поэтому в срез кладём measured_at —
время последней активности, и монитор показывает возраст. У части версий Codex журналы
не пишутся вовсе — отсюда и app-server.

Одно исправление делаем в обоих источниках: если resets_at уже в прошлом, окно закрылось,
а нового расхода не было. Такой процент — мусор, отдаём ноль и флаг rolled_over. Время
следующего сброса не выдумываем: окно скользящее, оно начнётся с первого запроса.

Запускает launchd (com.ccmonitor.app.codex) раз в 5 минут; скрипт лежит внутри CCMonitor.app.
"""
import datetime
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
DST = os.path.expanduser("~/.claude/usage-monitor/codex-usage.json")
APP_SERVER_TIMEOUT = 25     # жёсткий потолок на весь обмен с app-server, секунды
VSCODE_EXT_GLOB = os.path.expanduser("~/.vscode/extensions/openai.chatgpt-*-darwin-*/bin/macos-*/codex")
FILES_TO_SCAN = 12          # самых свежих журналов сессий
TAIL_BYTES = 1_500_000      # rate_limits лежит в конце файла; читаем хвост, а не весь журнал


def session_files():
    """Журналы сессий, свежие первыми. Поддерживаем обе раскладки: с датовыми
    подпапками (новый Codex) и плоскую (старый CLI)."""
    found = glob.glob(os.path.join(SESSIONS_DIR, "**", "rollout-*.jsonl"), recursive=True)
    found += glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
    found = list(dict.fromkeys(found))
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found[:FILES_TO_SCAN]


def read_lines(path, tail_only):
    """Строки файла в обратном порядке — от последней к первой."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if tail_only and size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()        # обрезанную строку выбрасываем
        chunk = f.read()
    return reversed(chunk.decode("utf-8", "replace").splitlines())


def scan(lines):
    for line in lines:
        if '"rate_limits"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        rl = payload.get("rate_limits")
        if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")):
            return rec.get("timestamp"), rl
    return None


def latest_rate_limits(path):
    """Последний непустой блок rate_limits в журнале плюс метка времени события.
    Сначала хвост — token_count пишется после каждого хода, поэтому он там почти всегда.
    Если в хвосте пусто, а файл длиннее хвоста, дочитываем целиком: иначе одна аномально
    длинная строка отрезала бы весь журнал."""
    hit = scan(read_lines(path, tail_only=True))
    if hit is None and os.path.getsize(path) > TAIL_BYTES:
        hit = scan(read_lines(path, tail_only=False))
    return hit


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def window(win, now):
    if not isinstance(win, dict):
        return None
    out = {
        "used_percent": win.get("used_percent"),
        "window_minutes": win.get("window_minutes"),
        "resets_at": win.get("resets_at"),
        "rolled_over": False,
    }
    resets_at = win.get("resets_at")
    if resets_at and resets_at <= now:
        # Окно закрылось, а новых трат не было — иначе журнал дал бы более свежий срез.
        out["used_percent"] = 0
        out["resets_at"] = None
        out["rolled_over"] = True
    return out


def _version_key(path):
    """Ключ сортировки по версии расширения: 26.930.21537 > 26.928.31416 (числами, не строкой)."""
    m = re.search(r"openai\.chatgpt-([0-9.]+)-darwin", path)
    return tuple(int(x) for x in m.group(1).split(".") if x) if m else ()


def codex_binary():
    """Самый свежий Codex из расширения VS Code, иначе codex из PATH, иначе None."""
    found = [p for p in glob.glob(VSCODE_EXT_GLOB) if os.access(p, os.X_OK)]
    if found:
        return max(found, key=_version_key)
    return shutil.which("codex")


def _kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    for stream in (proc.stdin, proc.stdout):
        try:
            stream.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def app_server_rate_limits(binary, timeout=APP_SERVER_TIMEOUT):
    """Спрашивает у codex app-server account/rateLimits/read. Возвращает result целиком.
    Строки без нашего id (уведомления) пропускаем. Процесс убивается всегда — и при успехе,
    и при ошибке, и по таймауту, чтобы не копились висящие app-server."""
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        init = {"id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "ccmonitor", "title": "CCMonitor", "version": "1.0"}}}
        msgs = [init, {"method": "initialized", "params": {}},
                {"id": 2, "method": "account/rateLimits/read", "params": None}]
        try:
            proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs).encode())
            proc.stdin.flush()
        except OSError as e:
            raise RuntimeError(f"stdin: {e}")
        fd = proc.stdout.fileno()
        buf = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError("timeout")
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                raise RuntimeError("timeout")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError("app-server закрыл вывод без ответа")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("id") != 2:
                    continue
                if msg.get("error") is not None:
                    raise RuntimeError(f"ответ с ошибкой: {str(msg['error'])[:200]}")
                result = msg.get("result")
                if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
                    raise RuntimeError("в ответе нет rateLimits")
                return result
    finally:
        _kill_tree(proc)


def app_window(win, now):
    if not isinstance(win, dict):
        return None
    return window({"used_percent": win.get("usedPercent"),
                   "window_minutes": win.get("windowDurationMins"),
                   "resets_at": win.get("resetsAt")}, now)


def reset_credits(info):
    """Доступные сбросы лимита и ближайший срок сгорания (epoch) — по самому раннему из доступных."""
    if not isinstance(info, dict):
        return None
    expiries = [c.get("expiresAt") for c in info.get("credits") or []
                if isinstance(c, dict) and c.get("status") == "available" and c.get("expiresAt")]
    return {"available": info.get("availableCount", len(expiries)),
            "nearest_expiry": min(expiries) if expiries else None}


def collect_app_server(now):
    binary = codex_binary()
    if not binary:
        raise RuntimeError("бинарник codex не найден")
    result = app_server_rate_limits(binary)
    rl = result["rateLimits"]
    cr = rl.get("credits") if isinstance(rl.get("credits"), dict) else None
    reached = rl.get("rateLimitReachedType")
    if not reached and rl.get("spendControlReached"):
        reached = "spend_control"
    return {
        "ok": True,
        "host": "mac",
        "collected_at": now,
        "plan": rl.get("planType"),
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "five_hour": app_window(rl.get("primary"), now),
        "weekly": app_window(rl.get("secondary"), now),
        "credits": {"has_credits": cr.get("hasCredits"), "unlimited": cr.get("unlimited"),
                    "balance": cr.get("balance")} if cr else None,
        "reset_credits": reset_credits(result.get("rateLimitResetCredits")),
        "limit_reached": reached,
        "source": "app-server",
    }


def collect():
    now = int(time.time())
    try:
        return collect_app_server(now)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
    data = collect_sessions()
    if data.get("ok"):
        data["error"] = reason
    else:
        data["app_server_error"] = reason
    return data


def collect_sessions():
    now = int(time.time())
    if not os.path.isdir(SESSIONS_DIR):
        return {"ok": False, "host": "mac", "collected_at": now, "error": "no_sessions"}

    best_ts, best_rl, best_file = None, None, None
    for path in session_files():
        try:
            hit = latest_rate_limits(path)
        except OSError:
            continue
        if not hit:
            continue
        ts, rl = hit
        parsed = parse_ts(ts)
        if parsed is None:
            parsed = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
            ts = parsed.isoformat().replace("+00:00", "Z")
        if best_ts is None or parsed > best_ts:
            best_ts, best_rl, best_file = parsed, rl, path

    if best_rl is None:
        return {"ok": False, "host": "mac", "collected_at": now, "error": "no_sessions"}

    return {
        "ok": True,
        "host": "mac",
        "collected_at": now,
        "plan": best_rl.get("plan_type"),
        "measured_at": best_ts.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "five_hour": window(best_rl.get("primary"), now),
        "weekly": window(best_rl.get("secondary"), now),
        "credits": best_rl.get("credits"),
        "limit_reached": best_rl.get("rate_limit_reached_type"),
        "source": "sessions",
        "session_file": os.path.basename(best_file or ""),
    }


def main():
    data = collect()
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    tmp = DST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DST)


if __name__ == "__main__":
    main()
