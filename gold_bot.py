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
CHAT_IDS = ["7590209265", "8329147064"]

API_KEY_TWELVE = "9f502fd5361c4e22ae6379b01ad18b09"
SYMBOL = "XAU/USD"

# Variables globales
ajuste_cfd_manual = None
ultimo_spot = None
ultima_oportunidad = {"mensaje": None, "hora": datetime.min}

# -------------------
# FUNCIONES DE PRECIOS
# -------------------
def obtener_precio_twelve():
    """Obtiene el precio spot en tiempo real de XAU/USD desde Twelve Data"""
    try:
        url = f"https://api.twelvedata.com/price?symbol={SYMBOL}&apikey={API_KEY_TWELVE}"
        r = requests.get(url).json()
        if "price" in r:
            return float(r["price"])
    except Exception as e:
        print("Error Twelve Data:", e)
    return None

def obtener_precio_cfd():
    """Aplica el ajuste manual (si existe) al precio spot"""
    global ultimo_spot, ajuste_cfd_manual
    spot = obtener_precio_twelve()
    ultimo_spot = spot
    if not spot:
        return None
    if ajuste_cfd_manual is not None:
        return spot + ajuste_cfd_manual
    return spot

# -------------------
# INDICADORES TÉCNICOS
# -------------------
def calcular_indicadores(df):
    if df.empty:
        return df
    close = df["close"].squeeze()
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
    """Descarga datos de 3 marcos temporales desde Twelve Data"""
    frames = {}
    intervals = {"1m": "1min", "5m": "5min", "15m": "15min"}
    for key, val in intervals.items():
        try:
            url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval={val}&outputsize=200&apikey={API_KEY_TWELVE}"
            r = requests.get(url).json()
            if "values" in r:
                df = pd.DataFrame(r["values"])
                df = df.rename(columns={"datetime":"time"})
                df = df.iloc[::-1].reset_index(drop=True)  # ordenar por tiempo
                df["close"] = df["close"].astype(float)
                df["high"] = df["high"].astype(float)
                df["low"] = df["low"].astype(float)
                df = calcular_indicadores(df)
                frames[key] = df
        except Exception as e:
            print(f"Error obteniendo {key}:", e)
            frames[key] = pd.DataFrame()
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
        return ["🤔 Señal indecisa"] + señales

# -------------------
# RECOMENDACIONES
# -------------------
def generar_recomendacion(signal, spot):
    if not spot:
        return "⚠️ No se pudo calcular recomendación (sin precio actual)"

    spot_cfd = ajustar_a_cfd(spot)

    df = yf.download(activo_yahoo, period="10d", interval="15m", auto_adjust=True).dropna()
    if df.empty or len(df) < 30:
        return "⚠️ Datos insuficientes para calcular recomendación"

    high, low, close = df["High"], df["Low"], df["Close"]

    # ATR dinámico
    atr_series = AverageTrueRange(high, low, close, window=14).average_true_range()
    atr = atr_series.iloc[-1].item()

    # Rango medio diario (ADR)
    df_daily = yf.download(activo_yahoo, period="10d", interval="1d", auto_adjust=True).dropna()
    adr = (df_daily["High"] - df_daily["Low"]).mean().item()

    soporte = low.min(skipna=True).item()
    resistencia = high.max(skipna=True).item()

    # Multiplicador dinámico ATR (más prudente si mercado lento)
    mult = 1.5 if atr < adr/2 else 2.0

    if "COMPRA" in signal[0]:
        entrada = spot_cfd
        tp = entrada + mult * atr
        sl = entrada - atr

        # Validar TP alcanzable
        if tp > resistencia or (tp - entrada) > adr:
            return f"📈 COMPRA detectada, pero TP poco realista (resistencia cercana o fuera del rango ADR). Mejor esperar."
        return f"📈 COMPRA CFD\n🎯 Entrada: {entrada:.2f}\n🛑 SL: {sl:.2f}\n✅ TP: {tp:.2f} (ATR={atr:.2f}, ADR={adr:.2f})"

    elif "VENTA" in signal[0]:
        entrada = spot_cfd
        tp = entrada - mult * atr
        sl = entrada + atr

        if tp < soporte or (entrada - tp) > adr:
            return f"📉 VENTA detectada, pero TP poco realista (soporte cercano o fuera del rango ADR). Mejor esperar."
        return f"📉 VENTA CFD\n🎯 Entrada: {entrada:.2f}\n🛑 SL: {sl:.2f}\n✅ TP: {tp:.2f} (ATR={atr:.2f}, ADR={adr:.2f})"

    else:
        return "🤔 Mercado con incertidumbre."

# -------------------
# TAREAS PROGRAMADAS
# -------------------
async def revisar_mercado(context: ContextTypes.DEFAULT_TYPE):
    spot = obtener_precio_cfd()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    recomendacion = generar_recomendacion(señales, spot)

    mensajes = []
    if spot:
        mensajes.append(f"📊 Precio actual XAU/USD: {spot:.2f} USD")
    mensajes.extend(señales)
    mensajes.append(recomendacion)

    for chat_id in CHAT_IDS:
        for msg in mensajes:
            await context.bot.send_message(chat_id=chat_id, text=msg)

async def revisar_oportunidad(context: ContextTypes.DEFAULT_TYPE):
    global ultima_oportunidad
    spot = obtener_precio_cfd()
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
# COMANDOS TELEGRAM
# -------------------
async def price(update, context):
    spot = obtener_precio_cfd()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    msg = generar_recomendacion(señales, spot)
    await update.message.reply_text(f"📊 Precio actual: {spot:.2f} USD\n" + "\n".join(señales) + "\n" + msg)

async def opportunity(update, context):
    spot = obtener_precio_cfd()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    msg = generar_recomendacion(señales, spot)
    await update.message.reply_text("📊 Oportunidad actual:\n" + "\n".join(señales) + "\n" + msg)

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

async def set_precio(update, context):
    global ajuste_cfd_manual, ultimo_spot
    if not context.args:
        await update.message.reply_text("Uso: /setprecio <valor>")
        return
    try:
        precio_etoro = float(context.args[0])
        if ultimo_spot:
            ajuste_cfd_manual = precio_etoro - ultimo_spot
            await update.message.reply_text(
                f"✅ Ajuste aplicado: {ajuste_cfd_manual:.2f} USD\n"
                f"(spot={ultimo_spot:.2f}, eToro={precio_etoro:.2f})"
            )
        else:
            await update.message.reply_text("⚠️ No hay spot cargado aún, prueba en 1 min.")
    except ValueError:
        await update.message.reply_text("⚠️ Valor no válido.")

async def help_cmd(update, context):
    help_text = (
        "🤖 Bot de Oro CFD\n\n"
        "Comandos disponibles:\n"
        "/price → Ver precio e indicadores\n"
        "/opportunity → Revisar oportunidad\n"
        "/setprecio <valor> → Ajustar al precio de eToro\n"
        "/addid <id> → Añadir usuario\n"
        "/listids → Listar usuarios autorizados\n"
        "/help → Ver esta ayuda"
    )
    await update.message.reply_text(help_text)

# -------------------
# MAIN
# -------------------
def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("opportunity", opportunity))
    application.add_handler(CommandHandler("addid", addid))
    application.add_handler(CommandHandler("listids", listids))
    application.add_handler(CommandHandler("setprecio", set_precio))
    application.add_handler(CommandHandler("help", help_cmd))

    job_queue = application.job_queue
    job_queue.run_repeating(revisar_mercado, interval=1800, first=5)
    job_queue.run_repeating(revisar_oportunidad, interval=300, first=30)

    application.run_polling()

# -------------------
# FLASK KEEP-ALIVE
# -------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    main()
