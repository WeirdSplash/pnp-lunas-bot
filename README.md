# Bot de citas – Lunas oscurecidas PNP

Revisa cada X minutos si hay fechas para el peritaje y te avisa por Telegram.
**Semi-automático:** el captcha y el código del correo los respondes tú desde el celular.
El bot no resuelve captchas ni toca el campo anti-bots de la web.

## 1. Crear el bot de Telegram (2 min)
1. En Telegram abre **@BotFather** → `/newbot`.
2. Ponle un nombre (ej. `Lunas PNP Joice`) y un username que termine en `bot` (ej. `lunas_pnp_joice_bot`).
3. BotFather te da un **token** tipo `123456:ABC-...` → eso es `TG_TOKEN`. No lo compartas.
4. Abre tu bot y mándale `/start` (necesario para que te pueda escribir).

## 2. VM en AWS
- **Instancia:** `t4g.small` (2 GB RAM, ARM) o `t3.small`. Chromium no se lleva bien con 1 GB.
- **AMI:** Ubuntu 24.04. Disco de 10 GB.
- **Security group:** solo entrada SSH (22) desde tu IP. El bot no expone puertos (usa long-polling hacia Telegram).
- **Región:** `sa-east-1` (São Paulo) o `us-east-1`.

> ⚠️ Antes de instalar todo, prueba que la web de la PNP responda desde AWS (algunos sitios del Estado bloquean IPs extranjeras o de nube):
> ```bash
> curl -sI https://sistemas.policia.gob.pe/lunasoscurecidas/Solicitud_Menu.aspx | head -1
> ```
> Si sale `HTTP/... 200` estás bien. Si da timeout o 403, córrelo en tu Mac (mismos pasos, sin systemd) o en un server en Perú.

## 3. Instalación
```bash
sudo mkdir -p /opt/pnp-lunas-bot && sudo chown ubuntu:ubuntu /opt/pnp-lunas-bot
# copia bot.py, requirements.txt, .env.example y pnp-lunas-bot.service a /opt/pnp-lunas-bot
cd /opt/pnp-lunas-bot
sudo apt update && sudo apt install -y python3-venv
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
sudo timedatectl set-timezone America/Lima

cp .env.example .env && chmod 600 .env
nano .env                # completa PNP_DNI, PNP_CLAVE, TG_TOKEN
set -a; . ./.env; set +a
python bot.py --whoami   # te imprime tu chat_id → ponlo en TG_CHAT_ID dentro de .env
```

Primera prueba en primer plano:
```bash
set -a; . ./.env; set +a
python bot.py            # en Telegram manda /check y responde el captcha
```

Si todo va bien, como servicio:
```bash
sudo cp pnp-lunas-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pnp-lunas-bot
journalctl -u pnp-lunas-bot -f
```

## 4. Uso
| Telegram | Qué hace |
|---|---|
| `/check` | revisa ahora |
| `/estado` | estado y próximo intento |
| `/pausa` / `/reanudar` | pausa o reanuda |
| `/saltar` | omite el captcha actual |

Cuando hay fechas: te manda la lista numerada → respondes el número → la PNP te manda un código al correo → se lo pasas al bot → te manda una captura con el resultado y se pausa.

## 5. Descubrir cuándo liberan cupos
Cada intento queda en `data/intentos.csv` (hora, día, resultado). Después de unos días:
```bash
grep HAY_FECHAS data/intentos.csv
```
Si los cupos aparecen siempre, por ejemplo, los lunes a las 8am, ajusta `ACTIVE_SCHEDULE`, `PEAK_HOURS` y `PEAK_INTERVAL_MIN` para revisar solo en ese momento y más seguido.

## Notas
- Si la PNP cambia la web (IDs o un reCAPTCHA real), el bot te manda la captura del error y habrá que ajustar los selectores al inicio de `bot.py`.
- Tus credenciales viven solo en `.env` de tu VM (`chmod 600`). Si quieres más seguridad, guárdalas en AWS SSM Parameter Store.
- No bajes `INTERVAL_MIN` a menos de ~15 min: no ganas nada y es carga innecesaria para la web.
