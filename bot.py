#!/usr/bin/env python3
"""
Monitor de citas - Permiso de lunas oscurecidas PNP (sistemas.policia.gob.pe)

Semi-automático: el bot hace login, abre "Reservar Cita" y te manda el captcha
por Telegram. TÚ respondes el captcha (y el código del correo si reservas).
El bot nunca resuelve el captcha ni toca el campo trampa anti-bots.

Comandos en Telegram:
  /check     revisar ahora
  /estado    ver estado y próximo intento
  /pausa     pausar revisiones automáticas
  /reanudar  reanudar
  /saltar    saltar el captcha actual
"""
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------- config
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Falta la variable de entorno {name}")
    return v

PNP_DNI = env("PNP_DNI", required=True)
PNP_CLAVE = env("PNP_CLAVE", required=True)
TG_TOKEN = env("TG_TOKEN", required=True)
TG_CHAT_ID = env("TG_CHAT_ID", "")
INTERVAL_MIN = int(env("INTERVAL_MIN", "45"))      # intervalo normal
PEAK_INTERVAL_MIN = int(env("PEAK_INTERVAL_MIN", "20"))  # intervalo en hora pico (mañanas)
PEAK_HOURS = env("PEAK_HOURS", "7-11")             # hora pico, inicio-fin
# Días y horas activas. 0=lunes ... 6=domingo. "días=inicio-fin;días=inicio-fin"
ACTIVE_SCHEDULE = env("ACTIVE_SCHEDULE", "0,1,2,3,4=7-16;5=7-13")
ANSWER_TIMEOUT_MIN = int(env("ANSWER_TIMEOUT_MIN", "10"))
SEDE_VALUE = env("SEDE_VALUE", "1")                 # 1 = LIMA-LA VICTORIA
HEADLESS = env("HEADLESS", "1") == "1"
DATA_DIR = Path(env("DATA_DIR", "./data"))

TZ = ZoneInfo("America/Lima")
BASE = "https://sistemas.policia.gob.pe/lunasoscurecidas/"
LOGIN_URL = BASE + "Solicitud_Menu.aspx"
SEG_URL = BASE + "Seguimiento.aspx"

# IDs reales de la página (revisados el 12/11/2026)
SEL_VER_TRAMITE = "#MainContent_gvProgramacion_btnAccion_0"   # el "ojito" de la lista de trámites
SEL_RESERVAR_ABRIR = "#MainContent_btnCita"
SEL_CAPTCHA_IMG = "#img-captcha"
SEL_CAPTCHA_INPUT = "#captcha-answer-input"
SEL_CAPTCHA_BTN = "#btn-captcha-verify"
SEL_SEDE = "#MainContent_idUcitas_cbosede"
SEL_FECHA = "#MainContent_idUcitas_cboFecha"
SEL_HORA = "#MainContent_idUcitas_cboHora"
SEL_CUPOS = "#MainContent_idUcitas_lblcupos"
SEL_RESERVAR_FINAL = "#MainContent_idUcitas_btgSiguiente"
SEL_CODIGO = "#MainContent_idUcitas_txtCodigo"
SEL_CODIGO_OK = "#MainContent_idUcitas_btnValidaAcepta"
SEL_MSG = "#MainContent_idUcitas_lblmensajess"
SEL_MSG_VALIDA = "#MainContent_idUcitas_lblmensajevalida"
SEL_MSG_ERROR = "#MainContent_idUcitas_lblmensajeError"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_CSV = DATA_DIR / "intentos.csv"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "bot.log")])
log = logging.getLogger("pnp")


# ---------------------------------------------------------------- telegram
class Telegram:
    def __init__(self, token, chat_id):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.offset = None

    def _call(self, method, **kw):
        r = requests.post(self.api + method, timeout=kw.pop("_timeout", 30), **kw)
        r.raise_for_status()
        return r.json()["result"]

    def send(self, text):
        try:
            self._call("sendMessage", data={"chat_id": self.chat_id, "text": text})
        except Exception as e:
            log.error("telegram send: %s", e)

    def photo(self, png: bytes, caption=""):
        try:
            self._call("sendPhoto", data={"chat_id": self.chat_id, "caption": caption},
                       files={"photo": ("img.png", png)})
        except Exception as e:
            log.error("telegram photo: %s", e)

    def updates(self, wait=25):
        """Mensajes nuevos SOLO de tu chat (ignora a cualquier otro)."""
        params = {"timeout": wait}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            res = self._call("getUpdates", data=params, _timeout=wait + 10)
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            return []
        out = []
        for u in res:
            self.offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) == self.chat_id and m.get("text"):
                out.append(m["text"].strip())
        return out

    def ask(self, timeout_min):
        """Espera una respuesta de texto. Devuelve None si vence o si escribes /saltar."""
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            for t in self.updates(wait=min(25, max(1, int(deadline - time.time())))):
                if t.lower() in ("/saltar", "/skip", "/no"):
                    return None
                return t
        return None


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(TZ)

def _parse_schedule(s):
    sched = {}
    for part in s.split(";"):
        if not part.strip():
            continue
        days, hours = part.split("=")
        h0, h1 = (int(x) for x in hours.split("-"))
        for d in days.split(","):
            sched[int(d)] = (h0, h1)
    return sched

SCHEDULE = _parse_schedule(ACTIVE_SCHEDULE)
PEAK = tuple(int(x) for x in PEAK_HOURS.split("-"))

def in_active_window(dt):
    w = SCHEDULE.get(dt.weekday())
    return bool(w) and w[0] <= dt.hour < w[1]

def interval_for(dt):
    return PEAK_INTERVAL_MIN if PEAK[0] <= dt.hour < PEAK[1] else INTERVAL_MIN

def next_run_after(dt):
    cand = dt + timedelta(minutes=interval_for(dt))
    if in_active_window(cand):
        return cand
    # fuera de horario: saltar al inicio de la siguiente ventana activa
    day = cand.replace(minute=0, second=0, microsecond=0)
    for _ in range(8 * 24):
        if in_active_window(day) and day > dt:
            return day
        day += timedelta(hours=1)
    return cand

def schedule_text():
    nombres = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    return ", ".join(f"{nombres[d]} {h0}-{h1}h" for d, (h0, h1) in sorted(SCHEDULE.items()))

def record(result, fechas="", nota=""):
    new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "dia", "resultado", "fechas", "nota"])
        t = now()
        w.writerow([t.isoformat(timespec="seconds"), t.strftime("%A"), result, fechas, nota])

def postback(page, action, timeout=30000):
    """Ejecuta una acción que dispara un postback ASP.NET y espera la respuesta."""
    try:
        with page.expect_response(lambda r: r.request.method == "POST" and "lunasoscurecidas" in r.url,
                                  timeout=timeout):
            action()
    except PWTimeout:
        log.warning("postback sin respuesta (timeout)")
    page.wait_for_timeout(1200)

def options(page, sel):
    return [(o["value"], o["text"].strip()) for o in page.eval_on_selector_all(
        f"{sel} option", "els => els.map(e => ({value: e.value, text: e.textContent}))")
        if o["value"] not in ("", "0", "00")]

def visible_text(page, sel):
    loc = page.locator(sel)
    try:
        if loc.count() and loc.first.is_visible():
            return loc.first.inner_text().strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- flujo
def login(page):
    page.goto(LOGIN_URL, wait_until="networkidle")
    # El primer combo con opción DNI es el del login
    combo = page.locator("select").filter(has=page.locator("option", has_text="CARNET DE EXTRANJERIA")).first
    combo.select_option("1")
    page.wait_for_timeout(800)
    page.get_by_placeholder("nro de documento", exact=True).fill(PNP_DNI)
    page.get_by_placeholder("clave", exact=True).fill(PNP_CLAVE)
    page.locator('input[type=submit][value="Ingresar"]').click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    if "Seguimiento" not in page.url:
        page.goto(SEG_URL, wait_until="networkidle")
    # Lista de trámites -> clic en el ojito para ver el detalle
    if not page.locator(SEL_RESERVAR_ABRIR).is_visible():
        if page.locator(SEL_VER_TRAMITE).count() == 0:
            raise RuntimeError(f"No encontré el trámite (ojito) en la lista (URL: {page.url})")
        postback(page, lambda: page.click(SEL_VER_TRAMITE))
        page.wait_for_load_state("networkidle")
    try:
        page.wait_for_selector(SEL_RESERVAR_ABRIR, state="visible", timeout=20000)
    except PWTimeout:
        raise RuntimeError(f"No encontré el botón 'Reservar Cita' en el detalle (URL: {page.url})")


def check(tg: Telegram, pw):
    browser = pw.chromium.launch(headless=HEADLESS)
    ctx = browser.new_context(locale="es-PE", timezone_id="America/Lima",
                              viewport={"width": 1100, "height": 900})
    page = ctx.new_page()
    try:
        login(page)
        postback(page, lambda: page.click(SEL_RESERVAR_ABRIR))
        page.wait_for_selector(SEL_CAPTCHA_IMG, state="visible", timeout=30000)

        # --- captcha: lo resuelves tú
        for intento in (1, 2):
            png = page.locator(SEL_CAPTCHA_IMG).screenshot()
            tg.photo(png, f"🔐 Captcha PNP ({now():%H:%M}). Responde solo el número. "
                          f"Tienes {ANSWER_TIMEOUT_MIN} min · /saltar para omitir")
            ans = tg.ask(ANSWER_TIMEOUT_MIN)
            if ans is None:
                tg.send("⏭️ Captcha omitido / sin respuesta. Lo intento en el próximo turno.")
                record("captcha_sin_respuesta")
                return
            page.fill(SEL_CAPTCHA_INPUT, ans)
            postback(page, lambda: page.click(SEL_CAPTCHA_BTN))
            if page.locator(SEL_SEDE).is_visible():
                break
            if intento == 1:
                tg.send("❌ Respuesta incorrecta, te mando otro.")
                page.wait_for_selector(SEL_CAPTCHA_IMG, state="visible", timeout=15000)
        else:
            tg.send("❌ Captcha incorrecto dos veces. Lo intento en el próximo turno.")
            record("captcha_incorrecto")
            return

        # --- sede -> fechas
        postback(page, lambda: page.select_option(SEL_SEDE, SEDE_VALUE))
        fechas = options(page, SEL_FECHA)
        if not fechas:
            msg = visible_text(page, SEL_MSG) or visible_text(page, SEL_MSG_ERROR)
            record("sin_fechas", nota=msg)
            nxt = next_run_after(now())
            tg.send(f"📭 Sin fechas disponibles ({now():%d/%m %H:%M}). "
                    f"Próximo intento: {nxt:%a %d/%m %H:%M}." + (f"\nWeb: {msg}" if msg else ""))
            return

        # --- hay fechas: recolectar horas y cupos
        slots = []
        for fv, ft in fechas:
            postback(page, lambda fv=fv: page.select_option(SEL_FECHA, fv))
            for hv, ht in options(page, SEL_HORA):
                postback(page, lambda hv=hv: page.select_option(SEL_HORA, hv))
                slots.append((fv, ft, hv, ht, visible_text(page, SEL_CUPOS)))
        record("HAY_FECHAS", fechas=" | ".join(f"{s[1]} {s[3]} ({s[4]})" for s in slots))

        lista = "\n".join(f"{i}. {s[1]} {s[3]} — cupos: {s[4] or '?'}" for i, s in enumerate(slots, 1))
        tg.send(f"🚨🚨 ¡HAY CITAS! 🚨🚨\n{lista}\n\nResponde el NÚMERO para reservar "
                f"o /no para no reservar. Tienes {ANSWER_TIMEOUT_MIN} min.")
        ans = tg.ask(ANSWER_TIMEOUT_MIN)
        if ans is None or not ans.isdigit() or not (1 <= int(ans) <= len(slots)):
            tg.send("👌 No reservé nada. Puedes entrar tú a la web ahora mismo.")
            return

        fv, ft, hv, ht, _ = slots[int(ans) - 1]
        postback(page, lambda: page.select_option(SEL_FECHA, fv))
        postback(page, lambda: page.select_option(SEL_HORA, hv))
        postback(page, lambda: page.click(SEL_RESERVAR_FINAL))

        # --- código enviado a tu correo
        if page.locator(SEL_CODIGO).is_visible():
            tg.send(f"📧 La PNP te mandó un código a tu correo para confirmar {ft} {ht}. "
                    f"Envíamelo aquí ({ANSWER_TIMEOUT_MIN} min).")
            code = tg.ask(ANSWER_TIMEOUT_MIN)
            if code is None:
                tg.send("⚠️ Sin código: la reserva NO se confirmó.")
                record("reserva_sin_codigo", fechas=f"{ft} {ht}")
                return
            page.fill(SEL_CODIGO, code)
            postback(page, lambda: page.click(SEL_CODIGO_OK))

        msg = " / ".join(filter(None, [visible_text(page, s) for s in (SEL_MSG, SEL_MSG_VALIDA, SEL_MSG_ERROR)]))
        tg.photo(page.screenshot(full_page=True), f"Resultado reserva {ft} {ht}: {msg or 'revisa la captura'}")
        tg.send("✅ Revisa tu correo y la web para confirmar que la cita quedó registrada. "
                "Pausé el bot: usa /reanudar si hace falta.")
        record("reserva_intentada", fechas=f"{ft} {ht}", nota=msg)
        return "pausar"

    except Exception as e:
        log.exception("error en check")
        record("error", nota=str(e)[:200])
        try:
            tg.photo(page.screenshot(full_page=True), f"⚠️ Error: {str(e)[:300]}")
        except Exception:
            tg.send(f"⚠️ Error: {str(e)[:300]}")
    finally:
        ctx.close()
        browser.close()


# ---------------------------------------------------------------- main loop
def whoami():
    tg = Telegram(TG_TOKEN, "0")
    res = tg._call("getUpdates", data={"timeout": 0})
    if not res:
        print("Escríbele cualquier cosa a tu bot en Telegram y vuelve a correr esto.")
    for u in res:
        c = (u.get("message") or {}).get("chat", {})
        print(f"chat_id={c.get('id')}  nombre={c.get('first_name')} {c.get('username', '')}")

def main():
    if "--whoami" in sys.argv:
        return whoami()
    if not TG_CHAT_ID:
        sys.exit("Falta TG_CHAT_ID. Corre: python bot.py --whoami")

    tg = Telegram(TG_TOKEN, TG_CHAT_ID)
    tg.updates(wait=0)  # descarta mensajes viejos
    paused = False
    nxt = now() if in_active_window(now()) else next_run_after(now())
    tg.send(f"🤖 Bot de lunas PNP activo.\nHorario: {schedule_text()}.\n"
            f"Cada {PEAK_INTERVAL_MIN} min de {PEAK[0]} a {PEAK[1]}h, luego cada {INTERVAL_MIN} min.\n"
            f"Primer intento: {nxt:%a %d/%m %H:%M}.\n/check /estado /pausa /reanudar")

    with sync_playwright() as pw:
        while True:
            force = False
            for t in tg.updates(wait=20):
                cmd = t.lower().split()[0]
                if cmd == "/check":
                    force = True
                elif cmd == "/pausa":
                    paused = True
                    tg.send("⏸️ Pausado.")
                elif cmd == "/reanudar":
                    paused = False
                    nxt = now()
                    tg.send("▶️ Reanudado.")
                elif cmd in ("/estado", "/start"):
                    tg.send(f"{'⏸️ Pausado' if paused else '▶️ Activo'}. "
                            f"Próximo intento: {nxt:%a %d/%m %H:%M}. Log: {LOG_CSV}")
            if force or (not paused and now() >= nxt):
                if check(tg, pw) == "pausar":
                    paused = True
                nxt = next_run_after(now())

if __name__ == "__main__":
    main()
