import yfinance as yf
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

# Lista de usuarios autorizados
CHAT_IDS = ["7590209265", "8329147064"]

activo_yahoo = "GC=F"   # Futuros COMEX
umbral_resistencia = 2000
rsi_high, rsi_low = 70, 30
ajuste_cfd_manual = None

# Control de duplicados de oportunidades
ultima_oportunidad = {"mensaje": None, "hora": datetime.min}


# -------------------
# FUNCIONES DE PRECIOS
# -------------------
def obtener_precio_actual():
    try:
        df = yf.download(activo_yahoo, period="1d", interval="1m", auto_adjust=True)
        if not df.empty:
            return df["Close"].iloc[-1].item()
    except Exception as e:
        print("Error Yahoo precio:", e)
    return None


def calcular_ajuste_cfd():
    global ajuste_cfd_manual
    if ajuste_cfd_manual is not None:
        return ajuste_cfd_manual
    try:
        df = yf.download("GC=F", period="1d", interval="1m", auto_adjust=True)
        if not df.empty:
            precio_futuros = df["Close"].iloc[-1].item()
            return (precio_futuros - 23) - precio_futuros
    except Exception as e:
        print("Error calculando ajuste:", e)
    return -23


def ajustar_a_cfd(precio):
    ajuste = calcular_ajuste_cfd()
    return precio + ajuste if precio else None


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
        "1m": yf.download(activo_yahoo, period="1d", interval="1m", auto_adjust=True),
        "5m": yf.download(activo_yahoo, period="3d", interval="5m", auto_adjust=True),
        "15m": yf.download(activo_yahoo, period="5d", interval="15m", auto_adjust=True),
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

        precio = df["Close"].iloc[-1].item()
        ema20 = df["EMA20"].iloc[-1].item()
        ema50 = df["EMA50"].iloc[-1].item()
        rsi = df["RSI"].iloc[-1].item()

        if ema20 > ema50 and rsi < 65:
            señales.append(f"{tf}: ✅ posible COMPRA (EMA20>EMA50, RSI={rsi:.1f})")
        elif ema20 < ema50 and rsi > 35:
            señales.append(f"{tf}: ❌ posible VENTA (EMA20<EMA50, RSI={rsi:.1f})")
        else:
            señales.append(f"{tf}: 🤔 sin señal clara (RSI={rsi:.1f})")

    buys = sum("COMPRA" in s for s in señales)
    sells = sum("VENTA" in s for s in señales)

    if buys >= 2:
        return ["🚀 Señal de **COMPRA** confirmada en varios marcos"] + señales
    elif sells >= 2:
        return ["🔻 Señal de **VENTA** confirmada en varios marcos"] + señales
    else:
        return ["🤔 Señal indecisa"] + señales


# -------------------
# RECOMENDACIONES
# -------------------
def generar_recomendacion(signal, spot):
    if not spot:
        return "⚠️ No se pudo calcular recomendación (sin precio actual)"

    spot_cfd = ajustar_a_cfd(spot)
    df = yf.download(activo_yahoo, period="5d", interval="15m", auto_adjust=True).dropna()

    high = pd.Series(df["High"].values.ravel(), index=df.index)
    low = pd.Series(df["Low"].values.ravel(), index=df.index)
    close = pd.Series(df["Close"].values.ravel(), index=df.index)

    atr = AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1].item()
    soporte = df["Low"].min(skipna=True).item()
    resistencia = df["High"].max(skipna=True).item()

    if "COMPRA" in signal[0]:
        entrada = spot_cfd
        sl = max(entrada - atr, soporte)
        tp = min(entrada + 2*atr, resistencia)
        return f"📈 Recomendación CFD: COMPRA\n🎯 Entrada: {entrada:.2f}\n🛑 SL: {sl:.2f}\n✅ TP: {tp:.2f} (ATR={atr:.2f})"
    elif "VENTA" in signal[0]:
        entrada = spot_cfd
        sl = min(entrada + atr, resistencia)
        tp = max(entrada - 2*atr, soporte)
        return f"📉 Recomendación CFD: VENTA\n🎯 Entrada: {entrada:.2f}\n🛑 SL: {sl:.2f}\n✅ TP: {tp:.2f} (ATR={atr:.2f})"
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
        mensajes.append(f"📊 Precio actual GC=F: {spot:.2f} USD (ajuste CFD aplicado)")
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
    await update.message.reply_text("📊 Precio actual:\n" + msg)


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
