"""
OKX USDT 永续合约 MACD 连续同色柱扫描（4H）
改进点：
  1. 只用已收盘的 K 线，结果不会因为未走完的 K 线来回跳
  2. K 线取 300 根，MACD 更准；遇到 OKX 限频会自动等待重试
  3. 结果按连续根数排序，并显示这段时间的涨跌幅
  4. 结果推送到微信（Server酱），没有符合条件时也会推送一条，
     这样"没收到推送"就说明脚本出问题了
  5. 出错时推送报错并让 Actions 变红，GitHub 会发邮件提醒
  6. 默认校验 SSL（GitHub 云端不需要关闭）
"""
import os
import re
import sys
import time

import numpy as np
import pandas as pd
import requests
import urllib3

# ================= 配置 =================
BASE_URL = "https://www.okx.com"
BAR = "4H"               # K 线周期
LIMIT = 300              # 取多少根 K 线（OKX 上限 300）
MIN_CONSECUTIVE = 16     # 至少连续同色柱数量
INST_SUFFIX = "-USDT-SWAP"
SLEEP = 0.2              # 每个币之间的间隔，防止限频
# ========================================

# 只有本地网络报 SSL 错误时，才设置环境变量 NO_VERIFY_SSL=1 临时关闭校验
VERIFY_SSL = os.environ.get("NO_VERIFY_SSL") != "1"
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()


def api_get(path, params, retries=4):
    """带重试的 GET；遇到限频(50011)会多等一会"""
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(BASE_URL + path, params=params,
                            timeout=15, verify=VERIFY_SSL)
            j = r.json()
            code = j.get("code")
            if code == "0":
                return j["data"]
            if code in ("50011", "50013"):      # 请求过于频繁 / 系统繁忙
                time.sleep(2 + attempt * 2)
                continue
            last_err = f"{code} {j.get('msg')}"
            break                                # 其他错误，重试没意义
        except Exception as e:
            last_err = str(e)
            time.sleep(1 + attempt)
    raise RuntimeError(last_err or "未知错误")


def get_instruments():
    data = api_get("/api/v5/public/instruments", {"instType": "SWAP"})
    return [d["instId"] for d in data
            if d["instId"].endswith(INST_SUFFIX) and d.get("state") == "live"]


def get_candles(inst_id):
    try:
        raw = api_get("/api/v5/market/candles",
                      {"instId": inst_id, "bar": BAR, "limit": LIMIT})
    except RuntimeError as e:
        print(f"  [跳过] {inst_id}: {e}")
        return None
    raw = [r for r in raw if r[8] == "1"]        # 只保留已收盘 K 线
    if len(raw) < 60:
        return None
    raw.reverse()                                # 旧 -> 新
    return pd.DataFrame({
        "close": [float(r[4]) for r in raw],
        "qvol": [float(r[7] or 0) for r in raw],     # 每根 K 线的 USDT 成交额
    })


def calculate_macd_hist(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


def count_streak(hist):
    """从最新一根往前数，连续同号的根数；返回 (根数, 符号)"""
    vals = hist.values
    sign = np.sign(vals[-1])
    if sign == 0:
        return 0, 0
    count = 0
    for v in vals[::-1]:
        if np.sign(v) == sign:
            count += 1
        else:
            break
    return count, int(sign)


def analyze(df):
    hist = calculate_macd_hist(df["close"])
    count, sign = count_streak(hist)
    if count < MIN_CONSECUTIVE:
        return None
    close = df["close"].values
    # 这段连续同色期间的涨跌幅（从第一根同色柱之前的收盘价算起）
    base_idx = len(close) - count - 1
    change = close[-1] / close[base_idx] - 1 if base_idx >= 0 else float("nan")
    # 成交额：4H 线每天 6 根，近 2 天 = 12 根，近 3 天 = 18 根
    q = df["qvol"].values
    vol_2d, vol_3d = q[-12:].sum(), q[-18:].sum()
    prev = q[-60:-18].sum()                      # 再往前 7 天
    ratio = (vol_3d / 3) / (prev / 7) if prev > 0 else float("nan")
    return {"sign": sign, "count": count, "change": change,
            "hist": hist.iloc[-1], "vol_2d": vol_2d, "vol_3d": vol_3d,
            "ratio": ratio}


def fmt_usd(x):
    if x >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{x / 1e4:.0f}万"
    return f"{x:.0f}"


def build_report(results):
    reds = sorted([r for r in results if r["sign"] > 0], key=lambda r: -r["count"])
    greens = sorted([r for r in results if r["sign"] < 0], key=lambda r: -r["count"])

    def fmt(group):
        if not group:
            return "无"
        lines = []
        for r in group:
            ch = "" if np.isnan(r["change"]) else f"，期间{r['change']:+.1%}"
            rt = "" if np.isnan(r["ratio"]) else f"（日均为前7天的{r['ratio']:.1f}倍）"
            lines.append(
                f"- {r['inst'].replace('-SWAP', '')}：连续 {r['count']} 根{ch}\n"
                f"  成交额：近2天 {fmt_usd(r['vol_2d'])}U，近3天 {fmt_usd(r['vol_3d'])}U{rt}")
        return "\n".join(lines)

    return (f"### 红柱（多头）连续 ≥ {MIN_CONSECUTIVE} 根\n\n{fmt(reds)}\n\n"
            f"### 绿柱（空头）连续 ≥ {MIN_CONSECUTIVE} 根\n\n{fmt(greens)}\n"), len(reds), len(greens)


def push(title, desp):
    """推送到微信（Server酱）。自动识别 SCT 和 sctp 两种 SendKey"""
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        print("未设置 SERVERCHAN_KEY，跳过推送")
        return
    m = re.match(r"sctp(\d+)t", key)
    url = (f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m
           else f"https://sctapi.ftqq.com/{key}.send")
    try:
        r = requests.post(url, json={"title": title[:30], "desp": desp[:30000]},
                          timeout=20)
        print("推送结果:", r.status_code, r.text[:200])
    except Exception as e:
        print("推送失败:", e)


def main():
    print("获取 OKX USDT 永续合约列表...")
    try:
        instruments = get_instruments()
    except Exception as e:
        push("OKX扫描失败", f"获取合约列表失败：{e}")
        print("获取合约列表失败:", e)
        sys.exit(1)

    total = len(instruments)
    print(f"共 {total} 个合约，开始扫描（{BAR}，连续同色 ≥ {MIN_CONSECUTIVE} 根）")

    results, failed = [], 0
    for idx, inst in enumerate(instruments, 1):
        df = get_candles(inst)
        if df is None:
            failed += 1
        else:
            res = analyze(df)
            if res:
                res["inst"] = inst
                results.append(res)
                print(f"[{idx}/{total}] {inst} 连续 {res['count']} 根 "
                      f"{'红' if res['sign'] > 0 else '绿'}")
        time.sleep(SLEEP)

    # 失败太多说明网络或接口有问题，不要假装"没信号"
    if failed > total * 0.3:
        push("OKX扫描异常", f"{total} 个合约里有 {failed} 个获取失败，结果不可靠。")
        print("失败过多，退出")
        sys.exit(1)

    report, n_red, n_green = build_report(results)
    print("\n" + report)
    push(f"OKX MACD 红{n_red} 绿{n_green}", report)


if __name__ == "__main__":
    main()
