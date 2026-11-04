"""One-shot перевірка: читає напругу з Deye Cloud і стан розетки Tuya,
вмикає/вимикає розетку за потреби й виходить. Запускається з GitHub Actions cron.
Усі секрети — з env-змінних (GitHub Secrets)."""

import os
import sys
from datetime import datetime, timezone, timedelta

import requests
import tinytuya

# ================= DEYE =================
DEYE_APP_ID = os.environ["DEYE_APP_ID"]
DEYE_APP_SECRET = os.environ["DEYE_APP_SECRET"]
DEYE_EMAIL = os.environ["DEYE_EMAIL"]
DEYE_PASSWORD = os.environ["DEYE_PASSWORD"]  # sha256-хеш, як і раніше
DEYE_DEVICE_SN = os.environ["DEYE_DEVICE_SN"]
DEYE_BASE_URL = os.environ.get("DEYE_BASE_URL", "https://eu1-developer.deyecloud.com/v1.0")

# ================= TUYA =================
TUYA_REGION = os.environ.get("TUYA_REGION", "eu")
TUYA_ACCESS_ID = os.environ["TUYA_ACCESS_ID"]
TUYA_ACCESS_SECRET = os.environ["TUYA_ACCESS_SECRET"]
TUYA_DEVICE_ID = os.environ["TUYA_DEVICE_ID"]
TUYA_SWITCH_CODE = os.environ.get("TUYA_SWITCH_CODE", "switch_1")

# ================= ЛОГІКА =================
VOLTAGE_THRESHOLD = float(os.environ.get("VOLTAGE_THRESHOLD", "100"))

KYIV = timezone(timedelta(hours=2))


def log(msg: str) -> None:
    print(f"[{datetime.now(KYIV).strftime('%H:%M:%S')}] {msg}", flush=True)


def get_deye_token() -> str:
    url = f"{DEYE_BASE_URL}/account/token?appId={DEYE_APP_ID}"
    payload = {"appSecret": DEYE_APP_SECRET, "email": DEYE_EMAIL, "password": DEYE_PASSWORD}
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Deye token: {data.get('msg')}")
    return data["accessToken"]


def get_grid_voltage(token: str) -> tuple[float, str]:
    url = f"{DEYE_BASE_URL}/device/latest?appId={DEYE_APP_ID}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(url, json={"deviceList": [DEYE_DEVICE_SN]}, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("success") or not data.get("deviceDataList"):
        raise RuntimeError(f"Deye latest: {data.get('msg')}")

    voltage, soc = 0.0, "N/A"
    for item in data["deviceDataList"][0].get("dataList", []):
        if item.get("key") == "GridVoltageL1L2":
            voltage = float(item.get("value") or 0)
        elif item.get("key") == "SOC":
            soc = item.get("value")
    return voltage, soc


def tuya_cloud() -> tinytuya.Cloud:
    return tinytuya.Cloud(apiRegion=TUYA_REGION, apiKey=TUYA_ACCESS_ID, apiSecret=TUYA_ACCESS_SECRET)


def get_socket_state(cloud: tinytuya.Cloud) -> bool:
    status = cloud.getstatus(TUYA_DEVICE_ID)
    if not status.get("success"):
        raise RuntimeError(f"Tuya status: {status}")
    for dp in status.get("result", []):
        if dp.get("code") == TUYA_SWITCH_CODE:
            return bool(dp.get("value"))
    raise RuntimeError(f"Tuya: не знайдено dp '{TUYA_SWITCH_CODE}' у {status.get('result')}")


def set_socket(cloud: tinytuya.Cloud, turn_on: bool) -> None:
    res = cloud.sendcommand(TUYA_DEVICE_ID, {"commands": [{"code": TUYA_SWITCH_CODE, "value": turn_on}]})
    if not res.get("success"):
        raise RuntimeError(f"Tuya command: {res}")
    log(f"✅ Розетка {'увімкнена' if turn_on else 'вимкнена'}")


def main() -> int:
    token = get_deye_token()
    voltage, soc = get_grid_voltage(token)
    grid_ok = voltage >= VOLTAGE_THRESHOLD

    cloud = tuya_cloud()
    socket_on = get_socket_state(cloud)

    log(f"Напруга: {voltage}V | АКБ: {soc}% | Розетка: {'ON' if socket_on else 'OFF'}")

    if not grid_ok and socket_on:
        log("‼️ Мережа зникла — вимикаємо розетку")
        set_socket(cloud, False)
    elif grid_ok and not socket_on:
        log("💡 Мережа є — вмикаємо розетку")
        set_socket(cloud, True)
    else:
        log("Змін не потрібно")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # падаємо з ненульовим кодом, щоб run був червоним
        log(f"❌ {e}")
        sys.exit(1)
