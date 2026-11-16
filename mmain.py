"""
OKX USDT 永续合约 MACD 扫描（4H）— 升级版
推送内容分三块：
  1. 刚转色：之前连续同色很久，最近 1~3 根刚翻色（最值得关注）
  2. 红柱 / 绿柱连续 ≥ 16 根，并按「趋势延续 / 反弹 / 回调」分组
  3. 自动排除成交额太小的币
完整列表（含被过滤的）在 Actions 日志里。
"""
import os
import re
import sys
import time

import numpy as np
import pandas as pd
import requests
import urllib3

# ================= 配置（想调松紧，改这里）=================
BASE_URL = "https://www.okx.com"
BAR = "4H"                  # K 线周期
LIMIT = 300                 # 取多少根 K 线（OKX 上限 300）
MIN_CONSECUTIVE = 16        # 连续同色柱至少多少根才算信号
MIN_VOL_3D = 10_000_000     # 近 3 天成交额下限（USDT），低于它的不推送
FLIP_MAX_AGE = 3            # 刚转色：转色后不超过几根
FLIP_MIN_PREV = 16          # 刚转色：转色前至少连续同色几根
SHOW_PER_GROUP = 12         # 每组最多显示几个（其余见日志）
INST_SUFFIX = "-USDT-SWAP"
SLEEP = 0.2                 # 每个币之间的间隔，防止限频
# ==========================================================

# 只有本地网络报 SSL 错误时，才设置环境变量 NO_VERIFY_SSL=1 临时关闭校验
VERIFY_SSL = os.environ.get("NO_VERIFY_SSL") != "1"
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()


# ---------------------- 数据获取 ----------------------
def api_get(path, params, retries=4):
    """带重试的 GET；遇到限频会多等一会"""
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(BASE_URL + path, params=params,
                            timeout=15, verify=VERIFY_SSL)
            j = r.json()
            code = j.get("code")
            if code == "0":
                return j["data"]
            if code in ("50011", "50013"):
                time.sleep(2 + attempt * 2)
                continue
            last_err = f"{code} {j.get('msg')}"
            break
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
        "qvol": [float(r[7] or 0) for r in raw],  # 每根 K 线的 USDT 成交额
    })


# ---------------------- 指标计算 ----------------------
def calculate_macd_hist(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


def streak_at(vals, end):
    """以 end 位置为结尾，往前数连续同号的根数；返回 (根数, 符号)"""
    sign = np.sign(vals[end])
    if sign == 0:
        return 0, 0
    count, i = 0, end
    while i >= 0 and np.sign(vals[i]) == sign:
        count += 1
        i -= 1
    return count, int(sign)


def trend_tag(sign, chg):
    """红绿柱 + 这段时间价格涨跌 -> 是趋势延续，还是反弹/回调"""
    if abs(chg) < 0.01:
        return "➖横盘"
    if sign > 0:
        return "📈上涨延续" if chg > 0 else "↩️下跌中反弹"
    return "📉下跌延续" if chg < 0 else "↩️上涨中回调"


def analyze(df):
    close = df["close"].values
    q = df["qvol"].values
    hist = calculate_macd_hist(df["close"]).values
    n = len(hist)
    count, sign = streak_at(hist, n - 1)
    if sign == 0:
        return None

    # 成交额：4H 线每天 6 根，近 2 天 = 12 根，近 3 天 = 18 根
    vol_2d, vol_3d = q[-12:].sum(), q[-18:].sum()
    prev = q[-60:-18].sum()                      # 再往前 7 天
    ratio = (vol_3d / 3) / (prev / 7) if prev > 0 else float("nan")
    info = {"count": count, "sign": sign, "vol_2d": vol_2d, "vol_3d": vol_3d,
            "ratio": ratio, "low_vol": vol_3d < MIN_VOL_3D}

    # 类型一：连续同色柱
    if count >= MIN_CONSECUTIVE:
        chg = close[-1] / close[max(n - count - 1, 0)] - 1
        a = np.abs(hist[-3:])
        if a[2] > a[1] > a[0]:
            mom = "柱子放大"
        elif a[2] < a[1] < a[0]:
            mom = "柱子缩短"
        else:
            mom = "柱子持平"
        info.update(kind="streak", change=chg, mom=mom, tag=trend_tag(sign, chg))
        return info

    # 类型二：刚转色（之前连续很久，最近刚翻色）
    if count <= FLIP_MAX_AGE and n > count:
        pc, _ = streak_at(hist, n - count - 1)
        if pc >= FLIP_MIN_PREV:
            chg = close[-1] / close[n - count - 1] - 1
            info.update(kind="flip", prev_count=pc, change=chg)
            return info
    return None


# ---------------------- 报告 ----------------------
def fmt_usd(x):
    if x >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{x / 1e4:.0f}万"
    return f"{x:.0f}"


def short(inst):
    return inst.replace(INST_SUFFIX, "")


def vol_text(r):
    rt = "" if np.isnan(r["ratio"]) else f"（日均为前7天的{r['ratio']:.1f}倍）"
    return f"成交额：近2天 {fmt_usd(r['vol_2d'])}U，近3天 {fmt_usd(r['vol_3d'])}U{rt}"


def streak_line(r):
    color = "红" if r["sign"] > 0 else "绿"
    return (f"- **{short(r['inst'])}** {color}{r['count']}根｜期间{r['change']:+.1%}｜{r['mom']}  \n"
            f"  {vol_text(r)}")


def flip_line(r):
    now = "红" if r["sign"] > 0 else "绿"
    before = "绿" if r["sign"] > 0 else "红"
    direction = "转多" if r["sign"] > 0 else "转空"
    return (f"- **{short(r['inst'])}** {before}{r['prev_count']}根后转{now}"
            f"（{direction}，已{r['count']}根）｜转色后{r['change']:+.1%}  \n"
            f"  {vol_text(r)}")


GROUP_ORDER = [
    (1, "📈上涨延续"), (1, "↩️下跌中反弹"),
    (-1, "📉下跌延续"), (-1, "↩️上涨中回调"),
    (1, "➖横盘"), (-1, "➖横盘"),
]


def build_report(results, total, n_low):
    flips = sorted([r for r in results if r["kind"] == "flip"],
                   key=lambda r: -r["vol_3d"])
    streaks = [r for r in results if r["kind"] == "streak"]
    n_red = sum(1 for r in streaks if r["sign"] > 0)
    n_green = len(streaks) - n_red

    parts = [f"共扫描 {total} 个合约｜连续同色 ≥{MIN_CONSECUTIVE} 根：红 {n_red}、绿 {n_green}｜"
             f"成交额不足已过滤 {n_low} 个（近3天 <{fmt_usd(MIN_VOL_3D)}U）"]

    parts.append(f"## 🔄 刚转色（{len(flips)}）")
    parts.append("\n".join(flip_line(r) for r in flips[:SHOW_PER_GROUP]) if flips else "无")

    for sign, tag in GROUP_ORDER:
        grp = sorted([r for r in streaks if r["sign"] == sign and r["tag"] == tag],
                     key=lambda r: -r["count"])
        if not grp:
            continue
        title = f"## {'🔴红柱' if sign > 0 else '🟢绿柱'} · {tag}（{len(grp)}）"
        body = "\n".join(streak_line(r) for r in grp[:SHOW_PER_GROUP])
        if len(grp) > SHOW_PER_GROUP:
            body += f"\n\n另有 {len(grp) - SHOW_PER_GROUP} 个未显示，见 Actions 日志"
        parts += [title, body]

    parts.append(
        "---\n"
        "**怎么看：** 倍数＞1 表示近期放量，＜1 表示缩量；"
        "柱子放大＝动能增强，缩短＝动能减弱；"
        "「反弹/回调」表示红绿柱与价格方向不一致，不是真正的趋势行情。\n\n"
        "仅为技术形态筛选，不构成投资建议。")
    return "\n\n".join(parts), len(flips), n_red, n_green


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


# ---------------------- 主流程 ----------------------
def main():
    print("获取 OKX USDT 永续合约列表...")
    try:
        instruments = get_instruments()
    except Exception as e:
        push("OKX扫描失败", f"获取合约列表失败：{e}")
        print("获取合约列表失败:", e)
        sys.exit(1)

    total = len(instruments)
    print(f"共 {total} 个合约，开始扫描（{BAR}）")

    results, low_vol, failed = [], [], 0
    for idx, inst in enumerate(instruments, 1):
        df = get_candles(inst)
        if df is None:
            failed += 1
        else:
            res = analyze(df)
            if res:
                res["inst"] = inst
                if res["low_vol"]:
                    low_vol.append(res)
                else:
                    results.append(res)
                    kind = "转色" if res["kind"] == "flip" else "连续"
                    print(f"[{idx}/{total}] {inst} {kind} {res['count']} 根 "
                          f"{'红' if res['sign'] > 0 else '绿'}")
        time.sleep(SLEEP)

    if failed > total * 0.3:
        push("OKX扫描异常", f"{total} 个合约里有 {failed} 个获取失败，结果不可靠。")
        print("失败过多，退出")
        sys.exit(1)

    # 日志里打印全部结果（不受每组数量限制）
    global SHOW_PER_GROUP
    keep = SHOW_PER_GROUP
    SHOW_PER_GROUP = 10 ** 6
    full_report, _, _, _ = build_report(results, total, len(low_vol))
    print("\n" + full_report)
    if low_vol:
        print("\n被成交额过滤的：", ", ".join(short(r["inst"]) for r in low_vol))
    SHOW_PER_GROUP = keep

    report, n_flip, n_red, n_green = build_report(results, total, len(low_vol))
    push(f"OKX MACD 转色{n_flip} 红{n_red} 绿{n_green}", report)


if __name__ == "__main__":
    main()
