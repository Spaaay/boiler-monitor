"""One-shot перевірка: читає стан інвертора Deye і розетки Tuya, керує бойлером,
шле сповіщення й обробляє кнопки в Telegram. Запускається кожні 5 хв (GitHub Actions).
Усі секрети — з env (GitHub Secrets). Пам'ять між запусками — state.json у репо.

Правила:
  Світло зникло      → бойлер OFF, якщо був режим 40 А — повернути 80 А.
  Світло повернулось → читаємо налаштування заряду з інвертора.
                       BMS ріже заряд (ліміт < 80 А при SOC < 90) або SOC ≥ 90 → бойлер ON.
                       Інакше → бойлер OFF. В обох випадках — кнопки.
  Далі кожні 5 хв    → бойлер OFF і SOC став ≥ 90 → бойлер ON + кнопки (один раз).
                       режим 40 А і мережа < GRID_POWER_MAX_KW → 80 А + кнопки.
  Кнопки (будь-коли, поки є світло):
     🔋 40 А + бойлер ON       ⚡ 80 А + бойлер OFF
"""

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

# ================= TELEGRAM =================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_IDS = [c.strip() for c in os.environ.get("TG_CHAT_ID", "").split(",") if c.strip()]
TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ================= ЛОГІКА =================
VOLTAGE_THRESHOLD = float(os.environ.get("VOLTAGE_THRESHOLD", "100"))
GRID_POWER_MAX_KW = float(os.environ.get("GRID_POWER_MAX_KW", "2.5"))  # поріг повернення 40 → 80 А
CHARGE_A_NORMAL = int(os.environ.get("CHARGE_A_NORMAL", "80"))
CHARGE_A_ECO = int(os.environ.get("CHARGE_A_ECO", "40"))
SOC_BOILER_OK = float(os.environ.get("SOC_BOILER_OK", "90"))  # SOC, від якого бойлер вмикається сам
DEYE_READ_TIMEOUT_S = int(os.environ.get("DEYE_READ_TIMEOUT_S", "60"))
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
            {"text": f"⚡ {CHARGE_A_NORMAL} А, бойлер OFF", "callback_data": CB_FULL},
        ]]}
    for chat_id in TG_CHAT_IDS:
        try:
            m = tg("sendMessage", chat_id=chat_id, **payload)
            sent.append((chat_id, m["message_id"]))
        except Exception as e:
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
    """Натискання кнопок з моменту останнього запуску. Повертає останнє (eco/full)."""
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


def read_grid_charge_ampere(token: str) -> int | None:
    """Живе читання gridChargeAmpere з інвертора. Чекає до DEYE_READ_TIMEOUT_S, None при таймауті."""
    deadline = time.time() + DEYE_READ_TIMEOUT_S
    try:
        while True:
            d = deye("POST", "/strategy/dynamicControl/read", {"deviceSn": DEYE_DEVICE_SN}, token)
            if str(d.get("code")) == "2104004" and time.time() < deadline:  # інвертор зайнятий
                time.sleep(3)
                continue
            break
        order_id = d.get("orderId")
        if not order_id:
            log(f"⚠️ Deye dynamicControl/read: {d.get('msg')}")
            return None
        time.sleep(4)
        while time.time() < deadline:
            r = deye("POST", "/strategy/dynamicControl/readResult", {"orderId": order_id}, token)
            if str(r.get("code")) in ("2104013", "2104004"):
                time.sleep(2)
                continue
            if r.get("success") and r.get("gridChargeAmpere") is not None:
                return int(r["gridChargeAmpere"])
            log(f"⚠️ Deye readResult: {r.get('msg')}")
            return None
        log(f"⚠️ Deye readResult: таймаут {DEYE_READ_TIMEOUT_S} с")
    except Exception as e:
        log(f"⚠️ Deye read: {e}")
    return None


def set_grid_charge_ampere(token: str, amps: int) -> None:
    """Міняє gridChargeAmpere і чекає підтвердження інвертора (статус 666)."""
    for _ in range(10):
        d = deye("POST", "/order/battery/parameter/update",
                 {"deviceSn": DEYE_DEVICE_SN, "paramterType": "GRID_CHARGE_AMPERE", "value": amps}, token)
        if str(d.get("code")) == "2104004":
            time.sleep(3)
            continue
        break
    order_id = d.get("orderId")
    if not order_id:
        raise RuntimeError(f"Deye order: {d.get('msg')}")
    for _ in range(20):
        s = deye("GET", f"/order/{order_id}", None, token)
        status = str(s.get("status"))
        if status == "666":
            log(f"✅ Deye: струм заряду = {amps} А")
            return
        if status not in ("0", "100"):
            raise RuntimeError(f"Deye order {order_id} failed: {s}")
        time.sleep(2)
    raise RuntimeError(f"Deye order {order_id} timeout")


def _num(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, (default, ""))[0])
    except (TypeError, ValueError):
        return default


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
        self.voltage = _num(d, "GridVoltageL1L2")
        self.soc = _num(d, "SOC")
        self.grid_kw = _kw(d, "TotalGridPower")
        self.load_kw = _kw(d, "UPSLoadPower", "TotalConsumptionPower")
        self.batt_kw = _kw(d, "BatteryPower")
        self.batt_v = _num(d, "BatteryVoltage", 52)
        self.batt_temp = _num(d, "BMSTemperature")
        self.bms_limit_a = _num(d, "ChargeCurrentLimit")  # ліміт заряду від BMS, 0 = невідомо
        self.charge_a: int | None = None                  # gridChargeAmpere (налаштування інвертора)

    @property
    def grid_import_kw(self) -> float:
        return max(0.0, self.grid_kw or 0.0)

    @property
    def bms_limits(self) -> bool:
        """BMS не дає зарядити на повну (холодна АКБ), хоча заряд ще далеко не закінчився."""
        return 0 < self.bms_limit_a < CHARGE_A_NORMAL and self.soc < SOC_BOILER_OK

    def status(self) -> str:
        def f(v):
            return "?" if v is None else f"{v:.1f} кВт"
        batt = ""
        if self.batt_kw is not None:
            batt = f" ({'заряд' if self.batt_kw < 0 else 'розряд'} {abs(self.batt_kw):.1f} кВт)"
        charge = f"{self.charge_a} А" if self.charge_a is not None else "?"
        if self.bms_limits:
            charge += f" (BMS обмежує до {self.bms_limit_a:.0f} А)"
        return (f"Мережа: {f(self.grid_kw)} | Будинок: {f(self.load_kw)}\n"
                f"АКБ: {self.soc:.0f}%{batt}, {self.batt_temp:.0f} °C\n"
                f"Заряд: {charge}")


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
    return bool(dps[TUYA_SWITCH_CODE]), float(dps.get("cur_power") or 0) / 10


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

    cloud = tuya_cloud()
    socket_on, socket_w = get_socket(cloud)

    state = load_state()
    prev_grid_ok = state.get("grid_ok")
    manual_warned = bool(state.get("manual_warned"))
    wait_soc = bool(state.get("wait_soc"))      # бойлер OFF, чекаємо SOC ≥ SOC_BOILER_OK
    eco = state.get("eco")                      # {"since": iso} поки струм заряду 40 А
    btn_msgs = state.get("btn_msgs") or []      # повідомлення з активними кнопками
    m.charge_a = state.get("charge_a")          # останнє відоме налаштування заряду
    action = poll_buttons(state)

    log(f"Напруга: {m.voltage:.0f}V | SOC {m.soc:.0f}% | {m.batt_temp:.0f}°C | мережа {m.grid_import_kw:.1f} кВт "
        f"| BMS ліміт {m.bms_limit_a:.0f} А | заряд {m.charge_a} А | розетка {'ON' if socket_on else 'OFF'} "
        f"{socket_w:.0f} Вт | grid_ok={grid_ok} (було {prev_grid_ok}) wait_soc={wait_soc} eco={bool(eco)}")

    def send(text: str, buttons: bool = True) -> None:
        """Нове повідомлення; кнопки зі старого знімаємо, щоб активні були лише останні."""
        nonlocal btn_msgs
        remove_buttons(btn_msgs)
        btn_msgs = notify(text, buttons=buttons) if buttons else (notify(text), [])[1]

    def set_charge(amps: int) -> None:
        nonlocal eco
        if m.charge_a != amps:
            set_grid_charge_ampere(token, amps)
        m.charge_a = amps
        eco = {"since": now_iso()} if amps == CHARGE_A_ECO else None

    def boiler(on: bool) -> None:
        nonlocal socket_on
        if socket_on != on:
            set_socket(cloud, on)
            socket_on = on

    # ---------- світло зникло ----------
    if prev_grid_ok is None:
        log("Перший запуск — запам'ятовуємо стан")
        if m.charge_a is None:
            m.charge_a = read_grid_charge_ampere(token)

    elif prev_grid_ok and not grid_ok:
        log("‼️ Мережа зникла")
        boiler(False)
        wait_soc = False
        if eco:
            set_charge(CHARGE_A_NORMAL)
        send(f"🔴 Світло зникло\n{m.status()}\nБойлер вимкнено", buttons=False)

    # ---------- світло повернулось ----------
    elif not prev_grid_ok and grid_ok:
        log("💡 Мережа повернулась — читаємо налаштування інвертора")
        read = read_grid_charge_ampere(token)
        if read is not None:
            m.charge_a = read
            eco = {"since": now_iso()} if read == CHARGE_A_ECO else None
        elif m.charge_a is None:
            m.charge_a = CHARGE_A_NORMAL
        if m.bms_limits:
            boiler(True)
            wait_soc = False
            send(f"🟢 Світло повернулось\n{m.status()}\n"
                 f"BMS обмежує заряд (АКБ {m.batt_temp:.0f} °C) — бойлер увімкнено")
        elif m.soc >= SOC_BOILER_OK:
            boiler(True)
            wait_soc = False
            send(f"🟢 Світло повернулось\n{m.status()}\nАКБ ≥ {SOC_BOILER_OK:g}% — бойлер увімкнено")
        else:
            was_on = socket_on
            boiler(False)
            wait_soc = True
            note = " (був увімкнений вручну — вимкнув)" if was_on else ""
            send(f"🟢 Світло повернулось\n{m.status()}\n"
                 f"Бойлер вимкнено{note}. Увімкну сам при АКБ ≥ {SOC_BOILER_OK:g}%.")

    # ---------- світло є ----------
    elif grid_ok:
        if action == CB_ECO:
            log("🔘 ECO: 40 А + бойлер")
            set_charge(CHARGE_A_ECO)
            time.sleep(15)  # інвертор скидає потужність
            boiler(True)
            wait_soc = False
            send(f"🔋 Заряд {CHARGE_A_ECO} А, бойлер увімкнено.\n"
                 f"Поверну {CHARGE_A_NORMAL} А, коли мережа < {GRID_POWER_MAX_KW:g} кВт.\n{m.status()}")
        elif action == CB_FULL:
            log("🔘 FULL: 80 А, бойлер OFF")
            set_charge(CHARGE_A_NORMAL)
            boiler(False)
            wait_soc = m.soc < SOC_BOILER_OK  # вимкнув при SOC ≥ 90 — значить свідомо, більше не вмикаємо
            tail = (f" Увімкну сам при АКБ ≥ {SOC_BOILER_OK:g}%." if wait_soc
                    else " До наступного відключення сам не вмикатиму.")
            send(f"⚡ Заряд {CHARGE_A_NORMAL} А, бойлер вимкнено.{tail}\n{m.status()}")
        elif wait_soc and m.soc >= SOC_BOILER_OK:
            log("🔥 SOC досяг порогу — вмикаємо бойлер")
            boiler(True)
            wait_soc = False
            send(f"🔥 АКБ {m.soc:.0f}% — бойлер увімкнено\n{m.status()}")
        elif eco and m.grid_import_kw < GRID_POWER_MAX_KW:
            log("🔌 Мережа розвантажилась — повертаємо 80 А")
            set_charge(CHARGE_A_NORMAL)
            send(f"🔌 Мережа {m.grid_import_kw:.1f} кВт < {GRID_POWER_MAX_KW:g} — заряд повернуто на "
                 f"{CHARGE_A_NORMAL} А\n{m.status()}")
        else:
            log("Змін не потрібно")

    # ---------- світла нема ----------
    else:
        if action:
            notify("Світла нема — кнопки неактивні.")
        if socket_on and not manual_warned:
            log("⚠️ Бойлер працює від АКБ (ручний режим)")
            notify(f"⚠️ Світла нема, а бойлер увімкнений вручну — працює від АКБ.\n{m.status()}\nНе забудь вимкнути.")
            manual_warned = True
    if grid_ok or not socket_on:
        manual_warned = False

    new_state = {"grid_ok": grid_ok, "manual_warned": manual_warned, "wait_soc": wait_soc, "eco": eco,
                 "charge_a": m.charge_a, "btn_msgs": btn_msgs, "tg_offset": state.get("tg_offset", 0)}
    if {k: state.get(k) for k in new_state} != new_state:
        new_state["updated"] = now_iso()
        save_state(new_state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"❌ {e}")
        sys.exit(1)
