import requests
import pandas as pd
from flask import Flask
import threading
from datetime import datetime, timedelta
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands, AverageTrueRange
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------
# CONFIGURACIÓN
# -------------------
TOKEN = "8172753785:AAF0pHsdL_9G3P6oR5MaY4799s_TjmR_eJQ"
TD_API_KEY = "9f502fd5361c4e22ae6379b01ad18b09"

# Lista de usuarios autorizados
CHAT_IDS = ["7590209265", "8329147064"]

# Parámetros técnicos
rsi_high, rsi_low = 70, 30
ultima_oportunidad = {"mensaje": None, "hora": datetime.min}


# -------------------
# FUNCIONES DE PRECIOS
# -------------------
def obtener_precio_actual():
    """Precio spot desde Twelve Data"""
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TD_API_KEY}"
        r = requests.get(url).json()
        if "price" in r:
            return float(r["price"])
    except Exception as e:
        print("Error Twelve Data precio:", e)
    return None


def obtener_velas(interval="1min", outputsize=200):
    """Obtiene velas desde Twelve Data (XAU/USD)"""
    try:
        url = (
            f"https://api.twelvedata.com/time_series?"
            f"symbol=XAU/USD&interval={interval}&outputsize={outputsize}&apikey={TD_API_KEY}"
        )
        r = requests.get(url).json()
        if "values" not in r:
            return pd.DataFrame()

        df = pd.DataFrame(r["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime")
        df = df.set_index("datetime")
        df = df.astype(float)
        return df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    except Exception as e:
        print(f"Error obteniendo velas {interval}:", e)
        return pd.DataFrame()


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
        "1m": obtener_velas("1min", 200),
        "5m": obtener_velas("5min", 200),
        "15m": obtener_velas("15min", 200),
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
            señales.append(f"{tf}: ⚠️ Sin datos disponibles")
            continue

        ema20 = df["EMA20"].iloc[-1]
        ema50 = df["EMA50"].iloc[-1]
        rsi = df["RSI"].iloc[-1]

        if ema20 > ema50 and rsi < 65:
            señales.append(f"{tf}: ✅ posible COMPRA (EMA20>EMA50, RSI={rsi:.1f})")
        elif ema20 < ema50 and rsi > 35:
            señales.append(f"{tf}: ❌ posible VENTA (EMA20<EMA50, RSI={rsi:.1f})")
        else:
            señales.append(f"{tf}: 🤔 sin señal clara (RSI={rsi:.1f})")

    buys = sum("COMPRA" in s for s in señales)
    sells = sum("VENTA" in s for s in señales)

    if buys >= 2:
        return ["🚀 Señal de **COMPRA** confirmada"] + señales
    elif sells >= 2:
        return ["🔻 Señal de **VENTA** confirmada"] + señales
    else:
        return ["🤔 Mercado indeciso"] + señales


# -------------------
# RECOMENDACIONES
# -------------------
def generar_recomendacion(signal, spot):
    if not spot:
        return "⚠️ No se pudo calcular recomendación (sin precio actual)"

    df = obtener_velas("15min", 200)
    if df.empty or len(df) < 20:
        return "⚠️ Datos insuficientes para ATR"

    high, low, close = df["High"], df["Low"], df["Close"]
    atr = AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1]

    soporte = df["Low"].tail(50).min(skipna=True)
    resistencia = df["High"].tail(50).max(skipna=True)

    if "COMPRA" in signal[0]:
        sl = max(spot - atr, soporte)
        tp = min(spot + 2 * atr, resistencia)
        return f"📈 COMPRA CFD\nEntrada: {spot:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f} (ATR={atr:.2f})"
    elif "VENTA" in signal[0]:
        sl = min(spot + atr, resistencia)
        tp = max(spot - 2 * atr, soporte)
        return f"📉 VENTA CFD\nEntrada: {spot:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f} (ATR={atr:.2f})"
    else:
        return "🤔 Mercado con incertidumbre."


# -------------------
# TAREAS PROGRAMADAS
# -------------------
async def revisar_mercado(context: ContextTypes.DEFAULT_TYPE):
    spot = obtener_precio_actual()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    mensajes = []

    if spot:
        mensajes.append(f"📊 Precio actual XAU/USD: {spot:.2f} USD")
    mensajes.extend(señales)
    mensajes.append(generar_recomendacion(señales, spot))

    for chat_id in CHAT_IDS:
        for msg in mensajes:
            await context.bot.send_message(chat_id=chat_id, text=msg)


async def revisar_oportunidad(context: ContextTypes.DEFAULT_TYPE):
    global ultima_oportunidad
    spot = obtener_precio_actual()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    msg = generar_recomendacion(señales, spot)
    ahora = datetime.now()

    if ("COMPRA" in señales[0] or "VENTA" in señales[0]) and \
       (ultima_oportunidad["mensaje"] != msg or ahora - ultima_oportunidad["hora"] > timedelta(minutes=30)):
        ultima_oportunidad = {"mensaje": msg, "hora": ahora}
        for chat_id in CHAT_IDS:
            await context.bot.send_message(chat_id=chat_id, text="🚨 OPORTUNIDAD DETECTADA 🚨\n" + msg)


# -------------------
# COMANDOS
# -------------------
async def price(update, context):
    spot = obtener_precio_actual()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    msg = generar_recomendacion(señales, spot)
    await update.message.reply_text(f"📊 Precio spot: {spot:.2f} USD\n" + msg)


async def opportunity(update, context):
    spot = obtener_precio_actual()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    msg = generar_recomendacion(señales, spot)
    await update.message.reply_text("📊 Oportunidad actual:\n" + msg)


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


async def listids(update, context):
    await update.message.reply_text("📋 Lista de chat_ids autorizados:\n" + "\n".join(CHAT_IDS))


async def help_cmd(update, context):
    help_text = (
        "🤖 Bot de Oro CFD\n\n"
        "Comandos disponibles:\n"
        "/price → Ver precio actual y recomendación\n"
        "/opportunity → Ver oportunidad actual\n"
        "/addid <id> → Añadir chat_id autorizado\n"
        "/listids → Ver todos los chat_ids autorizados\n"
        "/help → Mostrar esta ayuda"
    )
    await update.message.reply_text(help_text)


# -------------------
# MAIN
# -------------------
def main():
    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("opportunity", opportunity))
    application.add_handler(CommandHandler("addid", addid))
    application.add_handler(CommandHandler("listids", listids))
    application.add_handler(CommandHandler("help", help_cmd))

    # Jobs
    job_queue = application.job_queue
    job_queue.run_repeating(revisar_mercado, interval=1800, first=5)
    job_queue.run_repeating(revisar_oportunidad, interval=300, first=30)

    application.run_polling()


# FLASK KEEP-ALIVE
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    main()
