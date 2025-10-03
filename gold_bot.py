import yfinance as yf
import pandas as pd
from flask import Flask
import threading
from datetime import datetime, timedelta, timezone
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands, AverageTrueRange
from telegram.ext import Application, CommandHandler, ContextTypes
import os

# -------------------
# CONFIGURACIÓN
# -------------------
TOKEN = os.environ.get("TELEGRAM_TOKEN", "PON_AQUI_TU_TOKEN")
CHAT_IDS = ["7590209265", "8329147064"]

activo_futuros = "GC=F"
# Parámetros configurables
config = {
    "rsi_high": 70,
    "rsi_low": 30,
    "umbral_resistencia": 2000,
    "ajuste_cfd_manual": None
}

# Última oportunidad detectada
ultima_oportunidad = {"direccion": None, "precio": None, "hora": datetime.min}

# -------------------
# FUNCIONES DE PRECIOS
# -------------------
def obtener_precio_cfd():
    """
    Calcula el precio CFD del oro:
    - Si hay ajuste manual, usarlo sobre futuros.
    - Sino, intentar proxy (XAU=X o GLD).
    - Si falla todo, usar ajuste fijo (-23).
    """
    try:
        fut = yf.download("GC=F", period="1d", interval="1m", auto_adjust=True)
        if fut.empty:
            return None
        precio_fut = fut["Close"].iloc[-1]

        # Ajuste manual
        if config["ajuste_cfd_manual"] is not None:
            return float(precio_fut + config["ajuste_cfd_manual"])

        # Intentar proxies
        for proxy in ["XAU=X", "GLD"]:
            try:
                spot = yf.download(proxy, period="1d", interval="1m", auto_adjust=True)
                if not spot.empty:
                    precio_spot = spot["Close"].iloc[-1]
                    ajuste = precio_spot - precio_fut
                    return float(precio_fut + ajuste)
            except Exception:
                continue

        # Fallback
        return float(precio_fut - 23)

    except Exception as e:
        print("Error obteniendo precio CFD:", e)
        return None

# -------------------
# INDICADORES TÉCNICOS
# -------------------
def calcular_indicadores(df):
    if df.empty:
        return df
    close = df["Close"].squeeze()
    df["EMA20"] = EMAIndicator(close, window=20).ema_indicator()
    df["EMA50"] = EMAIndicator(close, window=50).ema_indicator()
    df["RSI"] = RSIIndicator(close, window=14).rsi()
    macd = MACD(close)
    df["MACD"] = macd.macd()
    df["MACD_Signal"] = macd.macd_signal()
    boll = BollingerBands(close)
    df["Boll_Upper"] = boll.bollinger_hband()
    df["Boll_Lower"] = boll.bollinger_lband()
    return df


def obtener_multiframe():
    frames = {
        "1m": yf.download(activo_futuros, period="1d", interval="1m", auto_adjust=True),
        "5m": yf.download(activo_futuros, period="3d", interval="5m", auto_adjust=True),
        "15m": yf.download(activo_futuros, period="5d", interval="15m", auto_adjust=True),
        "1h": yf.download(activo_futuros, period="1mo", interval="1h", auto_adjust=True),
        "4h": yf.download(activo_futuros, period="3mo", interval="4h", auto_adjust=True),
    }
    for key in frames:
        frames[key] = calcular_indicadores(frames[key])
    return frames

# -------------------
# ANÁLISIS
# -------------------
def analizar_oportunidad(frames):
    señales = []
    for tf, df in frames.items():
        if df.empty:
            señales.append(f"{tf}: ⚠️ Sin datos")
            continue

        ema20 = df["EMA20"].iloc[-1]
        ema50 = df["EMA50"].iloc[-1]
        rsi = df["RSI"].iloc[-1]

        if ema20 > ema50 and rsi < config["rsi_high"] - 5:
            señales.append(f"{tf}: ✅ COMPRA (RSI={rsi:.1f})")
        elif ema20 < ema50 and rsi > config["rsi_low"] + 5:
            señales.append(f"{tf}: ❌ VENTA (RSI={rsi:.1f})")
        else:
            señales.append(f"{tf}: 🤔 Incertidumbre (RSI={rsi:.1f})")

    buys = sum("COMPRA" in s for s in señales)
    sells = sum("VENTA" in s for s in señales)

    if buys >= 2:
        return ["🚀 Señal de COMPRA confirmada"] + señales
    elif sells >= 2:
        return ["🔻 Señal de VENTA confirmada"] + señales
    else:
        return ["🤔 Mercado indeciso"] + señales


def generar_recomendacion(signal, spot):
    if not spot:
        return "⚠️ Sin precio actual"

    df = yf.download(activo_futuros, period="5d", interval="15m", auto_adjust=True).dropna()
    if df.empty:
        return "⚠️ No hay datos para recomendación"

    high, low, close = df["High"], df["Low"], df["Close"]
    atr = AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1]
    soporte = low.min(skipna=True)
    resistencia = high.max(skipna=True)

    if "COMPRA" in signal[0]:
        entrada = spot
        sl = max(entrada - atr, soporte)
        tp = min(entrada + 2*atr, resistencia)
        return f"📈 CFD COMPRA\nEntrada: {entrada:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f} (ATR={atr:.2f})"
    elif "VENTA" in signal[0]:
        entrada = spot
        sl = min(entrada + atr, resistencia)
        tp = max(entrada - 2*atr, soporte)
        return f"📉 CFD VENTA\nEntrada: {entrada:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f} (ATR={atr:.2f})"
    else:
        return "🤔 Mercado sin dirección clara."

# -------------------
# MENSAJE UNIFICADO
# -------------------
def construir_mensaje():
    spot = obtener_precio_cfd()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    recomendacion = generar_recomendacion(señales, spot)

    mensaje = []
    if spot:
        mensaje.append(f"📊 Precio CFD actual: {spot:.2f} USD")
    mensaje.extend(señales)
    mensaje.append(recomendacion)
    return "\n".join(mensaje)

# -------------------
# TAREAS PROGRAMADAS
# -------------------
async def revisar_mercado(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now(timezone.utc).weekday() >= 5:  # Sábado o domingo
        return
    msg = construir_mensaje()
    for chat_id in CHAT_IDS:
        await context.bot.send_message(chat_id=chat_id, text=msg)


async def revisar_oportunidad(context: ContextTypes.DEFAULT_TYPE):
    global ultima_oportunidad
    if datetime.now(timezone.utc).weekday() >= 5:
        return

    spot = obtener_precio_cfd()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    recomendacion = generar_recomendacion(señales, spot)

    if "COMPRA" in señales[0]:
        direccion = "COMPRA"
    elif "VENTA" in señales[0]:
        direccion = "VENTA"
    else:
        direccion = "INDECISO"

    ahora = datetime.now()
    enviar = False

    if direccion != "INDECISO":
        if ultima_oportunidad["direccion"] != direccion:
            enviar = True
        elif ultima_oportunidad["precio"] is not None:
            if (ahora - ultima_oportunidad["hora"] > timedelta(minutes=20) and
                abs(spot - ultima_oportunidad["precio"]) >= 3):
                enviar = True

    if enviar:
        ultima_oportunidad = {"direccion": direccion, "precio": spot, "hora": ahora}
        msg = f"🚨 OPORTUNIDAD DETECTADA 🚨\n📊 Dirección: {direccion}\nPrecio: {spot:.2f} USD\n\n{recomendacion}"
        for chat_id in CHAT_IDS:
            await context.bot.send_message(chat_id=chat_id, text=msg)

# -------------------
# COMANDOS
# -------------------
async def start(update, context):
    help_text = (
        "🤖 Bot de Oro CFD\n\n"
        "Comandos rápidos:\n"
        "/p → Precio actual\n"
        "/o → Oportunidad actual\n"
        "/c → Configuración\n"
        "/s <param> <valor> → Ajustar parámetro\n"
        "\nComandos largos también disponibles:\n"
        "/price, /opportunity, /config, /set"
    )
    await update.message.reply_text(help_text)

help_cmd = start  # alias

async def price(update, context): await update.message.reply_text(construir_mensaje())
async def opportunity(update, context): await update.message.reply_text("📊 Oportunidad actual:\n" + construir_mensaje())
async def addid(update, context):
    if context.args:
        new_id = context.args[0]
        if new_id not in CHAT_IDS:
            CHAT_IDS.append(new_id)
            await update.message.reply_text(f"✅ Nuevo chat_id añadido: {new_id}")
        else:
            await update.message.reply_text("⚠️ Ese chat_id ya está autorizado.")
    else:
        await update.message.reply_text("Uso: /addid <id>")
async def show_config(update, context):
    msg = "⚙️ Configuración actual:\n" + "\n".join(f"{k}: {v}" for k, v in config.items())
    await update.message.reply_text(msg)
async def set_config(update, context):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /s <param> <valor>")
        return
    param, valor = context.args[0], context.args[1]
    if param in config:
        try:
            config[param] = float(valor)
        except:
            config[param] = valor
        await update.message.reply_text(f"✅ {param} actualizado a {valor}")
    else:
        await update.message.reply_text("⚠️ Parámetro no válido.")

# -------------------
# MAIN
# -------------------
def main():
    application = Application.builder().token(TOKEN).build()
    # Handlers cortos
    application.add_handler(CommandHandler("p", price))
    application.add_handler(CommandHandler("o", opportunity))
    application.add_handler(CommandHandler("c", show_config))
    application.add_handler(CommandHandler("s", set_config))
    # Handlers largos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("opportunity", opportunity))
    application.add_handler(CommandHandler("addid", addid))
    application.add_handler(CommandHandler("config", show_config))
    application.add_handler(CommandHandler("set", set_config))
    # Jobs
    job_queue = application.job_queue
    job_queue.run_repeating(revisar_mercado, interval=1800, first=10)
    job_queue.run_repeating(revisar_oportunidad, interval=300, first=60)
    application.run_polling()

# -------------------
# FLASK KEEP-ALIVE
# -------------------
app = Flask(__name__)

@app.route('/')
def home(): return "Bot is running!"

def run_flask(): app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    main()
