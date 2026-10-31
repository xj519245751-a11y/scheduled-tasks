import requests
import pandas as pd
import numpy as np
import time
import ssl
import urllib3
import os

# ================= 解决 SSL 报错的核心代码 =================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context
# ==========================================================

# ================= 配置 =================
BASE_URL = "https://www.okx.com"
BAR = "4H"                # 4小时线
LIMIT = 100               # 单次请求K线数量
MIN_CONSECUTIVE = 16      # 至少连续同色柱数量
INST_TYPE = "SWAP"        # 合约类型
# =======================================

def get_all_swap_instruments():
    url = f"{BASE_URL}/api/v5/public/instruments"
    params = {"instType": INST_TYPE}
    resp = requests.get(url, params=params, timeout=15, verify=False)
    data = resp.json()
    if data.get("code") != "0":
        raise Exception(f"获取合约列表失败: {data}")
    
    instruments = []
    for item in data["data"]:
        inst_id = item["instId"]
        if inst_id.endswith("-USDT-SWAP") and item.get("state") == "live":
            instruments.append(inst_id)
    return instruments

def get_candles(inst_id, bar=BAR, limit=LIMIT):
    url = f"{BASE_URL}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": limit}
    
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15, verify=False)
            data = resp.json()
            if data.get("code") != "0":
                return None
            raw = data["data"]
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
            df = df.iloc[::-1].reset_index(drop=True)
            df["close"] = df["close"].astype(float)
            return df
        except requests.exceptions.RequestException:
            if attempt < 2:
                time.sleep(1)
                continue
            else:
                return None

def calculate_macd(df, fast=12, slow=26, signal=9):
    if df is None or len(df) < slow + signal:
        return None
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return hist

def count_consecutive_same_sign(hist_series):
    if hist_series is None or len(hist_series) == 0:
        return 0, None
    latest = hist_series.iloc[-1]
    if latest == 0:
        return 0, None
    sign = np.sign(latest)
    count = 0
    for val in reversed(hist_series.values):
        if np.sign(val) == sign:
            count += 1
        else:
            break
    color = "红柱(多头)" if sign > 0 else "绿柱(空头)"
    return count, color

# ================= 发送微信通知的函数 =================
def send_wechat_notification(title, content):
    key = os.environ.get("SERVERCHAN_KEY")
    if not key:
        print("未找到 SERVERCHAN_KEY，跳过推送")
        return
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = {
        "title": title,
        "desp": content
    }
    try:
        requests.post(url, data=data, timeout=10)
        print("微信推送已发送！")
    except Exception as e:
        print(f"微信推送失败: {e}")

# ================= 主程序 =================
def main():
    print("正在获取 OKX USDT 永续合约列表...")
    try:
        instruments = get_all_swap_instruments()
    except Exception as e:
        error_msg = f"获取合约列表失败，请检查网络: {e}"
        print(error_msg)
        send_wechat_notification("❌ 脚本运行失败", error_msg)
        return
        
    print(f"共找到 {len(instruments)} 个 USDT 永续合约，开始筛选...\n")

    results = []
    total = len(instruments)

    for idx, inst_id in enumerate(instruments, 1):
        print(f"[{idx}/{total}] 检查 {inst_id} ...", end=" ")
        df = get_candles(inst_id)
        
        if df is None or len(df) < 40:
            print("数据不足或请求失败，跳过")
            continue

        hist = calculate_macd(df)
        if hist is None:
            print("MACD计算失败，跳过")
            continue

        count, color = count_consecutive_same_sign(hist)
        if count >= MIN_CONSECUTIVE:
            print(f"✅ 发现！{color} 连续 {count} 根")
            results.append({
                "合约": inst_id,
                "颜色": color,
                "连续根数": count,
                "最新MACD柱值": round(hist.iloc[-1], 6)
            })
        else:
            print(f"连续 {count} 根（未达标）")

        time.sleep(0.2)  

    print("\n" + "=" * 60)
    print(f"筛选结果（{BAR} 周期，连续同色 ≥ {MIN_CONSECUTIVE} 根）：")
    print("=" * 60)
    
    # ======= 组织微信推送的内容 =======
    if results:
        df_res = pd.DataFrame(results)
        print(df_res.to_string(index=False))
        
        # 把表格转换成 Markdown 格式发给微信
        markdown_table = df_res.to_markdown(index=False)
        push_content = f"### 🎯 发现符合条件的合约\n\n{markdown_table}\n\n*周期：{BAR} | 阈值：≥{MIN_CONSECUTIVE}根*"
        send_wechat_notification("📈 OKX MACD 扫描结果", push_content)
    else:
        print("没有符合条件的合约。")
        send_wechat_notification("OKX MACD 扫描完毕", f"本次没有发现连续同色 ≥ {MIN_CONSECUTIVE} 根的合约。")

if __name__ == "__main__":
    main()
