import yfinance as yf
import pandas as pd
from flask import Flask
import threading
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands, AverageTrueRange
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------
# CONFIGURACIÓN
# -------------------
TOKEN = "8172753785:AAF0pHsdL_9G3P6oR5MaY4799s_TjmR_eJQ"
CHAT_ID = "7590209265"

# Activo Yahoo Finance (futuros oro COMEX en USD)
activo_yahoo = "GC=F"

# Parámetros técnicos
umbral_resistencia = 2000
rsi_high, rsi_low = 70, 30

# Ajuste CFD dinámico
ajuste_cfd_manual = None   # se podrá definir con /set_cfd


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
            precio_cfd_simulado = precio_futuros - 23  # aproximación
            ajuste = precio_cfd_simulado - precio_futuros
            return ajuste
    except Exception as e:
        print("Error calculando ajuste:", e)
    return -23


def ajustar_a_cfd(precio):
    ajuste = calcular_ajuste_cfd()
    if precio:
        return precio + ajuste
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
        "1m": yf.download(activo_yahoo, period="1d", interval="1m", auto_adjust=True),
        "5m": yf.download(activo_yahoo, period="3d", interval="5m", auto_adjust=True),
        "15m": yf.download(activo_yahoo, period="5d", interval="15m", auto_adjust=True),
    }
    for key in frames:
        frames[key] = calcular_indicadores(frames[key])
    return frames


# -------------------
# ANÁLISIS DE OPORTUNIDAD
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
# GENERADOR DE RECOMENDACIONES
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
        return f"📈 Recomendación CFD (eToro): ABRIR COMPRA\n🎯 Entrada: {entrada:.2f}\n🛑 Stop Loss: {sl:.2f}\n✅ Take Profit: {tp:.2f} (ATR={atr:.2f})"

    elif "VENTA" in signal[0]:
        entrada = spot_cfd
        sl = min(entrada + atr, resistencia)
        tp = max(entrada - 2*atr, soporte)
        return f"📉 Recomendación CFD (eToro): ABRIR VENTA\n🎯 Entrada: {entrada:.2f}\n🛑 Stop Loss: {sl:.2f}\n✅ Take Profit: {tp:.2f} (ATR={atr:.2f})"

    else:
        return "🤔 Mercado con incertidumbre, posible volatilidad."


# -------------------
# SOPORTES / RESISTENCIAS
# -------------------
def calcular_sr():
    df = yf.download(activo_yahoo, period="3d", interval="15m", auto_adjust=True)
    if df.empty:
        return None, None
    soporte = df["Low"].min(skipna=True).item()
    resistencia = df["High"].max(skipna=True).item()
    return soporte, resistencia


def evaluar_sr(spot, soporte, resistencia):
    if not spot or not soporte or not resistencia:
        return "⚠️ No se pudieron calcular soportes/resistencias"

    spot_cfd = ajustar_a_cfd(spot)
    margen = spot_cfd * 0.003
    mensajes = []

    if abs(spot_cfd - soporte) <= margen:
        mensajes.append(f"🟢 Precio cerca del SOPORTE clave: {soporte:.2f}")
    if abs(spot_cfd - resistencia) <= margen:
        mensajes.append(f"🔴 Precio cerca de la RESISTENCIA clave: {resistencia:.2f}")
    if spot_cfd < soporte:
        mensajes.append(f"❌ RUPTURA de SOPORTE → posible VENTA (CFD={spot_cfd:.2f})")
    if spot_cfd > resistencia:
        mensajes.append(f"🚀 RUPTURA de RESISTENCIA → posible COMPRA (CFD={spot_cfd:.2f})")

    return "\n".join(mensajes) if mensajes else "📊 Precio dentro de rango normal"


# -------------------
# VOLATILIDAD
# -------------------
def calcular_volatilidad():
    df = yf.download(activo_yahoo, period="5d", interval="15m", auto_adjust=True)
    if df.empty or len(df) < 20:
        return "⚠️ No hay suficientes datos para calcular volatilidad"

    df = df.dropna().copy()
    high = pd.Series(df["High"].squeeze(), index=df.index)
    low = pd.Series(df["Low"].squeeze(), index=df.index)
    close = pd.Series(df["Close"].squeeze(), index=df.index)

    try:
        atr = AverageTrueRange(high=high, low=low, close=close, window=14)
        serie_atr = atr.average_true_range()
        if serie_atr.empty:
            return "⚠️ No se pudo calcular ATR"

        valor_atr = serie_atr.iloc[-1].item()
        if valor_atr > 15:
            return f"⚡ Volatilidad ALTA (ATR={valor_atr:.2f})"
        elif valor_atr < 5:
            return f"🐢 Volatilidad BAJA (ATR={valor_atr:.2f})"
        else:
            return f"📊 Volatilidad NORMAL (ATR={valor_atr:.2f})"
    except Exception as e:
        return f"⚠️ Error en cálculo ATR: {e}"


# -------------------
# TAREAS PROGRAMADAS
# -------------------
async def revisar_mercado(context: ContextTypes.DEFAULT_TYPE):
    mensajes = []

    spot = obtener_precio_actual()
    if spot:
        mensajes.append(f"📊 Precio actual GC=F: {spot:.2f} USD (ajuste CFD aplicado)")
    else:
        mensajes.append("⚠️ No se pudo obtener precio actual")

    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)
    mensajes.extend(señales)

    mensajes.append(generar_recomendacion(señales, spot))

    soporte, resistencia = calcular_sr()
    if soporte and resistencia and spot:
        mensajes.append(f"📉 Soporte: {soporte:.2f} | 📈 Resistencia: {resistencia:.2f}")
        mensajes.append(evaluar_sr(spot, soporte, resistencia))

    mensajes.append(calcular_volatilidad())

    for msg in mensajes:
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)


async def revisar_oportunidad(context: ContextTypes.DEFAULT_TYPE):
    spot = obtener_precio_actual()
    frames = obtener_multiframe()
    señales = analizar_oportunidad(frames)

    if "COMPRA" in señales[0] or "VENTA" in señales[0]:
        msg = generar_recomendacion(señales, spot)
        await context.bot.send_message(chat_id=CHAT_ID, text="🚨 OPORTUNIDAD DETECTADA 🚨\n" + msg)


# -------------------
# PANEL DE CONTROL
# -------------------
async def set_resistance(update, context):
    global umbral_resistencia
    try:
        umbral_resistencia = float(context.args[0])
        await update.message.reply_text(f"✅ Resistencia ajustada a {umbral_resistencia}")
    except:
        await update.message.reply_text("⚠️ Usa: /set_resistance 2000")


async def set_rsi(update, context):
    global rsi_high, rsi_low
    try:
        rsi_high, rsi_low = map(float, context.args)
        await update.message.reply_text(f"✅ RSI ajustado: sobrecompra {rsi_high}, sobreventa {rsi_low}")
    except:
        await update.message.reply_text("⚠️ Usa: /set_rsi 80 20")


async def set_cfd(update, context):
    global ajuste_cfd_manual
    try:
        precio_cfd = float(context.args[0])
        spot = obtener_precio_actual()
        if spot:
            ajuste_cfd_manual = precio_cfd - spot
            await update.message.reply_text(
                f"✅ CFD ajustado. Precio GC=F: {spot:.2f}, CFD: {precio_cfd:.2f}, Dif: {ajuste_cfd_manual:.2f}"
            )
        else:
            await update.message.reply_text("⚠️ No se pudo obtener GC=F")
    except:
        await update.message.reply_text("⚠️ Usa: /set_cfd 3880")


async def status(update, context):
    global ajuste_cfd_manual
    await update.message.reply_text(
        f"📌 Config actual:\nResistencia: {umbral_resistencia}\nRSI: {rsi_high}/{rsi_low}\nAjuste CFD manual: {ajuste_cfd_manual}"
    )


# -------------------
# MAIN
# -------------------
def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("set_resistance", set_resistance))
    application.add_handler(CommandHandler("set_rsi", set_rsi))
    application.add_handler(CommandHandler("set_cfd", set_cfd))
    application.add_handler(CommandHandler("status", status))

    job_queue = application.job_queue
    job_queue.run_repeating(revisar_mercado, interval=1800, first=5)
    job_queue.run_repeating(revisar_oportunidad, interval=300, first=30)

    application.run_polling()


# -------------------
# FLASK SERVER PARA RENDER
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
