"""One-shot перевірка: читає стан інвертора Deye і розетки Tuya, керує бойлером,
шле сповіщення й обробляє кнопки в Telegram. Запускається кожні 5 хв (GitHub Actions).
Усі секрети — з env (GitHub Secrets). Пам'ять між запусками — state.json у репо."""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import tinytuya

# ================= DEYE =================
DEYE_APP_ID = os.environ["DEYE_APP_ID"]
DEYE_APP_SECRET = os.environ["DEYE_APP_SECRET"]
DEYE_EMAIL = os.environ["DEYE_EMAIL"]
DEYE_PASSWORD = os.environ["DEYE_PASSWORD"]  # sha256-хеш
DEYE_DEVICE_SN = os.environ["DEYE_DEVICE_SN"]
DEYE_BASE_URL = os.environ.get("DEYE_BASE_URL", "https://eu1-developer.deyecloud.com/v1.0")

# ================= TUYA =================
TUYA_REGION = os.environ.get("TUYA_REGION", "eu")
TUYA_ACCESS_ID = os.environ["TUYA_ACCESS_ID"]
TUYA_ACCESS_SECRET = os.environ["TUYA_ACCESS_SECRET"]
TUYA_DEVICE_ID = os.environ["TUYA_DEVICE_ID"]
TUYA_SWITCH_CODE = os.environ.get("TUYA_SWITCH_CODE", "switch_1")

# ================= TELEGRAM (необов'язково) =================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
# один chat_id або кілька через кому: "317300041,693765287"
TG_CHAT_IDS = [c.strip() for c in os.environ.get("TG_CHAT_ID", "").split(",") if c.strip()]
TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ================= ЛОГІКА =================
VOLTAGE_THRESHOLD = float(os.environ.get("VOLTAGE_THRESHOLD", "100"))
# Вмикати бойлер лише коли споживання з мережі (без бойлера) нижче цього, кВт.
# Автомат 20 А ≈ 4.4 кВт; бойлер в еко ≈ 1.5 кВт → 2.5 + 1.5 = 4.0 кВт, є запас.
GRID_POWER_MAX_KW = float(os.environ.get("GRID_POWER_MAX_KW", "2.5"))
# Струм заряду АКБ від мережі (gridChargeAmpere): звичайний і "економ" для кнопки 🔋
CHARGE_A_NORMAL = int(os.environ.get("CHARGE_A_NORMAL", "80"))
CHARGE_A_ECO = int(os.environ.get("CHARGE_A_ECO", "40"))
# Еко-режим (40 А + бойлер) знімаємо, коли споживання з мережі впаде нижче GRID_POWER_MAX_KW
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

KYIV = timezone(timedelta(hours=2))
CB_ECO, CB_FULL = "boiler_eco", "boiler_full"


def log(msg: str) -> None:
    print(f"[{datetime.now(KYIV).strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(KYIV).isoformat(timespec="seconds")


# ================= STATE =================
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.write("\n")


# ================= TELEGRAM =================
def tg(method: str, **payload) -> dict:
    r = requests.post(f"{TG_API}/{method}", json=payload, timeout=10)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]


def notify(text: str, buttons: bool = False) -> list:
    """Шле всім чатам. Повертає [(chat_id, message_id)] для подальшого зняття кнопок."""
    if not (TG_BOT_TOKEN and TG_CHAT_IDS):
        return []
    sent = []
    payload = {"text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": f"🔋 {CHARGE_A_ECO} А + бойлер", "callback_data": CB_ECO},
            {"text": f"⚡ {CHARGE_A_NORMAL} А без бойлера", "callback_data": CB_FULL},
        ]]}
    for chat_id in TG_CHAT_IDS:
        try:
            m = tg("sendMessage", chat_id=chat_id, **payload)
            sent.append((chat_id, m["message_id"]))
        except Exception as e:  # сповіщення не має валити основну логіку
            log(f"⚠️ Telegram ({chat_id}): {e}")
    return sent


def remove_buttons(msgs: list) -> None:
    for chat_id, message_id in msgs or []:
        try:
            tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
               reply_markup={"inline_keyboard": []})
        except Exception as e:
            log(f"⚠️ Telegram editMessage: {e}")


def poll_buttons(state: dict) -> str | None:
    """Забирає натискання кнопок з моменту останнього запуску. Повертає останнє (eco/full)."""
    if not (TG_BOT_TOKEN and TG_CHAT_IDS):
        return None
    try:
        updates = tg("getUpdates", offset=state.get("tg_offset", 0),
                     allowed_updates=["callback_query"], timeout=0)
    except Exception as e:
        log(f"⚠️ Telegram getUpdates: {e}")
        return None
    action = None
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        who = str(cq["from"]["id"])
        try:
            if who not in TG_CHAT_IDS:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Не твоя кнопка 🙂")
                continue
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Прийнято")
        except Exception as e:
            log(f"⚠️ Telegram answerCallbackQuery: {e}")
        if cq.get("data") in (CB_ECO, CB_FULL):
            action = cq["data"]
            log(f"🔘 Кнопка {action} від {who}")
    return action


# ================= DEYE =================
def deye(method: str, path: str, body: dict | None, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{DEYE_BASE_URL}{path}"
    r = (requests.get(url, params=body, headers=headers, timeout=20) if method == "GET"
         else requests.post(url, json=body, headers=headers, timeout=20))
    r.raise_for_status()
    return r.json()


def get_deye_token() -> str:
    d = deye("POST", f"/account/token?appId={DEYE_APP_ID}",
             {"appSecret": DEYE_APP_SECRET, "email": DEYE_EMAIL, "password": DEYE_PASSWORD})
    if not d.get("success"):
        raise RuntimeError(f"Deye token: {d.get('msg')}")
    return d["accessToken"]


def get_inverter_data(token: str) -> dict:
    """Всі параметри інвертора: {key: (value, unit)}."""
    d = deye("POST", f"/device/latest?appId={DEYE_APP_ID}", {"deviceList": [DEYE_DEVICE_SN]}, token)
    if not d.get("success") or not d.get("deviceDataList"):
        raise RuntimeError(f"Deye latest: {d.get('msg')}")
    return {item.get("key"): (item.get("value"), item.get("unit") or "")
            for item in d["deviceDataList"][0].get("dataList", [])}


def set_grid_charge_ampere(token: str, amps: int) -> None:
    """Міняє gridChargeAmpere і чекає підтвердження інвертора (статус 666)."""
    for _ in range(10):
        d = deye("POST", "/order/battery/parameter/update",
                 {"deviceSn": DEYE_DEVICE_SN, "paramterType": "GRID_CHARGE_AMPERE", "value": amps}, token)
        if str(d.get("code")) == "2104004":  # інвертор виконує попередню команду
            time.sleep(3)
            continue
        break
    order_id = d.get("orderId")
    if not order_id:
        raise RuntimeError(f"Deye order: {d.get('msg')}")
    for _ in range(15):
        s = deye("GET", f"/order/{order_id}", None, token)
        status = str(s.get("status"))
        if status == "666":
            log(f"✅ Deye: струм заряду = {amps} А")
            return
        if status not in ("0", "100"):
            raise RuntimeError(f"Deye order {order_id} failed: {s}")
        time.sleep(2)
    raise RuntimeError(f"Deye order {order_id} timeout")


def _kw(d: dict, *keys: str) -> float | None:
    for k in keys:
        if k in d and d[k][0] not in (None, ""):
            try:
                v = float(d[k][0])
            except ValueError:
                continue
            return v if d[k][1].lower() == "kw" else v / 1000
    return None


class Metrics:
    def __init__(self, d: dict):
        self.voltage = float(d.get("GridVoltageL1L2", (0, ""))[0] or 0)
        self.soc = d.get("SOC", ("N/A", ""))[0]
        self.grid_kw = _kw(d, "TotalGridPower", "GridPower")
        self.load_kw = _kw(d, "UPSLoadPower", "TotalLoadPower", "LoadPower")
        self.batt_kw = _kw(d, "BatteryPower", "TotalBatteryPower", "BatteryChargePower")

    @property
    def grid_import_kw(self) -> float:
        return max(0.0, self.grid_kw or 0.0)

    def status(self) -> str:
        def f(v):
            return "?" if v is None else f"{v:.1f} кВт"
        batt = ""
        if self.batt_kw is not None:
            batt = f" ({'заряд' if self.batt_kw < 0 else 'розряд'} {abs(self.batt_kw):.1f} кВт)"
        return f"Мережа: {f(self.grid_kw)} | Навантаження: {f(self.load_kw)} | АКБ: {self.soc}%{batt}"


# ================= TUYA =================
def tuya_cloud() -> tinytuya.Cloud:
    return tinytuya.Cloud(apiRegion=TUYA_REGION, apiKey=TUYA_ACCESS_ID, apiSecret=TUYA_ACCESS_SECRET)


def get_socket(cloud: tinytuya.Cloud) -> tuple[bool, float]:
    """(увімкнена?, потужність Вт)."""
    status = cloud.getstatus(TUYA_DEVICE_ID)
    if not status.get("success"):
        raise RuntimeError(f"Tuya status: {status}")
    dps = {dp.get("code"): dp.get("value") for dp in status.get("result", [])}
    if TUYA_SWITCH_CODE not in dps:
        raise RuntimeError(f"Tuya: не знайдено dp '{TUYA_SWITCH_CODE}' у {dps}")
    return bool(dps[TUYA_SWITCH_CODE]), float(dps.get("cur_power") or 0) / 10  # cur_power у 0.1 Вт


def set_socket(cloud: tinytuya.Cloud, turn_on: bool) -> None:
    res = cloud.sendcommand(TUYA_DEVICE_ID, {"commands": [{"code": TUYA_SWITCH_CODE, "value": turn_on}]})
    if not res.get("success"):
        raise RuntimeError(f"Tuya command: {res}")
    log(f"✅ Розетка {'увімкнена' if turn_on else 'вимкнена'}")


# ================= MAIN =================
def main() -> int:
    token = get_deye_token()
    m = Metrics(get_inverter_data(token))
    grid_ok = m.voltage >= VOLTAGE_THRESHOLD
    power_ok = m.grid_import_kw <= GRID_POWER_MAX_KW

    cloud = tuya_cloud()
    socket_on, socket_w = get_socket(cloud)

    state = load_state()
    prev_grid_ok = state.get("grid_ok")
    manual_warned = bool(state.get("manual_warned"))
    pending = bool(state.get("boiler_pending"))      # світло є, бойлер чекає, поки спаде заряд
    pending_msgs = state.get("pending_msgs") or []   # повідомлення з кнопками
    eco = state.get("eco")                           # {"since": iso} поки струм заряду знижений
    eco_started_now = False
    action = poll_buttons(state)

    log(f"Напруга: {m.voltage}V | {m.status()} | Розетка: {'ON' if socket_on else 'OFF'} {socket_w:.0f} Вт "
        f"| Мережа: {'є' if grid_ok else 'нема'} (було: {prev_grid_ok}) | pending={pending} eco={bool(eco)}")

    wait_msg = (f"Заряд {CHARGE_A_NORMAL} А. Бойлер увімкну сам, коли мережа впаде нижче "
                f"{GRID_POWER_MAX_KW:g} кВт (зараз {m.grid_import_kw:.1f}), або обери кнопкою.")

    def start_pending(text: str) -> None:
        nonlocal pending, pending_msgs
        pending = True
        pending_msgs = notify(text, buttons=True)

    def resolve_pending() -> None:
        nonlocal pending, pending_msgs
        pending = False
        remove_buttons(pending_msgs)
        pending_msgs = []

    def restore_charge(reason: str) -> None:
        nonlocal eco
        set_grid_charge_ampere(token, CHARGE_A_NORMAL)
        eco = None
        notify(f"🔌 Струм заряду повернуто на {CHARGE_A_NORMAL} А — {reason}\n{m.status()}")

    # ---- зміна стану мережі ----
    if prev_grid_ok is None:
        log("Перший запуск — запам'ятовуємо стан, розетку не чіпаємо")

    elif prev_grid_ok and not grid_ok:
        log("‼️ Мережа зникла — вимикаємо розетку")
        if socket_on:
            set_socket(cloud, False)
            socket_on = False
        if pending:
            resolve_pending()
        if eco:
            restore_charge("світло зникло")
        notify(f"🔴 Світло зникло\n{m.status()}\nБойлер вимкнено")

    elif not prev_grid_ok and grid_ok:
        log("💡 Мережа повернулась")
        if socket_on and not power_ok:
            # бойлер увімкнули вручну під час блекауту, а зараз інвертор заряджає на повну —
            # разом виб'є автомат. Вимикаємо і ставимо в чергу.
            set_socket(cloud, False)
            socket_on = False
            start_pending(f"🟢 Світло повернулось\n{m.status()}\n"
                          f"⚠️ Бойлер був увімкнений — вимкнув, щоб не вибило автомат. {wait_msg}")
        elif socket_on:
            notify(f"🟢 Світло повернулось\n{m.status()}\nБойлер уже увімкнений")
        elif power_ok:
            set_socket(cloud, True)
            socket_on = True
            notify(f"🟢 Світло повернулось\n{m.status()}\nБойлер увімкнено")
        else:
            start_pending(f"🟢 Світло повернулось\n{m.status()}\n⏳ {wait_msg}")

    # ---- бойлер у черзі ----
    elif grid_ok and pending:
        if action == CB_ECO:
            log(f"🔋 Кнопка: знижуємо заряд до {CHARGE_A_ECO} А і вмикаємо бойлер")
            set_grid_charge_ampere(token, CHARGE_A_ECO)
            eco = {"since": now_iso()}
            eco_started_now = True
            time.sleep(20)  # дати інвертору скинути потужність
            set_socket(cloud, True)
            socket_on = True
            resolve_pending()
            notify(f"🔋 Заряд {CHARGE_A_ECO} А, бойлер увімкнено.\n"
                   f"Поверну {CHARGE_A_NORMAL} А, коли мережа впаде нижче {GRID_POWER_MAX_KW:g} кВт.\n{m.status()}")
        elif action == CB_FULL:
            log(f"⚡ Кнопка: лишаємо {CHARGE_A_NORMAL} А, бойлер чекає")
            remove_buttons(pending_msgs)
            pending_msgs = []
            notify(f"⚡ Заряд {CHARGE_A_NORMAL} А, бойлер увімкну сам, коли мережа впаде нижче "
                   f"{GRID_POWER_MAX_KW:g} кВт.")
        elif socket_on:
            log("Бойлер увімкнули вручну — чергу знімаємо")
            resolve_pending()
        elif power_ok:
            log("🔥 Споживання спало — вмикаємо бойлер")
            set_socket(cloud, True)
            socket_on = True
            resolve_pending()
            notify(f"🔥 Бойлер увімкнено\n{m.status()}")
        else:
            log(f"⏳ Чекаємо: мережа {m.grid_import_kw:.1f} кВт > {GRID_POWER_MAX_KW:g}")

    else:
        log("Змін не потрібно")
        if action:
            notify("Кнопка вже неактуальна — бойлер не в черзі.")

    # ---- еко-режим: повернути струм заряду, коли мережа розвантажилась ----
    if eco and grid_ok and not eco_started_now:  # щойно натиснуту кнопку не оцінюємо
        if m.grid_import_kw < GRID_POWER_MAX_KW:
            restore_charge(f"мережа {m.grid_import_kw:.1f} кВт < {GRID_POWER_MAX_KW:g}")
        else:
            log(f"🔋 Еко-режим: мережа {m.grid_import_kw:.1f} кВт, чекаємо < {GRID_POWER_MAX_KW:g}")

    # ---- бойлер увімкнули вручну під час блекауту — нагадати один раз ----
    if not grid_ok and socket_on:
        if not manual_warned:
            log("⚠️ Бойлер працює від АКБ (ручний режим) — нагадуємо")
            notify(f"⚠️ Світла нема, а бойлер увімкнений вручну — працює від АКБ.\n"
                   f"{m.status()}\nНе забудь вимкнути.")
            manual_warned = True
    else:
        manual_warned = False

    new_state = {"grid_ok": grid_ok, "manual_warned": manual_warned, "boiler_pending": pending,
                 "pending_msgs": pending_msgs, "eco": eco, "tg_offset": state.get("tg_offset", 0)}
    if {k: state.get(k) for k in new_state} != new_state:
        new_state["updated"] = now_iso()
        save_state(new_state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # падаємо з ненульовим кодом, щоб run був червоним
        log(f"❌ {e}")
        sys.exit(1)
