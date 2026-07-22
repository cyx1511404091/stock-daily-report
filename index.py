# -*- coding: utf-8 -*-
"""
腾讯云函数 - 模拟投资每日财报推送
每天8:00自动触发
"""
import json
import os
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, date, timedelta
import urllib.request
import urllib.error


# ===== 配置 =====
INITIAL_CAPITAL = 100000.00
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
SMTP_USER = "1511404091@qq.com"
SMTP_PASSWORD = "iwzwcypzvwcnjgeg"
TO_EMAIL = "1511404091@qq.com"

STOP_LOSS_PCT = -0.05
TAKE_PROFIT_PCT = 0.10
MAX_HOLD_DAYS = 5

WATCHLIST = {
    "603986": {"name": "兆易创新", "sector": "存储芯片"},
    "300394": {"name": "天孚通信", "sector": "光器件"},
    "601138": {"name": "工业富联", "sector": "AI服务器"},
    "688012": {"name": "中微公司", "sector": "半导体设备"},
    "002371": {"name": "北方华创", "sector": "半导体设备"},
    "688072": {"name": "拓荆科技", "sector": "半导体设备"},
    "300308": {"name": "中际旭创", "sector": "光模块"},
    "300502": {"name": "新易盛", "sector": "光模块"},
    "688525": {"name": "佰维存储", "sector": "存储芯片"},
    "688256": {"name": "寒���纪", "sector": "AI芯片"},
    "300346": {"name": "南大光电", "sector": "光刻胶"},
    "688268": {"name": "华特气体", "sector": "电子特气"},
    "002156": {"name": "通富微电", "sector": "先进封装"},
    "600744": {"name": "华银电力", "sector": "火��"},
    "001258": {"name": "立新能源", "sector": "新能源发电"},
    "600900": {"name": "长江电力", "sector": "水电"},
    "688017": {"name": "绿的谐波", "sector": "减速器"},
    "600030": {"name": "中信证券", "sector": "券商"},
    "300059": {"name": "东方财富", "sector": "互联网券商"},
    "603259": {"name": "药明康德", "sector": "CXO"},
}

# GitHub Actions 环境：存在仓库目录；云函数环境：存在 /tmp
ACCOUNT_KEY = os.environ.get("ACCOUNT_FILE", 
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock-account.json"))
# 如果当前目录不可写（如云函数），回退到 /tmp
if not os.access(os.path.dirname(ACCOUNT_KEY), os.W_OK):
    ACCOUNT_KEY = "/tmp/stock-account.json"


# ===== 持久化 =====
def load_account():
    try:
        if os.path.exists(ACCOUNT_KEY):
            with open(ACCOUNT_KEY, "r") as f:
                return json.load(f)
    except:
        pass
    return {
        "cash": INITIAL_CAPITAL,
        "positions": {},
        "trades": [],
        "trade_count": 0,
    }


def save_account(acct):
    try:
        with open(ACCOUNT_KEY, "w") as f:
            json.dump(acct, f, ensure_ascii=False)
    except:
        pass


# ===== 行情获取 =====
def fetch_prices():
    results = {}
    codes = list(WATCHLIST.keys())
    sid_list = []
    for code in codes:
        market = "sh" if code.startswith(("6", "9")) else "sz"
        sid_list.append(market + code)

    for i in range(0, len(sid_list), 20):
        batch = sid_list[i:i + 20]
        url = "https://hq.sinajs.cn/list=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("gbk")
            for line in raw.strip().split("\n"):
                if "=" not in line:
                    continue
                try:
                    sid = line.split("=")[0].split("_")[-1]
                    data = line.split('"')[1].split(",")
                    code = sid[2:]
                    price = float(data[3])
                    prev_close = float(data[2])
                    chg_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
                    results[code] = {
                        "name": WATCHLIST.get(code, {}).get("name", code),
                        "sector": WATCHLIST.get(code, {}).get("sector", ""),
                        "price": round(price, 2),
                        "prev_close": prev_close,
                        "change_pct": round(chg_pct, 2),
                    }
                except:
                    continue
        except Exception as e:
            print("[Fetch] Error: " + str(e))
    return results


# ===== 交易逻辑 =====
def check_positions(acct, prices):
    sells = []
    today = str(date.today())
    for code, pos in list(acct["positions"].items()):
        p = prices.get(code, {})
        price = p.get("price", 0)
        if not price:
            continue
        cost = pos["avg_cost"]
        pnl_pct = (price - cost) / cost
        buy_date = pos.get("buy_date", "")
        hold_days = 0
        if buy_date:
            try:
                hold_days = (date.today() - date.fromisoformat(buy_date)).days
            except:
                pass

        reason = None
        if pnl_pct <= STOP_LOSS_PCT:
            reason = "止损: 浮亏{:.1f}%".format(pnl_pct * 100)
        elif hold_days >= MAX_HOLD_DAYS:
            reason = "超期卖出: 已持有{}天".format(hold_days)
        elif pnl_pct >= TAKE_PROFIT_PCT:
            reason = "止盈: 浮盈{:.1f}%".format(pnl_pct * 100)

        if reason:
            sells.append({"code": code, "shares": pos["shares"], "price": price, "reason": reason})
    return sells


def execute_sell(acct, code, price, shares, reason):
    pos = acct["positions"][code]
    amount = price * shares
    fee = max(amount * 0.00025, 5)
    stamp = amount * 0.001
    net = amount - fee - stamp
    acct["cash"] += net
    pos["shares"] -= shares
    if pos["shares"] <= 0:
        del acct["positions"][code]
    acct["trade_count"] += 1
    acct["trades"].append({
        "type": "SELL", "code": code, "name": pos.get("name", ""),
        "price": price, "shares": shares, "amount": amount,
        "reason": reason,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def get_buy_candidates(acct, prices):
    candidates = []
    available = acct["cash"]
    if available < 5000:
        return candidates
    priority = [
        ("603986", "存储芯片"), ("300394", "光器件"), ("688012", "半导体设备"),
        ("601138", "AI服务器"), ("300308", "光模块"), ("300502", "光模块"),
    ]
    for code, sector in priority:
        if code in acct["positions"]:
            continue
        p = prices.get(code, {})
        price = p.get("price", 0)
        if price <= 0:
            continue
        max_amount = available * 0.4
        shares = int(max_amount / price / 100) * 100
        if shares >= 100:
            candidates.append({
                "code": code, "name": p.get("name", ""), "sector": sector,
                "price": price, "shares": shares, "amount": price * shares,
                "reason": "主线赛道({})+回调低吸".format(sector),
            })
    return candidates


# ===== 邮件 =====
def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print("[Email] " + str(e))
        return False


def generate_html(acct, prices):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    pv = 0
    for code, pos in acct["positions"].items():
        pv += pos["shares"] * prices.get(code, {}).get("price", 0)

    tv = acct["cash"] + pv
    pnl = tv - INITIAL_CAPITAL
    pnl_pct = pnl / INITIAL_CAPITAL * 100
    pc = "#27ae60" if pnl >= 0 else "#e74c3c"
    ps = "+" if pnl >= 0 else ""

    pos_rows = ""
    for code, pos in acct["positions"].items():
        p = prices.get(code, {})
        price = p.get("price", 0)
        mv = pos["shares"] * price
        cost = pos["shares"] * pos["avg_cost"]
        ppnl = mv - cost
        ppp = ppnl / cost * 100 if cost else 0
        pc2 = "#27ae60" if ppnl >= 0 else "#e74c3c"
        ps2 = "+" if ppnl >= 0 else ""
        bd = pos.get("buy_date", "")
        hd = 0
        if bd:
            try:
                hd = (date.today() - date.fromisoformat(bd)).days
            except:
                pass
        pos_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>¥{:.2f}</td><td>¥{:.2f}</td><td>¥{:,.2f}</td><td style='color:{}'>{}¥{:,.2f}</td><td style='color:{}'>{}{:.2f}%</td><td>{}天</td></tr>".format(
            code, pos.get("name", ""), pos["sector"], pos["shares"],
            pos["avg_cost"], price, mv, pc2, ps2, ppnl, pc2, ps2, ppp, hd
        )

    trade_rows = ""
    for t in reversed(acct["trades"][-10:]):
        tt = "买入" if t["type"] == "BUY" else "卖出"
        trade_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>¥{:.2f}</td><td>{}股</td><td>¥{:,.2f}</td><td>{}</td></tr>".format(
            t.get("timestamp", "")[:10], tt, t.get("name", ""), t.get("code", ""),
            t.get("price", 0), t.get("shares", 0), t.get("amount", 0), t.get("reason", "")
        )
    if not trade_rows:
        trade_rows = '<tr><td colspan="8" style="text-align:center;color:#999;">暂无</td></tr>'

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:'Microsoft YaHei',sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#333;background:#f0f2f5;">
<div style="background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.06);">
<h1 style="color:#1a1a1a;border-bottom:3px solid #e74c3c;padding-bottom:10px;margin:0 0 15px;">模拟投资每日财报</h1>
<p style="color:#999;margin:0 0 20px;">{today_str} | {time_str} | 初始资金 ¥{icap:,}</p>

<h2 style="color:#2c3e50;font-size:18px;margin:20px 0 10px;">账户总览</h2>
<div style="display:flex;flex-wrap:wrap;gap:12px;margin:10px 0;">
<div style="flex:1;min-width:140px;background:#f8f9fa;padding:12px;border-radius:8px;text-align:center;"><div style="color:#888;font-size:12px;">总资产</div><div style="font-size:22px;font-weight:bold;color:#2c3e50;">¥{tv:,.2f}</div></div>
<div style="flex:1;min-width:140px;background:#f8f9fa;padding:12px;border-radius:8px;text-align:center;"><div style="color:#888;font-size:12px;">可用资金</div><div style="font-size:22px;font-weight:bold;color:#2980b9;">¥{cash:,.2f}</div></div>
<div style="flex:1;min-width:140px;background:#f8f9fa;padding:12px;border-radius:8px;text-align:center;"><div style="color:#888;font-size:12px;">持仓市值</div><div style="font-size:22px;font-weight:bold;color:#8e44ad;">¥{pv:,.2f}</div></div>
<div style="flex:1;min-width:140px;background:#f8f9fa;padding:12px;border-radius:8px;text-align:center;"><div style="color:#888;font-size:12px;">累计盈亏</div><div style="font-size:22px;font-weight:bold;color:{pc};">{ps}¥{pnl:,.2f}<br><span style="font-size:14px;">({ps}{pnl_pct:.2f}%)</span></div></div>
</div>

<h2 style="color:#2c3e50;font-size:18px;margin:25px 0 10px;">持仓明细</h2>
{pos_table}

<h2 style="color:#2c3e50;font-size:18px;margin:25px 0 10px;">近期交易</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;"><tr style="background:#f8f9fa;"><th style="border:1px solid #eee;padding:6px;">日期</th><th style="border:1px solid #eee;padding:6px;">类型</th><th style="border:1px solid #eee;padding:6px;">名称</th><th style="border:1px solid #eee;padding:6px;">代码</th><th style="border:1px solid #eee;padding:6px;">价格</th><th style="border:1px solid #eee;padding:6px;">数量</th><th style="border:1px solid #eee;padding:6px;">金额</th><th style="border:1px solid #eee;padding:6px;">理由</th></tr>{trade_rows}</table>

<div style="background:#fff3cd;padding:12px;border-left:4px solid #ffc107;margin:20px 0;border-radius:4px;"><strong>风险提示</strong><br>本报告为模拟投资系统自动生成，不构成任何投资建议。股市有风险，投资需谨慎。</div>
</div>
<p style="text-align:center;color:#bbb;font-size:12px;margin-top:16px;">模拟投资系统 v1.0 | 每日 8:00 自动推送 | 腾讯云函数</p>
</body></html>""".format(
        today_str=today_str, time_str=time_str, icap=int(INITIAL_CAPITAL),
        tv=tv, cash=acct["cash"], pv=pv, pc=pc, ps=ps, pnl=pnl, pnl_pct=pnl_pct,
        pos_table="<p style='color:#999;'>当前无持仓</p>" if not pos_rows else "<table style='width:100%;border-collapse:collapse;font-size:13px;'><tr style='background:#f8f9fa;'><th style='border:1px solid #eee;padding:8px;'>代码</th><th style='border:1px solid #eee;padding:8px;'>名称</th><th style='border:1px solid #eee;padding:8px;'>板块</th><th style='border:1px solid #eee;padding:8px;'>持仓</th><th style='border:1px solid #eee;padding:8px;'>成本</th><th style='border:1px solid #eee;padding:8px;'>现价</th><th style='border:1px solid #eee;padding:8px;'>市值</th><th style='border:1px solid #eee;padding:8px;'>盈亏</th><th style='border:1px solid #eee;padding:8px;'>盈亏%</th><th style='border:1px solid #eee;padding:8px;'>天数</th></tr>" + pos_rows + "</table>",
        trade_rows=trade_rows,
    )
    return html


# ===== 主函数 =====
def main_handler(event, context):
    print("===== 模拟投资每日任务 =====")

    # 1. 行情
    print("[1/5] 获取行情...")
    prices = fetch_prices()
    print("  -> {} 只".format(len(prices)))

    # 2. 账户
    print("[2/5] 加载账户...")
    acct = load_account()

    # 3. 检查持仓
    print("[3/5] 检查持仓...")
    sells = check_positions(acct, prices)
    for s in sells:
        execute_sell(acct, s["code"], s["price"], s["shares"], s["reason"])
        print("  卖出 {} {}: {}".format(s["code"], s.get("name", ""), s["reason"]))
    if not sells:
        print("  -> 无卖出信号")

    # 4. 买入
    print("[4/5] 生成买入建议...")
    buys = get_buy_candidates(acct, prices)
    bought = 0
    for b in buys[:2]:
        amount = b["price"] * b["shares"]
        fee = max(amount * 0.00025, 5)
        if amount + fee <= acct["cash"]:
            acct["cash"] -= amount + fee
            acct["positions"][b["code"]] = {
                "shares": b["shares"], "avg_cost": b["price"],
                "buy_date": str(date.today()), "sector": b["sector"],
                "name": b["name"],
            }
            acct["trade_count"] += 1
            acct["trades"].append({
                "type": "BUY", "code": b["code"], "name": b["name"],
                "price": b["price"], "shares": b["shares"], "amount": amount,
                "reason": b["reason"],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            bought += 1
            print("  买入 {} {}: {}股 @ ¥{:.2f}".format(b["code"], b["name"], b["shares"], b["price"]))
    if not bought:
        print("  -> 无买入")

    save_account(acct)

    # 5. 发送邮件
    print("[5/5] 生成财报并发送...")
    html = generate_html(acct, prices)
    subject = "模拟投资每日财报 - " + datetime.now().strftime("%Y-%m-%d")
    ok = send_email(subject, html)

    pv = sum(pos["shares"] * prices.get(code, {}).get("price", 0) for code, pos in acct["positions"].items())
    result = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_value": round(acct["cash"] + pv, 2),
        "positions": len(acct["positions"]),
        "sells": len(sells),
        "buys": bought,
        "email_sent": ok,
    }
    print("完成: " + json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main_handler(None, None)
