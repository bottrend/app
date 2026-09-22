import os, time, threading
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
import requests

app=Flask(__name__)
lock=threading.Lock()

START_TOTAL=2000.0
START_USDT=1000.0
TRADE_PCT=0.10
MIN_TRADE_USD=2.0
STEP=0.005
FEE_RATE=0.001

state={"price":None,"anchor":None,"btc":0.0,"usdt":START_USDT,"initial_btc":0.0,
       "trades":0,"buys":0,"sells":0,"fees":0.0,"last_trade":None,"started":None,"error":None}

def btc_price():
    r=requests.get("https://www.okx.com/api/v5/market/ticker",params={"instId":"BTC-USDT"},timeout=8)
    r.raise_for_status()
    return float(r.json()["data"][0]["last"])

def init(px):
    state["price"]=px; state["anchor"]=px
    state["initial_btc"]=1000.0/px; state["btc"]=state["initial_btc"]
    state["started"]=datetime.now(timezone.utc).isoformat()

def sell(px):
    trade_usd=(state["btc"]*px)*TRADE_PCT
    if trade_usd < MIN_TRADE_USD: return
    qty=trade_usd/px
    fee=trade_usd*FEE_RATE
    state["btc"]-=qty
    state["usdt"]+=trade_usd-fee
    state["fees"]+=fee; state["trades"]+=1; state["sells"]+=1
    state["anchor"]=px
    state["last_trade"]={"side":"SELL","price":px,"usd":trade_usd,"fee":fee,"time":datetime.now(timezone.utc).isoformat()}

def buy(px):
    # Use 10% of current USDT while charging the fee inside that budget.
    trade_usd=state["usdt"]*TRADE_PCT
    if trade_usd < MIN_TRADE_USD: return
    fee=trade_usd*FEE_RATE
    net_btc_usd=trade_usd-fee
    state["usdt"]-=trade_usd
    state["btc"]+=net_btc_usd/px
    state["fees"]+=fee; state["trades"]+=1; state["buys"]+=1
    state["anchor"]=px
    state["last_trade"]={"side":"BUY","price":px,"usd":trade_usd,"fee":fee,"time":datetime.now(timezone.utc).isoformat()}

def worker():
    while True:
        try:
            px=btc_price()
            with lock:
                state["error"]=None
                if state["anchor"] is None: init(px)
                state["price"]=px
                # one dynamic 10% balance order per crossed 0.5% level; minimum order $2
                while px >= state["anchor"]*(1+STEP):
                    level=state["anchor"]*(1+STEP)
                    before=state["trades"]; sell(level)
                    if state["trades"]==before: break
                while px <= state["anchor"]*(1-STEP):
                    level=state["anchor"]*(1-STEP)
                    before=state["trades"]; buy(level)
                    if state["trades"]==before: break
        except Exception as e:
            with lock: state["error"]=str(e)[:160]
        time.sleep(5)

def snapshot():
    with lock:
        s=dict(state)
        px=s["price"]
        if px:
            initial_btc_value=s["initial_btc"]*px
            btc_value=s["btc"]*px
            total=btc_value+s["usdt"]
            s.update({
              "btc_value":btc_value,"total":total,"pnl":total-START_TOTAL,
              "pnl_pct":(total/START_TOTAL-1)*100,
              "btc_change":s["btc"]-s["initial_btc"],
              "btc_change_value":btc_value-initial_btc_value,
              "usdt_change":s["usdt"]-START_USDT,
              "upper":s["anchor"]*(1+STEP),"lower":s["anchor"]*(1-STEP)
            })
        return s

@app.get("/api")
def api(): return jsonify(snapshot())

@app.get("/health")
def health(): return {"ok":True}

HTML="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC 0.5% Grid Simulator</title><style>
body{margin:0;background:#080b12;color:#eaf0ff;font-family:system-ui,Arial}.wrap{max-width:900px;margin:auto;padding:20px}
h1{font-size:22px}.muted{color:#8d98ad}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.card{background:#111724;border:1px solid #222d42;border-radius:12px;padding:14px}.v{font-size:22px;font-weight:700;margin-top:6px}
.pos{color:#4ade80}.neg{color:#fb7185}.buy{color:#4ade80}.sell{color:#fb7185}
small{color:#8d98ad}</style></head><body><div class="wrap">
<h1>₿ BTC 0.5% GRID SIMULATOR</h1><div class="muted">Paper simulation · $1,000 BTC + $1,000 USDT · 10% of current side/order · min $2 · fee 0.1% · update 5s</div>
<div id="x" style="margin-top:14px">Loading...</div></div>
<script>
const n=(x,d=2)=>x==null?'N/A':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
async function go(){try{let s=await (await fetch('/api',{cache:'no-store'})).json(); let p=s.pnl||0, cls=p>=0?'pos':'neg';
document.getElementById('x').innerHTML=`
<div class="grid">
<div class="card">BTC PRICE<div class="v">$${n(s.price,2)}</div></div>
<div class="card">ANCHOR<div class="v">$${n(s.anchor,2)}</div><small>Buy ≤ $${n(s.lower,2)} · Sell ≥ $${n(s.upper,2)}</small></div>
<div class="card">TOTAL VALUE<div class="v ${cls}">$${n(s.total,4)}</div><small class="${cls}">${p>=0?'+':''}$${n(p,4)} (${p>=0?'+':''}${n(s.pnl_pct,3)}%)</small></div>
<div class="card">TOTAL FEES<div class="v">$${n(s.fees,4)}</div></div>
<div class="card">BTC<div class="v">${n(s.btc,8)}</div><small>Δ ${n(s.btc_change,8)} BTC · $${n(s.btc_change_value,4)}</small></div>
<div class="card">USDT<div class="v">$${n(s.usdt,4)}</div><small>Δ $${n(s.usdt_change,4)}</small></div>
<div class="card">ORDERS<div class="v">${s.trades}</div><small><span class="buy">BUY ${s.buys}</span> · <span class="sell">SELL ${s.sells}</span></small></div>
<div class="card">LAST TRADE<div class="v ${s.last_trade?.side==='BUY'?'buy':'sell'}">${s.last_trade?s.last_trade.side:'N/A'}</div><small>${s.last_trade?'$'+n(s.last_trade.price,2)+' · fee $'+n(s.last_trade.fee,4):'Waiting for ±0.5%'}</small></div>
</div><p class="muted">Status: ${s.error?'Price feed error: '+s.error:'LIVE · server checks BTC every 5 seconds'}</p>`; }catch(e){}}
go();setInterval(go,5000);
</script></body></html>"""
@app.get("/")
def home(): return render_template_string(HTML)

if __name__=="__main__":
    threading.Thread(target=worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
