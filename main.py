"""
StarFx V14 - Candlestick Trading Bible Full Implementation
Includes: Pin Bar, Engulfing, Inside Bar, Fakey + Confluence
"""
import os
os.environ['MPLCONFIGDIR'] = '/tmp'
os.environ['MPLBACKEND'] = 'Agg'
import asyncio, json, logging, sqlite3, sys, threading, time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import numpy as np
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("starfx")

TELEGRAM_TOKEN = "8656945768:AAG1avs7PEkGlwJ6VI8cBiOyclOIqmyPjDA"
CHAT_ID = "-1004365660319"
if not TELEGRAM_TOKEN or not CHAT_ID:
    log.critical("Missing secrets"); sys.exit(1)

DB_PATH = os.environ.get("STARFX_DB_PATH", "trading_data.db")
WEEKDAY_SYMBOLS = ["XAU/USD", "GBP/USD", "R_75"]
WEEKEND_SYMBOLS = ["R_75", "R_100", "BOOM1000", "CRASH1000"]
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "10000"))
RISK_PER_TRADE_PCT = 0.01
MAX_DAILY_LOSS_PCT = 0.03
MAX_CONCURRENT_TRADES = 3
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

daily_stats = {"date": None, "losses_today": 0.0, "is_circuit_broken": False, "wins": 0, "losses": 0, "tp1_hits": 0}
active_trades = []
last_signal_time = {}

def is_paused(): return os.path.exists("PAUSE")
def get_active_symbols(): return WEEKEND_SYMBOLS if datetime.now(timezone.utc).weekday() >= 5 else WEEKDAY_SYMBOLS
def to_deriv_symbol(s):
    if "BOOM1000" in s: return "BOOM_1000"
    if "CRASH1000" in s: return "CRASH_1000"
    if s == "XAU/USD": return "frxXAUUSD"
    if s == "GBP/USD": return "frxGBPUSD"
    return s

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, *a): return
def start_dummy_server(): HTTPServer(("0.0.0.0", int(os.environ.get("PORT",10000))), HealthCheckHandler).serve_forever()
threading.Thread(target=start_dummy_server, daemon=True).start()

def init_db():
    c=sqlite3.connect(DB_PATH); c.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, bias TEXT, result INTEGER, ts TEXT)"); c.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)"); c.commit(); c.close()
def save_state():
    try: c=sqlite3.connect(DB_PATH); c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", ("active_trades", json.dumps(active_trades))); c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", ("daily_stats", json.dumps({**daily_stats, "date": str(daily_stats["date"]) if daily_stats["date"] else None}))); c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", ("last_signal_time", json.dumps(last_signal_time))); c.commit(); c.close()
    except: log.exception("save_state")
def load_state():
    try:
        c=sqlite3.connect(DB_PATH); cur=c.cursor()
        cur.execute("SELECT v FROM state WHERE k='active_trades'"); r=cur.fetchone();
        if r: active_trades.clear(); active_trades.extend(json.loads(r[0]))
        cur.execute("SELECT v FROM state WHERE k='daily_stats'"); r=cur.fetchone()
        if r: ds=json.loads(r[0]); daily_stats.update(ds)
        c.close()
    except: log.exception("load_state")
def record_trade_result(s,b,res):
    try: c=sqlite3.connect(DB_PATH); c.execute("INSERT INTO trades (symbol,bias,result,ts) VALUES (?,?,?,?)", (s,b,res,datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
    except: pass
def check_circuit_breaker():
    today=datetime.now(timezone.utc).date()
    if daily_stats["date"]!=today: daily_stats["date"]=today; daily_stats["losses_today"]=0.0; daily_stats["is_circuit_broken"]=False
    if daily_stats["losses_today"] >= ACCOUNT_BALANCE*MAX_DAILY_LOSS_PCT: daily_stats["is_circuit_broken"]=True; return False
    return not daily_stats["is_circuit_broken"]

def format_price(s,p): return f"{p:.2f}" if any(x in s for x in ["XAU","R_","BOOM","CRASH"]) else f"{p:.5f}"
def calculate_position_size(bal,risk_pct,dist): return round((bal*risk_pct)/dist,4) if dist else 0
def format_lots(sym,dist): return f"{calculate_position_size(ACCOUNT_BALANCE,RISK_PER_TRADE_PCT,dist)} units"

# Data
def fetch_data(symbol,tf,limit=100):
    try:
        import websocket; gran={'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400}
        d=to_deriv_symbol(symbol); ws=websocket.create_connection(DERIV_WS_URL, timeout=10)
        ws.send(json.dumps({"ticks_history":d,"count":limit,"end":"latest","granularity":gran.get(tf,300),"style":"candles"}))
        res=json.loads(ws.recv()); ws.close()
        if "candles" not in res: return None
        df=pd.DataFrame(res["candles"]); df['timestamp']=pd.to_datetime(df['epoch'],unit='s',utc=True); df.set_index('timestamp',inplace=True)
        df=df[['open','high','low','close']].astype(float); df['volume']=1000; return df
    except: log.exception("fetch_data %s",symbol); return None

def fetch_current_price(symbol):
    try:
        import websocket; d=to_deriv_symbol(symbol); ws=websocket.create_connection(DERIV_WS_URL,timeout=5)
        ws.send(json.dumps({"ticks":d})); res=json.loads(ws.recv()); ws.close()
        if "tick" in res: return float(res["tick"]["quote"])
    except: pass
    df=fetch_data(symbol,'1m',2); return df['close'].iloc[-1] if df is not None else None
def fetch_spread(s): return 0.0

# ===== CANDLESTICK BIBLE PATTERNS =====

def detect_pinbar(df):
    if len(df)<3: return None
    c1=df.iloc[-2]
    body=abs(c1['close']-c1['open']); uw=c1['high']-max(c1['close'],c1['open']); lw=min(c1['close'],c1['open'])-c1['low']
    if lw >= 2*body and uw <= 0.5*body and body>0: return "BULLISH_PINBAR"
    if uw >= 2*body and lw <= 0.5*body and body>0: return "BEARISH_PINBAR"
    return None

def detect_engulfing(df):
    if len(df)<3: return None
    c1=df.iloc[-2]; c0=df.iloc[-3]
    prev_bear=c0['close']<c0['open']; prev_bull=c0['close']>c0['open']; curr_bull=c1['close']>c1['open']; curr_bear=c1['close']<c1['open']
    # Book rule: second body must fully engulf first
    if curr_bull and prev_bear and c1['open']<=c0['close'] and c1['close']>=c0['open']: return "BULLISH_ENGULFING"
    if curr_bear and prev_bull and c1['open']>=c0['close'] and c1['close']<=c0['open']: return "BEARISH_ENGULFING"
    return None

def detect_inside_bar(df):
    # Book p140: inside bar = consolidation, trade with trend
    if len(df)<3: return None
    c1=df.iloc[-2]; c0=df.iloc[-3]
    if c1['high'] < c0['high'] and c1['low'] > c0['low']:
        return "INSIDE_BAR"
    return None

def detect_fakey(df):
    # Book p148: false breakout of inside bar
    if len(df)<4: return None
    c1=df.iloc[-2]; c2=df.iloc[-3]; c0=df.iloc[-4]
    # c2 is inside c0, then c1 false breaks then closes back inside
    inside = c2['high'] < c0['high'] and c2['low'] > c0['low']
    if not inside: return None
    # Bullish fakey: break below then close back up
    if c1['low'] < c2['low'] and c1['close'] > c2['low']:
        return "BULLISH_FAKEY"
    if c1['high'] > c2['high'] and c1['close'] < c2['high']:
        return "BEARISH_FAKEY"
    return None

def detect_morning_evening_star(df):
    if len(df)<4: return None
    c2=df.iloc[-4]; c1=df.iloc[-3]; c0=df.iloc[-2]
    # Morning star: bearish, small indecision, bullish closing above midpoint
    if c2['close']<c2['open'] and abs(c1['close']-c1['open']) < abs(c2['close']-c2['open'])*0.3 and c0['close']>c0['open'] and c0['close'] > (c2['open']+c2['close'])/2:
        return "MORNING_STAR"
    if c2['close']>c2['open'] and abs(c1['close']-c1['open']) < abs(c2['close']-c2['open'])*0.3 and c0['close']<c0['open'] and c0['close'] < (c2['open']+c2['close'])/2:
        return "EVENING_STAR"
    return None

def detect_harami(df):
    if len(df)<3: return None
    c1=df.iloc[-2]; c0=df.iloc[-3]
    if abs(c0['close']-c0['open'])>0 and abs(c1['close']-c1['open']) < abs(c0['close']-c0['open'])*0.5 and max(c1['open'],c1['close']) < max(c0['open'],c0['close']) and min(c1['open'],c1['close']) > min(c0['open'],c0['close']):
        return "BULLISH_HARAMI" if c0['close']<c0['open'] else "BEARISH_HARAMI"
    return None

# Support/Resistance + Confluence (Book p58-98)
def get_sr_levels(df, lookback=50):
    recent=df.iloc[-lookback:]
    highs=recent['high'].rolling(5,center=True).max(); lows=recent['low'].rolling(5,center=True).min()
    resistances=[h for h in highs if h==recent['high'].max() or list(highs).count(h)>1]
    supports=[l for l in lows if l==recent['low'].min() or list(lows).count(l)>1]
    swing_high=recent['high'].max(); swing_low=recent['low'].min()
    return swing_high, swing_low, recent['high'].max(), recent['low'].min()

def check_confluence(df, pattern):
    # Book p98: 4 factors = Trend + Level + Signal + 21 EMA + Fib
    close=df['close'].iloc[-2]
    ema21=df['close'].ewm(span=21).mean().iloc[-2]
    ema50=df['close'].ewm(span=50).mean().iloc[-2]
    swing_high, swing_low, _, _ = get_sr_levels(df)
    # Fib 50/61.8 from last swing
    fib_range=swing_high-swing_low
    fib50=swing_low+fib_range*0.5; fib61=swing_low+fib_range*0.618

    # Distance to key level
    near_support = abs(close - swing_low) / close < 0.005 or abs(close - fib50) / close < 0.003 or abs(close - fib61) / close < 0.003 or (close>ema21 and close>ema50)
    near_resistance = abs(close - swing_high) / close < 0.005 or abs(close - fib50) / close < 0.003 or abs(close - fib61) / close < 0.003 or (close<ema21 and close<ema50)

    # 21 EMA as dynamic support/resistance (book p89)
    above_ema = close > ema21
    below_ema = close < ema21

    score=0
    reasons=[]
    if pattern in ["BULLISH_PINBAR","BULLISH_ENGULFING","MORNING_STAR","BULLISH_HARAMI","BULLISH_FAKEY"]:
        if near_support: score+=1; reasons.append("Near Support/Fib")
        if above_ema: score+=1; reasons.append("Above 21EMA")
    else:
        if near_resistance: score+=1; reasons.append("Near Resistance/Fib")
        if below_ema: score+=1; reasons.append("Below 21EMA")

    return score, reasons, ema21, fib50, fib61, swing_high, swing_low

def analyze_structure(df):
    if len(df)<21: return "NEUTRAL"
    ema21=df['close'].ewm(span=21).mean().iloc[-1]
    curr=df['close'].iloc[-1]
    if curr>ema21 and df['close'].iloc[-5:].mean()>ema21: return "BULLISH"
    if curr<ema21 and df['close'].iloc[-5:].mean()<ema21: return "BEARISH"
    return "NEUTRAL"

def evaluate_aplus_setup(symbol):
    if not check_circuit_breaker() or len(active_trades)>=MAX_CONCURRENT_TRADES: return None
    h4=fetch_data(symbol,'4h'); h1=fetch_data(symbol,'1h'); m5=fetch_data(symbol,'5m',100)
    if h4 is None or h1 is None or m5 is None: return None
    h4_bias=analyze_structure(h4); h1_bias=analyze_structure(h1)
    if h4_bias!=h1_bias or h4_bias=="NEUTRAL": return None

    # Check ALL patterns from book
    patterns=[]
    for detector in [detect_pinbar, detect_engulfing, detect_inside_bar, detect_fakey, detect_morning_evening_star, detect_harami]:
        p=detector(m5)
        if p: patterns.append(p)
    if not patterns: return None

    # Pick strongest signal
    # Priority: Engulfing > Pinbar > Morning/Evening Star > Fakey > Inside Bar
    priority={"BULLISH_ENGULFING":5,"BEARISH_ENGULFING":5,"BULLISH_PINBAR":4,"BEARISH_PINBAR":4,"MORNING_STAR":4,"EVENING_STAR":4,"BULLISH_FAKEY":3,"BEARISH_FAKEY":3,"BULLISH_HARAMI":2,"BEARISH_HARAMI":2,"INSIDE_BAR":1}
    patterns.sort(key=lambda x: priority.get(x,0), reverse=True)
    best=patterns[0]

    # Trend alignment - Book rule p88: trade with trend
    if "BULLISH" in best and h4_bias!="BULLISH": return None
    if "BEARISH" in best and h4_bias!="BEARISH": return None
    # Inside bar trades with trend
    if best=="INSIDE_BAR":
        best = "BULLISH_INSIDE" if h4_bias=="BULLISH" else "BEARISH_INSIDE"

    bias="BUY" if "BULLISH" in best else "SELL"

    # Confluence check - need at least 1 confluence factor per book p98
    score,reasons,ema21,fib50,fib61,sh,sl = check_confluence(m5,best)
    if score==0 and "INSIDE" not in best: # Inside bar can trade without level if strong trend
        return None

    price=m5['close'].iloc[-1]
    atr=(m5['high']-m5['low']).rolling(14).mean().iloc[-1]
    if pd.isna(atr): atr=price*0.001

    if "R_75" in symbol: min_sl=price*0.003; mult=5.0
    elif any(x in symbol for x in ["R_","BOOM","CRASH"]): min_sl=price*0.002; mult=3.0
    elif "XAU" in symbol: min_sl=2.5; mult=1.8
    else: min_sl=atr*1.2; mult=1.2

    raw_sl = (m5['low'].iloc[-3:].min() - max(min_sl,atr*mult)) if bias=="BUY" else (m5['high'].iloc[-3:].max() + max(min_sl,atr*mult))
    raw_risk=abs(price-raw_sl)
    if raw_risk<=0: return None
    spread=fetch_spread(symbol); effective=raw_risk+spread
    if bias=="BUY": tp1,tp2=price+effective*1.5,price+effective*3.0
    else: tp1,tp2=price-effective*1.5,price-effective*3.0

    return {"symbol":symbol,"bias":bias,"pattern":best,"score":score,"reasons":reasons,"price":price,"sl":raw_sl,"tp1":tp1,"tp2":tp2,"risk_distance":effective,"ema21":ema21,"fib50":fib50,"fib61":fib61,"swing_high":sh,"swing_low":sl,"h4_bias":h4_bias,"h1_bias":h1_bias,"df":m5,"tp1_hit":False}

def generate_tradingview_chart(df,symbol,setup,filename="chart.png"):
    plot_df=df.iloc[-80:].copy()
    mc=mpf.make_marketcolors(up='#26a69a',down='#ef5350',edge='inherit',wick='inherit',volume='in')
    style=mpf.make_mpf_style(marketcolors=mc,gridstyle=":",gridcolor="#2a2e39",facecolor="#131722")
    disp=symbol.replace("R_75","V75").replace("R_100","V100")
    hlines=[setup['price'],setup['tp1'],setup['tp2'],setup['sl'],setup['ema21'],setup['fib50'],setup['fib61'],setup['swing_high'],setup['swing_low']]
    colors=['#2962ff','#00e676','#00c853','#ff1744','#ff9800','#9c27b0','#9c27b0','#787b86','#787b86']
    linestyles=['-','--','--','-','-',' :',':',':',':']
    fig,axes=mpf.plot(plot_df,type='candle',style=style,title=f"\n{disp} {setup['bias']} {setup['pattern']} Score:{setup['score']}/2",hlines=dict(hlines=hlines,colors=colors,linestyle=linestyles),returnfig=True,figratio=(16,9),figscale=1.3)
    ax=axes[0]; last=len(plot_df)-1
    for pr,label,col in [(setup['price'],f" ENTRY {format_price(symbol,setup['price'])}",'#2962ff'),(setup['sl'],f" SL {format_price(symbol,setup['sl'])}",'#ff1744'),(setup['tp1'],f" TP1 {format_price(symbol,setup['tp1'])}",'#00e676'),(setup['tp2'],f" TP2 {format_price(symbol,setup['tp2'])}",'#00c853')]:
        ax.text(last+2,pr,label,color=col,fontsize=9,fontweight='bold',va='center',bbox=dict(boxstyle="round,pad=0.3",facecolor='#131722',edgecolor=col,alpha=0.9))
    ax.text(last+2,setup['ema21'],f" 21EMA {format_price(symbol,setup['ema21'])}",color='#ff9800',fontsize=7)
    ax.text(last+2,setup['fib50'],f" Fib50",color='#9c27b0',fontsize=7)
    ax.text(last+2,setup['swing_high'],f" HIGH {format_price(symbol,setup['swing_high'])}",color='#787b86',fontsize=7)
    ax.text(last+2,setup['swing_low'],f" LOW {format_price(symbol,setup['swing_low'])}",color='#787b86',fontsize=7)
    fig.text(0.02,0.92,f"{setup['pattern']} | {','.join(setup['reasons'])} | H4:{setup['h4_bias']} H1:{setup['h1_bias']}",color='#26a69a' if setup['bias']=='BUY' else '#ef5350',fontsize=9,bbox=dict(facecolor='#1e222d',alpha=0.8))
    fig.savefig(filename,dpi=150,bbox_inches='tight',facecolor='#131722'); plt.close(fig); return filename

async def track_positions(app):
    while True:
        try:
            if is_paused(): await asyncio.sleep(60); continue
            for trade in list(active_trades):
                cur=fetch_current_price(trade['symbol']);
                if cur is None: continue
                sym=trade['symbol']; ep=format_price(sym,trade['price']); tp1p=format_price(sym,trade['tp1']); tp2p=format_price(sym,trade['tp2']); curr_p=format_price(sym,cur)
                hit_tp1=(cur>=trade['tp1'] if trade['bias']=="BUY" else cur<=trade['tp1']) and not trade['tp1_hit']
                hit_tp2=(cur>=trade['tp2'] if trade['bias']=="BUY" else cur<=trade['tp2'])
                hit_sl=(cur<=trade['sl'] if trade['bias']=="BUY" else cur>=trade['sl'])
                if hit_tp1: trade['tp1_hit']=True; daily_stats['tp1_hits']+=1; save_state(); await app.bot.send_message(chat_id=CHAT_ID,text=f"✅ TP1 HIT {sym} {trade['pattern']} {ep} -> {curr_p}")
                elif hit_tp2: daily_stats['wins']+=1; record_trade_result(sym,trade['bias'],1); active_trades.remove(trade); save_state(); await app.bot.send_message(chat_id=CHAT_ID,text=f"🎯🎯 TP2 HIT {sym} {trade['pattern']} {ep} -> {tp2p}")
                elif hit_sl:
                    if not trade['tp1_hit']: daily_stats['losses']+=1; daily_stats['losses_today']+=ACCOUNT_BALANCE*RISK_PER_TRADE_PCT; record_trade_result(sym,trade['bias'],0)
                    active_trades.remove(trade); save_state(); await app.bot.send_message(chat_id=CHAT_ID,text=f"🔴 SL HIT {sym} {trade['pattern']} {ep} SL {format_price(sym,trade['sl'])} Now {curr_p}")
            await asyncio.sleep(10)
        except: log.exception("track error"); await asyncio.sleep(10)

async def market_scanner(app):
    last_m5={}
    while True:
        try:
            if is_paused(): await asyncio.sleep(60); continue
            for symbol in get_active_symbols():
                if symbol in last_signal_time and time.time()-last_signal_time[symbol]<3600: continue
                if any(t['symbol']==symbol for t in active_trades): continue
                m5_df=fetch_data(symbol,'5m',5);
                if m5_df is None: continue
                cur=m5_df.index[-1]
                if last_m5.get(symbol)==cur: continue
                last_m5[symbol]=cur
                setup=evaluate_aplus_setup(symbol)
                if not setup: continue
                if not check_circuit_breaker(): continue
                chart=generate_tradingview_chart(setup['df'],symbol,setup)
                                ep=format_price(symbol,setup['price']); slp=format_price(symbol,setup['sl']); tp1p=format_price(symbol,setup['tp1']); tp2p=format_price(symbol,setup['tp2'])
                disp=symbol.replace("R_75","V75").replace("R_100","V100")
                cap=(f"🎯 {setup['bias']} {disp} {setup['pattern']}\n"
                     f"Confluence: {setup['score']}/2 {','.join(setup['reasons'])}\n"
                     f"Entry {ep}\nSL {slp}\nTP1 {tp1p} (1:1.5)\nTP2 {tp2p} (1:3)\n"
                     f"Size {format_lots(symbol,setup['risk_distance'])}\n"
                     f"H4 {setup['h4_bias']} H1 {setup['h1_bias']} EMA21 {format_price(symbol,setup['ema21'])}")
                try:
                    with open(chart,"rb") as photo: await app.bot.send_photo(chat_id=CHAT_ID,photo=photo,caption=cap)
                    active_trades.append({k:v for k,v in setup.items() if k!="df"}); last_signal_time[symbol]=time.time(); save_state()
                except: log.exception("send fail %s",symbol)
                finally:
                    if os.path.exists(chart): os.remove(chart)
                await asyncio.sleep(3)
            await asyncio.sleep(30)
        except: log.exception("scanner error"); await asyncio.sleep(10)

async def schedule_daily_report(app):
    sent=None
    while True:
        try:
            now=datetime.now(timezone.utc)
            if now.hour==20 and now.minute<=10 and sent!=now.date():
                total=daily_stats['wins']+daily_stats['losses']+daily_stats['tp1_hits']; rw=daily_stats['wins']+daily_stats['tp1_hits']; wr=rw/total*100 if total>0 else 0
                rep=f"📊 DAILY REPORT {now.date()} V14 BIBLE\nWR {wr:.1f}% Wins {rw} Losses {daily_stats['losses']} TP1 {daily_stats['tp1_hits']} TP2 {daily_stats['wins']}\nActive {len(active_trades)} Patterns: Pin/Engulf/Inside/Fakey/MorningStar"
                try: await app.bot.send_message(chat_id=CHAT_ID,text=rep); sent=now.date()
                except: pass
            await asyncio.sleep(60)
        except: await asyncio.sleep(60)

async def start_command(u,c): await u.message.reply_text(f"StarFx V14 BIBLE ACTIVE {', '.join(get_active_symbols())} RUNNING\nStrategies: PinBar, Engulfing, InsideBar, Fakey, MorningStar, Harami\nConfluence: Support/Resistance + 21EMA + Fib50/61.8 + Trend\n/signal /price /report")
async def signal_command(u,c):
    await u.message.reply_text("Scanning Bible patterns...")
    for sym in get_active_symbols():
        s=evaluate_aplus_setup(sym)
        if s:
            chart=generate_tradingview_chart(s['df'],sym,s); ep=format_price(sym,s['price'])
            try:
                with open(chart,"rb") as ph: await c.bot.send_photo(chat_id=u.effective_chat.id,photo=ph,caption=f"{s['bias']} {sym} {s['pattern']} {ep} Score {s['score']}")
            finally:
                if os.path.exists(chart): os.remove(chart)
async def price_command(u,c):
    txt="💰 Live:\n"
    for sym in get_active_symbols():
        p=fetch_current_price(sym)
        if p: txt+=f"{sym}: {format_price(sym,p)}\n"
    await u.message.reply_text(txt)
async def report_command(u,c): total=daily_stats['wins']+daily_stats['losses']+daily_stats['tp1_hits']; rw=daily_stats['wins']+daily_stats['tp1_hits']; wr=rw/total*100 if total>0 else 0; await u.message.reply_text(f"📊 REPORT V14 BIBLE {datetime.now(timezone.utc).date()}\nWR {wr:.1f}% Wins {rw} Losses {daily_stats['losses']} TP1 {daily_stats['tp1_hits']} TP2 {daily_stats['wins']}\nActive {len(active_trades)}")

async def main():
    init_db(); load_state()
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",start_command)); app.add_handler(CommandHandler("signal",signal_command)); app.add_handler(CommandHandler("scan",signal_command)); app.add_handler(CommandHandler("price",price_command)); app.add_handler(CommandHandler("report",report_command))
    asyncio.create_task(market_scanner(app)); asyncio.create_task(track_positions(app)); asyncio.create_task(schedule_daily_report(app))
    log.info("StarFx V14 Bible online")
    async with app: await app.initialize(); await app.start(); await app.updater.start_polling(); await asyncio.Event().wait()

if __name__=="__main__": asyncio.run(main())
