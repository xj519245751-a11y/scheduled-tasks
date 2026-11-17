# -*- coding: utf-8 -*-
"""
OKX Quant Market Scanner 4.0
USDT 永续合约多空高质量信号扫描器
只读取公开行情，不会自动下单，不需要 API Key。

核心原则：
1. 1小时与4小时趋势必须同向；
2. 必须通过成交额、成交量、RSI、回踩/反抽过滤；
3. 只输出通过全部硬性条件的候选信号；
4. 默认宁缺毋滥，评分不是盈利概率。
"""

import argparse
import csv
import json
import math
from pathlib import Path
from datetime import datetime, timezone

import ccxt


def find_project_root():
    """兼容脚本放在项目根目录或子目录的情况。"""
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent, here.parent.parent):
        if (candidate / "config").exists() or (candidate / "data").exists():
            return candidate
    return here


ROOT = find_project_root()
CONFIG_FILE = ROOT / "config" / "settings.json"
DATA_DIR = ROOT / "data" / "market_data"
REPORT_DIR = ROOT / "reports"

DEFAULTS = {
    "leverage_min": 2,
    "leverage_max": 3,
    "trading_direction": "both",
    "auto_trading": False,
    "require_manual_confirmation": True,

    # 高质量信号过滤参数
    "min_turnover_usdt": 10000000,
    "volume_ratio_min": 0.8,
    "volume_ratio_strong": 1.2,
    "require_4h_confirmation": True,
    "min_signal_score": 80,
    "pullback_max_distance_pct": 1.2,
    "long_rsi_min": 45,
    "long_rsi_max": 65,
    "short_rsi_min": 35,
    "short_rsi_max": 55,

    # 可在 settings.json 中添加不希望扫描的交易对，例如 ["XAU/USDT:USDT"]
    "exclude_symbols": [],
    "exclude_bases": [],

    "top_markets": 100,
    "ohlcv_limit": 120
}


def load_settings():
    config = DEFAULTS.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            if isinstance(user_config, dict):
                config.update(user_config)
        except Exception as exc:
            print("配置文件读取失败，使用内置默认值：", exc)
    return config


def safe_float(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def percentage_change(current, previous):
    if previous is None or previous == 0:
        return 0.0
    return (current / previous - 1.0) * 100.0


def get_turnover(ticker, market):
    """优先读取 OKX 报价成交额字段，缺失时才估算。"""
    info = ticker.get("info") or {}
    price = safe_float(ticker.get("last"))

    quote_volume = safe_float(info.get("volCcyQuote24h"))
    if quote_volume is not None and quote_volume > 0:
        return quote_volume

    base_volume = safe_float(info.get("volCcy24h"))
    if base_volume and price and base_volume > 0:
        return base_volume * price

    contracts = safe_float(info.get("vol24h"))
    contract_size = safe_float(market.get("contractSize"), 1) or 1
    if contracts and price and contracts > 0:
        return contracts * contract_size * price

    base_volume = safe_float(ticker.get("baseVolume"))
    if base_volume and price and base_volume > 0:
        return base_volume * price

    return safe_float(ticker.get("quoteVolume"), 0) or 0


def filter_closed_candles(candles, timeframe_ms, now_ms):
    """仅保留完整收盘K线；交易所K线时间戳为开盘时间。"""
    valid = []
    for row in candles or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        ts = safe_float(row[0])
        close = safe_float(row[4])
        volume = safe_float(row[5])
        if ts is None or close is None or volume is None:
            continue
        if close <= 0 or volume < 0:
            continue
        if ts + timeframe_ms <= now_ms:
            valid.append([ts, *row[1:6]])

    deduped = {int(row[0]): row for row in valid}
    return [deduped[key] for key in sorted(deduped)]


def calculate_ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def calculate_rsi(values, period=14):
    """Wilder RSI。"""
    if len(values) < period + 1:
        return None

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def get_trend_state(closes):
    if len(closes) < 55:
        return "数据不足", False, False
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    slope = percentage_change(ema20[-1], ema20[-5])

    up = closes[-1] > ema20[-1] > ema50[-1] and slope > 0
    down = closes[-1] < ema20[-1] < ema50[-1] and slope < 0

    if up:
        return "多头", True, False
    if down:
        return "空头", False, True
    return "震荡/不明确", False, False


def analyze_market(candles_1h, candles_4h, ticker, turnover, config):
    if len(candles_1h) < 55 or len(candles_4h) < 55:
        raise ValueError("完整收盘K线不足55根")

    closes_1h = [float(row[4]) for row in candles_1h]
    volumes_1h = [float(row[5]) for row in candles_1h]
    closes_4h = [float(row[4]) for row in candles_4h]

    ema20_1h = calculate_ema(closes_1h, 20)
    ema50_1h = calculate_ema(closes_1h, 50)
    rsi = calculate_rsi(closes_1h)

    slope_1h = percentage_change(ema20_1h[-1], ema20_1h[-5])
    trend_up_1h = closes_1h[-1] > ema20_1h[-1] > ema50_1h[-1] and slope_1h > 0
    trend_down_1h = closes_1h[-1] < ema20_1h[-1] < ema50_1h[-1] and slope_1h < 0

    trend_4h, trend_up_4h, trend_down_4h = get_trend_state(closes_4h)

    return_4h = percentage_change(closes_1h[-1], closes_1h[-5])
    return_20h = percentage_change(closes_1h[-1], closes_1h[-21])

    previous_volumes = volumes_1h[-21:-1]
    avg_volume = sum(previous_volumes) / len(previous_volumes) if previous_volumes else 0
    volume_ratio = volumes_1h[-1] / avg_volume if avg_volume > 0 else 0

    # 回踩/反抽：收盘价贴近EMA20，而不是远离均线追价。
    ema_distance_pct = percentage_change(closes_1h[-1], ema20_1h[-1])
    max_distance = safe_float(config.get("pullback_max_distance_pct"), 1.2)

    pullback_long = (
        trend_up_1h
        and ema_distance_pct >= 0
        and ema_distance_pct <= max_distance
        and closes_1h[-1] >= ema50_1h[-1]
    )
    pullback_short = (
        trend_down_1h
        and ema_distance_pct <= 0
        and ema_distance_pct >= -max_distance
        and closes_1h[-1] <= ema50_1h[-1]
    )

    volume_min = safe_float(config.get("volume_ratio_min"), 0.8)
    volume_strong = safe_float(config.get("volume_ratio_strong"), 1.2)
    min_turnover = safe_float(config.get("min_turnover_usdt"), 10000000)

    long_rsi_ok = (
        rsi is not None
        and safe_float(config.get("long_rsi_min"), 45) <= rsi
        <= safe_float(config.get("long_rsi_max"), 65)
    )
    short_rsi_ok = (
        rsi is not None
        and safe_float(config.get("short_rsi_min"), 35) <= rsi
        <= safe_float(config.get("short_rsi_max"), 55)
    )

    long_trend_ok = trend_up_1h and trend_up_4h
    short_trend_ok = trend_down_1h and trend_down_4h

    long_momentum_ok = return_4h > 0 and return_20h > 0
    short_momentum_ok = return_4h < 0 and return_20h < 0

    liquidity_ok = turnover >= min_turnover
    volume_ok = volume_ratio >= volume_min

    # 分数只用于候选排序；硬性条件不通过时绝不发出方向信号。
    long_score = 0
    short_score = 0

    if trend_up_1h:
        long_score += 25
    if trend_down_1h:
        short_score += 25
    if trend_up_4h:
        long_score += 20
    if trend_down_4h:
        short_score += 20
    if long_momentum_ok:
        long_score += 10
    if short_momentum_ok:
        short_score += 10
    if pullback_long:
        long_score += 20
    if pullback_short:
        short_score += 20
    if long_rsi_ok:
        long_score += 10
    if short_rsi_ok:
        short_score += 10
    if volume_ratio >= volume_strong:
        if trend_up_1h:
            long_score += 10
        elif trend_down_1h:
            short_score += 10
    elif volume_ok:
        if trend_up_1h:
            long_score += 5
        elif trend_down_1h:
            short_score += 5
    if liquidity_ok:
        long_score += 5
        short_score += 5

    min_score = int(safe_float(config.get("min_signal_score"), 80))
    require_4h = bool(config.get("require_4h_confirmation", True))

    long_hard_pass = (
        trend_up_1h
        and (trend_up_4h or not require_4h)
        and long_momentum_ok
        and pullback_long
        and long_rsi_ok
        and volume_ok
        and liquidity_ok
    )
    short_hard_pass = (
        trend_down_1h
        and (trend_down_4h or not require_4h)
        and short_momentum_ok
        and pullback_short
        and short_rsi_ok
        and volume_ok
        and liquidity_ok
    )

    direction = "观望"
    if long_hard_pass and long_score >= min_score and long_score > short_score:
        direction = "偏多"
    elif short_hard_pass and short_score >= min_score and short_score > long_score:
        direction = "偏空"

    score = max(long_score, short_score)
    reasons = []
    risks = []

    if trend_up_1h:
        reasons.append("1小时多头趋势")
    elif trend_down_1h:
        reasons.append("1小时空头趋势")
    else:
        risks.append("1小时趋势不明确")

    if trend_up_4h:
        reasons.append("4小时多头确认")
    elif trend_down_4h:
        reasons.append("4小时空头确认")
    else:
        risks.append("4小时趋势未确认")

    if pullback_long:
        reasons.append("价格贴近EMA20，多头回踩形态")
    elif pullback_short:
        reasons.append("价格贴近EMA20，空头反抽形态")
    else:
        risks.append("未满足贴近EMA20的回踩/反抽条件")

    if long_momentum_ok or short_momentum_ok:
        reasons.append("4小时与20小时动量同向")
    else:
        risks.append("短中周期动量不一致")

    if volume_ok:
        reasons.append(f"成交量达标（量比{volume_ratio:.2f}）")
    else:
        risks.append(f"成交量不足（量比{volume_ratio:.2f}，要求≥{volume_min:.2f}）")

    if liquidity_ok:
        reasons.append("24小时成交额达标")
    else:
        risks.append("24小时成交额不足")

    if rsi is None:
        risks.append("RSI数据不足")
    elif not (long_rsi_ok or short_rsi_ok):
        risks.append(f"RSI不在入场区间（当前{rsi:.2f}）")

    if direction == "观望":
        risks.append("未通过全部硬性过滤，不生成交易信号")

    return {
        "price": closes_1h[-1],
        "change_24h_pct": safe_float(ticker.get("percentage"), 0) or 0,
        "turnover_24h_usdt": turnover,
        "direction": direction,
        "score": score,
        "long_score": long_score,
        "short_score": short_score,
        "rsi_1h": round(rsi, 2) if rsi is not None else "",
        "volume_ratio": round(volume_ratio, 3),
        "ema20_distance_pct": round(ema_distance_pct, 3),
        "return_4h_pct": round(return_4h, 3),
        "return_20h_pct": round(return_20h, 3),
        "trend_1h": "多头" if trend_up_1h else "空头" if trend_down_1h else "震荡/不明确",
        "trend_4h": trend_4h,
        "pullback_long": pullback_long,
        "pullback_short": pullback_short,
        "hard_filter_pass": direction in ("偏多", "偏空"),
        "reasons": "；".join(reasons),
        "risk_notes": "；".join(risks) if risks else "仍需人工检查资金费率、支撑阻力和止损位置"
    }


def main():
    parser = argparse.ArgumentParser(description="OKX USDT永续合约多空高质量信号扫描器")
    parser.add_argument("--top", type=int, default=None, help="最多分析成交额排名前N个合约")
    parser.add_argument("--min-turnover", type=float, default=None, help="覆盖最低24小时成交额（USDT）")
    parser.add_argument("--signals-only", action="store_true", help="只输出通过全部硬性过滤的信号")
    args = parser.parse_args()

    config = load_settings()
    top_n = max(1, min(args.top or int(config.get("top_markets", 100)), 200))
    min_turnover = (
        args.min_turnover
        if args.min_turnover is not None
        else safe_float(config.get("min_turnover_usdt"), 10000000)
    )
    config["min_turnover_usdt"] = min_turnover

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print("OKX Quant Market Scanner 4.0")
    print("多空双向 | 严格过滤 | 仅行情扫描 | 不自动下单")
    print("=" * 64)

    exchange = ccxt.okx({
        "enableRateLimit": True,
        "timeout": 60000,
        "options": {"defaultType": "swap"}
    })

    print("正在加载OKX市场……")
    markets = exchange.load_markets()

    excluded_symbols = set(config.get("exclude_symbols") or [])
    excluded_bases = {str(x).upper() for x in (config.get("exclude_bases") or [])}

    valid_markets = {}
    for symbol, market in markets.items():
        if not (
            market.get("active", True)
            and market.get("swap")
            and market.get("quote") == "USDT"
            and market.get("settle") == "USDT"
            and market.get("linear") is not False
        ):
            continue
        if symbol in excluded_symbols:
            continue
        if str(market.get("base", "")).upper() in excluded_bases:
            continue
        valid_markets[symbol] = market

    symbols = list(valid_markets.keys())
    print("符合条件的USDT永续合约：", len(symbols))
    if not symbols:
        print("没有找到符合条件的合约。")
        return

    print("正在获取行情……")
    try:
        tickers = exchange.fetch_tickers(symbols)
    except Exception as exc:
        print("批量行情获取失败，改为逐个获取：", type(exc).__name__)
        tickers = {}
        for symbol in symbols:
            try:
                tickers[symbol] = exchange.fetch_ticker(symbol)
            except Exception:
                continue

    candidates = []
    for symbol, market in valid_markets.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        last = safe_float(ticker.get("last"))
        if last is None or last <= 0:
            continue
        turnover = get_turnover(ticker, market)
        if turnover >= min_turnover:
            candidates.append((symbol, market, ticker, turnover))

    candidates.sort(key=lambda item: item[3], reverse=True)
    selected = candidates[:top_n]
    print(f"有效行情：{len(tickers)}；成交额达标：{len(candidates)}；本轮分析：{len(selected)}")

    results = []
    failures = 0
    ohlcv_limit = max(80, min(int(config.get("ohlcv_limit", 120)), 300))

    for index, (symbol, market, ticker, turnover) in enumerate(selected, 1):
        try:
            raw_1h = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=ohlcv_limit)
            raw_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=ohlcv_limit)
            now_ms = exchange.milliseconds()

            candles_1h = filter_closed_candles(raw_1h, 60 * 60 * 1000, now_ms)
            candles_4h = filter_closed_candles(raw_4h, 4 * 60 * 60 * 1000, now_ms)

            analysis = analyze_market(candles_1h, candles_4h, ticker, turnover, config)
            analysis.update({
                "symbol": symbol,
                "base": market.get("base", ""),
                "leverage_reference": (
                    f"{config.get('leverage_min', 2)}-{config.get('leverage_max', 3)}x"
                ),
                "manual_review_required": True,
                "auto_trading": False
            })
            results.append(analysis)
        except Exception as exc:
            failures += 1
            print(f"跳过 {symbol}：{type(exc).__name__}: {str(exc)[:120]}")

        if index % 10 == 0 or index == len(selected):
            print(f"分析进度：{index}/{len(selected)}")

    results.sort(
        key=lambda item: (
            item["direction"] != "偏多" and item["direction"] != "偏空",
            -item["score"],
            -item["turnover_24h_usdt"]
        )
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
    csv_file = DATA_DIR / f"swap_scan_v4_{timestamp}.csv"
    json_file = REPORT_DIR / f"swap_report_v4_{timestamp}.json"

    fields = [
        "symbol", "base", "price", "turnover_24h_usdt", "change_24h_pct",
        "direction", "score", "long_score", "short_score", "rsi_1h",
        "volume_ratio", "ema20_distance_pct", "return_4h_pct", "return_20h_pct",
        "trend_1h", "trend_4h", "pullback_long", "pullback_short",
        "hard_filter_pass", "reasons", "risk_notes", "leverage_reference",
        "manual_review_required", "auto_trading"
    ]

    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    signals = [item for item in results if item["direction"] in ("偏多", "偏空")]
    display_rows = signals if args.signals_only else results[:20]

    print("\n" + "=" * 64)
    print("扫描完成")
    print("成功分析：", len(results))
    print("读取失败：", failures)
    print("通过全部硬性条件的信号：", len(signals))
    print("多头信号：", sum(1 for x in signals if x["direction"] == "偏多"))
    print("空头信号：", sum(1 for x in signals if x["direction"] == "偏空"))

    if display_rows:
        print("\n扫描结果：")
        for item in display_rows:
            print(
                f"\n{item['symbol']} | {item['direction']} | "
                f"评分 {item['score']} | RSI {item['rsi_1h']} | "
                f"量比 {item['volume_ratio']}"
            )
            print("1小时/4小时趋势：", item["trend_1h"], "/", item["trend_4h"])
            print("依据：", item["reasons"])
            print("风险：", item["risk_notes"])
    else:
        print("\n本轮没有通过全部严格条件的多空信号。宁可空仓等待，不降低门槛凑信号。")

    print("\nCSV报告：", csv_file)
    print("JSON报告：", json_file)
    print("\n安全说明：本程序只读取公开行情，不会下单；评分不是盈利概率或买卖指令。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户中断扫描。")
    except Exception as exc:
        print("\n程序运行失败：", type(exc).__name__, str(exc))
