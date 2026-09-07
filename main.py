from flask import Flask
import threading, time, os, yfinance as yf, json
import pandas as pd, numpy as np, requests, pytz
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches

app = Flask(__name__)
@app.route('/')
def home(): return "STARFX V7.3 WEEKLY GRAPH LIVE"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000)).start()

BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHANNEL_ID = "-1004365660319"
EAT = pytz.timezone('Africa/Nairobi')
SYMBOLS = {"GOLD":"GC=F","GBPUSD":"GBPUSD=X","BTCUSD":"BTC-USD"}
TRADES_FILE="/tmp/active_trades.json"; CLOSED_FILE="/tmp/closed_trades.json"
LAST_REPORT_FILE="/tmp/last_report.txt"; LAST_WEEK_FILE="/tmp/last_week.txt"

active_trades={}; closed_trades=[]
if os.path.exists(TRADES_FILE):
    try:
        with open(TRADES_FILE,'r') as f: active_trades=json.load(f)
    except: active_trades={}
if os.path.exists(CLOSED_FILE):
    try:
        with open(CLOSED_FILE,'r') as f: closed_trades=json.load(f)
    except: closed_trades=[]

def save_trades():
    try:
        with open(TRADES_FILE,'w') as f: json.dump(active_trades,f)
        with open(CLOSED_FILE,'w') as f: json.dump(closed_trades,f)
    except: pass

def send_photo(path, caption):
    try:
        url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        with open(path,'rb') as f:
            r=requests.post(url, files={'photo':f}, data={"chat_id":CHANNEL_ID,"caption":caption,"parse_mode":"Markdown"}, timeout=30).json()
            if r.get('ok'): return r['result']['message_id']
    except Exception as e: print(e)
    return None

def edit_caption(mid, new_cap):
    try:
        url=f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption"
        requests.post(url, json={"chat_id":CHANNEL_ID,"message_id":mid,"caption":new_cap,"parse_mode":"Markdown"}, timeout=15)
    except: pass

def send_msg(m):
    try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHANNEL_ID,"text":m,"parse_mode":"Markdown"}, timeout=15)
    except: pass

def get_data(sym, period, interval):
    df=yf.download(sym, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty: return df
    if isinstance(df.columns, pd.MultiIndex): df.columns=df.columns.get_level_values(0)
    return df.dropna()

def is_choppy(df):
    df['tr']=np.maximum(df['High']-df['Low'], np.maximum(abs(df['High']-df['Close'].shift(1)), abs(df['Low']-df['Close'].shift(1))))
    atr=df['tr'].rolling(14).mean().iloc[-1]
    if atr/df['Close'].iloc[-1]<0.0008: return True
    overlaps=0
    for i in range(-7,-1):
        overlap=max(0, min(df.iloc[i]['High'], df.iloc[i+1]['High']) - max(df.iloc[i]['Low'], df.iloc[i+1]['Low']))
        if df.iloc[i]['High']-df.iloc[i]['Low']>0 and overlap/(df.iloc[i]['High']-df.iloc[i]['Low'])>0.6: overlaps+=1
    return overlaps>=4

def find_trendline(df, mode):
    df=df.reset_index(drop=True); pts=[]
    for i in range(5,len(df)-5):
        if mode=="support":
            if df.loc[i,'Low']==df['Low'][i-5:i+5].min(): pts.append((i,float(df.loc[i,'Low'])))
        else:
            if df.loc[i,'High']==df['High'][i-5:i+5].max(): pts.append((i,float(df.loc[i,'High'])))
    if len(pts)<2: return None
    best=None; best_t=0
    for a in range(len(pts)):
        for b in range(a+1,len(pts)):
            x1,y1=pts[a]; x2,y2=pts[b]
            if x2==x1: continue
            slope=(y2-y1)/(x2-x1); touches=0
            for x,y in pts:
                if abs(y-(y1+slope*(x-x1)))/y <0.0015: touches+=1
            if touches>=2 and touches>best_t: best_t=touches; best=(x1,y1,x2,y2,slope,touches)
    return best

def detect_fvg(df):
    fvg_b=None; fvg_s=None
    for i in range(len(df)-2):
        if df.iloc[i+2]['Low']>df.iloc[i]['High']: fvg_b=(float(df.iloc[i]['High']), float(df.iloc[i+2]['Low']))
        if df.iloc[i]['Low']>df.iloc[i+2]['High']: fvg_s=(float(df.iloc[i+2]['High']), float(df.iloc[i]['Low']))
    return fvg_b,fvg_s

def detect_patterns(m5):
    c=m5.iloc[-1]; p=m5.iloc[-2]; body=abs(c['Close']-c['Open'])
    bull_eng=c['Close']>c['Open'] and p['Close']<p['Open'] and c['Close']>p['Open'] and c['Open']<p['Close']
    bear_eng=c['Close']<c['Open'] and p['Close']>p['Open'] and c['Close']<p['Open'] and c['Open']>p['Close']
    low_wick=min(c['Open'],c['Close'])-c['Low']; up_wick=c['High']-max(c['Open'],c['Close'])
    hammer=low_wick>body*2 and up_wick<body*0.3; shooting=up_wick>body*2 and low_wick<body*0.3
    highs=m5['High'].tail(15); lows=m5['Low'].tail(15)
    triple_top=len(highs[highs>=highs.max()*0.999])>=3; triple_bot=len(lows[lows<=lows.min()*1.001])>=3
    conf=[];
    if bull_eng: conf.append("Bull Engulfing")
    if bear_eng: conf.append("Bear Engulfing")
    if hammer: conf.append("Hammer")
    if shooting: conf.append("Shooting Star")
    if triple_top: conf.append("Triple Top")
    if triple_bot: conf.append("Triple Bottom")
    return conf, bull_eng or hammer or triple_bot, bear_eng or shooting or triple_top

def draw_chart(df_m15, name, hh, ll, ob_zone, fvg, entry, sl, tp1, tp2, sig_type, sup_line, res_line, score, status="LIVE"):
    plt.figure(figsize=(13,7)); ax=plt.gca()
    df=df_m15.tail(90).copy().reset_index(drop=True)
    for i in range(len(df)):
        col='#26a69a' if df.loc[i,'Close']>=df.loc[i,'Open'] else '#ef5350'
        plt.plot([i,i],[df.loc[i,'Low'],df.loc[i,'High']], color=col, lw=1)
        btm=min(df.loc[i,'Open'],df.loc[i,'Close']); h=abs(df.loc[i,'Close']-df.loc[i,'Open'])
        rect=patches.Rectangle((i-0.3,btm),0.6,max(h,df.loc[i,'High']*0.0001), facecolor=col, edgecolor=col)
        ax.add_patch(rect)
    plt.axhline(hh, color='orange', ls='--', lw=1, alpha=0.7, label=f'H4 HH')
    plt.axhline(ll, color='orange', ls='--', lw=1, alpha=0.7, label=f'H4 LL')
    if ob_zone: plt.axhspan(ob_zone[0],ob_zone[1], color='yellow' if 'BUY' in sig_type else '#ab47bc', alpha=0.25, label=f'OB')
    if fvg: plt.axhspan(fvg[0],fvg[1], color='#00e676', alpha=0.15, label=f'FVG')
    x_vals=np.arange(len(df)+20)
    if sup_line:
        x1,y1,x2,y2,slope,t=sup_line
        plt.plot(x_vals, y1+slope*(x_vals-x1), color='#00bcd4', lw=2, label=f'Support {t} touches')
        plt.scatter([x1,x2],[y1,y2], color='#00bcd4', s=70, zorder=5)
    if res_line:
        x1,y1,x2,y2,slope,t=res_line
        plt.plot(x_vals, y1+slope*(x_vals-x1), color='#ff9800', lw=2, label=f'Resist {t} touches')
        plt.scatter([x1,x2],[y1,y2], color='#ff9800', s=70, zorder=5)
    col_entry='#2962ff' if status=="LIVE" else '#00e676' if "TP" in status else '#ef5350'
    plt.axhline(entry, color=col_entry, lw=3, label=f'ENTRY {entry:.2f} {status}')
    plt.axhline(sl, color='#ef5350', lw=2, label=f'SL'); plt.axhline(tp1, color='#26a69a', ls=':', lw=2, label=f'TP1'); plt.axhline(tp2, color='#26a69a', ls='--', lw=2, label=f'TP2')
    plt.title(f'{name} | {sig_type} | {status} | {score}/100', color='white', fontsize=13, fontweight='bold')
    ax.set_facecolor('#131722'); plt.gcf().set_facecolor('#131722')
    ax.tick_params(colors='white'); plt.grid(True, alpha=0.15)
    plt.legend(loc='upper left', fontsize=7, facecolor='#1e222d', edgecolor='gray', labelcolor='white')
    plt.tight_layout()
    path=f"/tmp/{name}_{int(time.time())}.png"
    plt.savefig(path, dpi=230, facecolor='#131722'); plt.close()
    return path

def draw_weekly_graph():
    # Equity curve + Win rate
    if not closed_trades:
        return None
    # Last 7 days
    last7=[]
    for i in range(7):
        d = (datetime.now(EAT) - timedelta(days=i)).strftime('%Y-%m-%d')
        day_trades = [t for t in closed_trades if t['date']==d]
        day_r = sum([t['r'] for t in day_trades]) if day_trades else 0
        last7.append((d, day_r, len(day_trades)))
    last7 = list(reversed(last7))

    dates = [x[0][5:] for x in last7] # MM-DD
    r_vals = [x[1] for x in last7]
    cum_r = np.cumsum(r_vals)

    fig, (ax1, ax2) = plt.subplots(1,2, figsize=(14,6))
    fig.set_facecolor('#131722')

    # Equity curve
    ax1.set_facecolor('#1e222d')
    ax1.plot(dates, cum_r, color='#00e676', linewidth=3, marker='o', markersize=8, label='Cumulative R')
    ax1.bar(dates, r_vals, color=['#26a69a' if r>=0 else '#ef5350' for r in r_vals], alpha=0.6)
    ax1.set_title('Weekly Equity Curve (R)', color='white', fontsize=12, fontweight='bold')
    ax1.tick_params(colors='white'); ax1.grid(True, alpha=0.2)
    ax1.set_ylabel('R', color='white')
    ax1.legend(facecolor='#1e222d', edgecolor='gray', labelcolor='white')

    # Win rate pie
    wins = len([t for t in closed_trades[-30:] if t['r']>0])
    losses = len([t for t in closed_trades[-30:] if t['r']<0])
    total = wins+losses if wins+losses>0 else 1
    ax2.set_facecolor('#1e222d')
    ax2.pie([wins, losses], labels=[f'Wins {wins}', f'Losses {losses}'], colors=['#26a69a','#ef5350'], autopct='%1.1f%%', textprops={'color':'white'}, wedgeprops={'edgecolor':'#131722'})
    ax2.set_title(f'Win Rate Last 30 Trades - {wins/total*100:.1f}%', color='white', fontsize=12, fontweight='bold')

    plt.tight_layout()
    path = f"/tmp/weekly_{int(time.time())}.png"
    plt.savefig(path, dpi=220, facecolor='#131722')
    plt.close()
    return path

def check_active_trades():
    global active_trades, closed_trades
    if not active_trades: return
    for tid, trade in list(active_trades.items()):
        try:
            df=get_data(trade['yf_sym'], "2d", "5m")
            if df.empty: continue
            curr_high=float(df['High'].iloc[-1]); curr_low=float(df['Low'].iloc[-1]); curr_price=float(df['Close'].iloc[-1])
            name=trade['name']; entry=trade['entry']; sl=trade['sl']; tp1=trade['tp1']; tp2=trade['tp2']; msg_id=trade['message_id']
            is_buy="BUY" in trade['type']; result=None
            if is_buy:
                if curr_low<=sl: result="SL"
                elif curr_high>=tp2: result="TP2"
                elif curr_high>=tp1 and not trade.get('tp1_hit'): result="TP1"
            else:
                if curr_high>=sl: result="SL"
                elif curr_low<=tp2: result="TP2"
                elif curr_low<=tp1 and not trade.get('tp1_hit'): result="TP1"
            if result:
                now_str=datetime.now(EAT).strftime('%H:%M EAT'); date_str=datetime.now(EAT).strftime('%Y-%m-%d')
                if result=="SL":
                    edit_caption(msg_id, f"*❌ SL HIT | {name} | {trade['type']}*\nEntry {entry:.2f} -> SL {sl:.2f} -1R Closed {curr_price:.2f}")
                    send_msg(f"*❌ SL HIT | {name}* -1R\n{trade['type']} Entry {entry:.2f}")
                    closed_trades.append({"date":date_str,"name":name,"type":trade['type'],"result":result,"r":-1,"time":now_str})
                elif result=="TP1":
                    edit_caption(msg_id, f"*✅ TP1 HIT | {name} | {trade['type']}* +1.5R Running to TP2... {curr_price:.2f}")
                    send_msg(f"*✅ TP1 HIT | {name} +1.5R* 🎯 Running to TP2")
                    trade['tp1_hit']=True; save_trades(); continue
                elif result=="TP2":
                    edit_caption(msg_id, f"*🏆 TP2 FULL WIN | {name} | {trade['type']}* +3R {curr_price:.2f}")
                    send_msg(f"*🏆 TP2 WIN | {name} +3R* 🏆")
                    closed_trades.append({"date":date_str,"name":name,"type":trade['type'],"result":result,"r":3.0,"time":now_str})
                del active_trades[tid]; save_trades()
        except Exception as e: print(f"Check err {e}")

def check_daily_report():
    try:
        now=datetime.now(EAT)
        if now.hour!=23: return
        today=now.strftime('%Y-%m-%d')
        if os.path.exists(LAST_REPORT_FILE):
            with open(LAST_REPORT_FILE,'r') as f:
                if f.read().strip()==today: return
        todays=[t for t in closed_trades if t['date']==today]
        if not todays:
            send_msg(f"*📊 DAILY {today}*\nNo A+ closed today. Active: {len(active_trades)}")
        else:
            wins=len([t for t in todays if t['r']>0]); losses=len([t for t in todays if t['r']<0])
            total_r=sum([t['r'] for t in todays]); winrate=wins/len(todays)*100
            breakdown=""
            for sym in SYMBOLS.keys():
                sym_trades=[t for t in todays if t['name']==sym]
                if sym_trades: breakdown+=f"{sym}: {len(sym_trades)} trades {sum([t['r'] for t in sym_trades]):+.1f}R\n"
            send_msg(f"*📊 DAILY {today}*\nW:{wins} L:{losses} WR:{winrate:.1f}% Total:{total_r:+.1f}R\n{breakdown}Active:{len(active_trades)}")
        with open(LAST_REPORT_FILE,'w') as f: f.write(today)
    except Exception as e: print(f"Daily err {e}")

def check_weekly_report():
    try:
        now=datetime.now(EAT)
        if now.weekday()!=6: return # Sunday = 6
        if now.hour!=23: return
        today=now.strftime('%Y-%m-%d')
        if os.path.exists(LAST_WEEK_FILE):
            with open(LAST_WEEK_FILE,'r') as f:
                if f.read().strip()==today: return

        # Last 7 days
        week_trades=[]
        for i in range(7):
            d=(now - timedelta(days=i)).strftime('%Y-%m-%d')
            week_trades.extend([t for t in closed_trades if t['date']==d])

        if not week_trades:
            send_msg(f"*📈 WEEKLY REPORT {today}*\nNo trades this week. Active: {len(active_trades)}")
        else:
            wins=len([t for t in week_trades if t['r']>0]); losses=len([t for t in week_trades if t['r']<0])
            total_r=sum([t['r'] for t in week_trades]); winrate=wins/len(week_trades)*100 if week_trades else 0

            graph_path = draw_weekly_graph()

            cap=f"""*📈 WEEKLY REPORT | {today} | STARFX V7.3*

*Week Results (Mon-Sun):*
Trades: {len(week_trades)} | Wins: {wins} | Losses: {losses}
Win Rate: {winrate:.1f}%
Total: {total_r:+.1f}R

*Breakdown:*
GOLD: {sum([t['r'] for t in week_trades if t['name']=='GOLD']):+.1f}R
GBPUSD: {sum([t['r'] for t in week_trades if t['name']=='GBPUSD']):+.1f}R
BTCUSD: {sum([t['r'] for t in week_trades if t['name']=='BTCUSD']):+.1f}R

*Equity Curve:* See graph
*All Time:* {len(closed_trades)} closed | {sum([t['r'] for t in closed_trades]):+.1f}R total

Next week we go again 🎯"""

            if graph_path:
                send_photo(graph_path, cap)
            else:
                send_msg(cap)

        with open(LAST_WEEK_FILE,'w') as f: f.write(today)
    except Exception as e: print(f"Weekly err {e}")

def analyze(name, yf_sym):
    try:
        h4_raw=get_data(yf_sym, "60d", "60m")
        if h4_raw.empty: return
        h4=h4_raw.resample('4H').agg({'Open':'first','High':'max','Low':'min','Close':'last'}).dropna()
        hh=float(h4['High'].tail(50).max()); ll=float(h4['Low'].tail(50).min())
        is_range=(hh-ll)/ll <0.008
        h1=get_data(yf_sym, "20d", "60m"); m15=get_data(yf_sym, "10d", "15m"); m5=get_data(yf_sym, "3d", "5m")
        if h1.empty or m15.empty or m5.empty: return
        if is_choppy(m15): return
        if not (11 <= datetime.now(EAT).hour <= 23): return
        price=float(m5['Close'].iloc[-1])
        h1_high=h1['High'].tail(20).max(); h1_low=h1['Low'].tail(20).min()
        bos_bull=price>h1_high; bos_bear=price<h1_low
        sweep_low=m5['Low'].iloc[-1]<ll and m5['Close'].iloc[-1]>ll
        sweep_high=m5['High'].iloc[-1]>hh and m5['Close'].iloc[-1]<hh
        avg_body=abs(m15['Close']-m15['Open']).rolling(20).mean().iloc[-1]
        big_bull=m15.iloc[-1]['Close']>m15.iloc[-1]['Open'] and abs(m15.iloc[-1]['Close']-m15.iloc[-1]['Open'])>avg_body*1.8
        big_bear=m15.iloc[-1]['Close']<m15.iloc[-1]['Open'] and abs(m15.iloc[-1]['Close']-m15.iloc[-1]['Open'])>avg_body*1.8
        ob_zone=(float(m15.iloc[-2]['Low']), float(m15.iloc[-2]['High'])) if (big_bull or big_bear) else None
        fvg_b,fvg_s=detect_fvg(m15.tail(10))
        sup_line=find_trendline(m15, "support"); res_line=find_trendline(m15, "resistance")
        trend_touch_bull=False; trend_touch_bear=False
        if sup_line:
            x1,y1,x2,y2,slope,t=sup_line
            if abs(price-(y1+slope*(len(m15)-1 - x1)))/price<0.0025: trend_touch_bull=True
        if res_line:
            x1,y1,x2,y2,slope,t=res_line
            if abs(price-(y1+slope*(len(m15)-1 - x1)))/price<0.0025: trend_touch_bear=True
        conf_list,bull_conf,bear_conf=detect_patterns(m5)
        score=0
        if not is_range: score+=25
        if sweep_low or sweep_high: score+=20
        if ob_zone: score+=15
        if fvg_b or fvg_s: score+=5
        if bos_bull or bos_bear: score+=15
        if trend_touch_bull or trend_touch_bear: score+=10
        if bull_conf or bear_conf: score+=10
        if any(t['name']==name for t in active_trades.values()): return
        if score<85: return
        sl=tp1=tp2=0; fvg_draw=None; sig_type=""
        if (sweep_low or big_bull or trend_touch_bull) and bull_conf:
            base_low=ob_zone[0] if ob_zone else m15['Low'].tail(5).min()
            sl=base_low - price*0.0015
            if sl>=price: sl=price*0.996
            tp1=price + (price-sl)*1.5; tp2=price + (price-sl)*3
            fvg_draw=fvg_b; sig_type="SNIPER BUY A+"
        elif (sweep_high or big_bear or trend_touch_bear) and bear_conf:
            base_high=ob_zone[1] if ob_zone else m15['High'].tail(5).max()
            sl=base_high + price*0.0015
            if sl<=price: sl=price*1.004
            tp1=price - (sl-price)*1.5; tp2=price - (sl-price)*3
            fvg_draw=fvg_s; sig_type="SNIPER SELL A+"
        else: return
        chart=draw_chart(m15, name, hh, ll, ob_zone, fvg_draw, price, sl, tp1, tp2, sig_type, sup_line, res_line, score, "LIVE")
        cap=f"*🎯 {sig_type} | {name} | {score}/100 A+*\nENTRY {price:.2f} SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f}\nConfirm: {', '.join(conf_list)} | LIVE TRACKING"
        msg_id=send_photo(chart, cap)
        if msg_id:
            tid=f"{name}_{int(time.time())}"
            active_trades[tid]={"name":name,"yf_sym":yf_sym,"type":sig_type,"entry":price,"sl":float(sl),"tp1":float(tp1),"tp2":float(tp2),"message_id":msg_id,"time":datetime.now(EAT).isoformat()}
            save_trades()
    except Exception as e: print(f"Analyze err {e}")

send_msg("*STARFX V7.3 WEEKLY GRAPH ONLINE* 🎯\nA+ 85+ | Auto SL/TP | Daily 23:00 | Weekly Graph Sunday 23:00 EAT\nGOLD, GBPUSD, BTCUSD")

while True:
    check_active_trades()
    check_daily_report()
    check_weekly_report()
    for n,s in SYMBOLS.items():
        analyze(n,s)
    time.sleep(900)
