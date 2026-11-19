# -*- coding: utf-8 -*-
"""Daily intersection report for OKX V2 MACD and V4 scanner."""
import io, json, os, re, sys, zipfile
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

V2_REPO = "xj519245751-a11y/scheduled-tasks"
V2_WORKFLOW = "scan2.yml"
V4_REPO = "xj519245751-a11y/okx-quant-v4"
V4_WORKFLOW = "market_scan_v4.yml"
V4_ARTIFACT_NAME = "okx-quant-v4-reports"
BEIJING = ZoneInfo("Asia/Shanghai")
API = "https://api.github.com"
S = requests.Session()
PUBLIC = requests.Session()
PUBLIC.headers.update({"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"daily-v2-v4-common-coins"})
S.headers.update({"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"daily-v2-v4-common-coins"})
TOKEN = os.environ.get("GITHUB_TOKEN","").strip()
if TOKEN:
    S.headers["Authorization"] = f"Bearer {TOKEN}"

def api_json(url, params=None, public=False):
    client = PUBLIC if public else S
    r = client.get(url, params=params, timeout=45)
    r.raise_for_status()
    return r.json()

def day_window():
    now = datetime.now(BEIJING)
    start = datetime.combine(now.date(), time.min, tzinfo=BEIJING)
    return start.astimezone(timezone.utc), (start+timedelta(days=1)).astimezone(timezone.utc), now.date()

def zulu(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def list_runs(repo, workflow, start, end):
    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs"
    data = api_json(url, {"per_page":100,"created":f"{zulu(start)}..{zulu(end-timedelta(seconds=1))}"}, public=(repo == V4_REPO))
    return data.get("workflow_runs",[])

def get_v2_signals(start,end):
    runs = list_runs(V2_REPO,V2_WORKFLOW,start,end)
    found, log_count = {}, 0
    for run in runs:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        jobs = api_json(run["jobs_url"]).get("jobs",[])
        for job in jobs:
            if job.get("conclusion") != "success":
                continue
            url = f"{API}/repos/{V2_REPO}/actions/jobs/{job['id']}/logs"
            response = S.get(url,timeout=60)
            response.raise_for_status()
            try:
                archive = zipfile.ZipFile(io.BytesIO(response.content))
            except zipfile.BadZipFile:
                print("Warning: V2 job log was not ZIP",job["id"])
                continue
            log_count += 1
            for name in archive.namelist():
                if not name.lower().endswith((".txt",".log")):
                    continue
                text = archive.read(name).decode("utf-8",errors="replace")
                for line in text.splitlines():
                    # Only final qualified report lines, not "检查/未达标" progress.
                    m = re.search(r"-\s+\*\*([A-Z0-9]{2,})\*\*\s+(?:([红绿])(\d+)根|([红绿])(\d+)根后转([红绿]).*?（(转多|转空)",line)
                    if not m:
                        continue
                    symbol = m.group(1)
                    if m.group(6):
                        color, direction = m.group(6), m.group(7)
                        detail = f"转为{color}柱（{direction}）"
                    else:
                        color, count = m.group(2), m.group(3)
                        direction = "偏多形态" if color=="红" else "偏空形态"
                        detail = f"连续{count}根{color}柱（{direction}）"
                    item = found.setdefault(symbol,{"symbol":symbol,"details":[],"runs":[]})
                    if detail not in item["details"]:
                        item["details"].append(detail)
                    if run.get("html_url") and run["html_url"] not in item["runs"]:
                        item["runs"].append(run["html_url"])
    return found,len(runs),log_count

def latest_v4_report(start,end):
    runs = list_runs(V4_REPO,V4_WORKFLOW,start,end)
    runs = sorted([r for r in runs if r.get("status")=="completed" and r.get("conclusion")=="success"],key=lambda r:r.get("created_at",""),reverse=True)
    for run in runs:
        artifacts = api_json(run["artifacts_url"], public=True).get("artifacts",[])
        artifact = next((a for a in artifacts if a.get("name")==V4_ARTIFACT_NAME and not a.get("expired")),None)
        if not artifact:
            continue
        response = PUBLIC.get(artifact["archive_download_url"],timeout=90)
        response.raise_for_status()
        try:
            archive = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile:
            continue
        files = sorted(n for n in archive.namelist() if n.startswith("reports/") and n.endswith(".json"))
        if not files:
            continue
        payload = json.loads(archive.read(files[-1]).decode("utf-8-sig"))
        rows = payload if isinstance(payload,list) else payload.get("results",[])
        return rows,run
    raise RuntimeError("当天没有找到成功的 V4 扫描报告 artifact。")

def base_symbol(value):
    value = str(value or "").strip().upper()
    return value.split("/",1)[0] if "/" in value else value.split("-",1)[0]

def push(title,body):
    key = os.environ.get("SERVERCHAN_KEY_2","").strip()
    if not key:
        raise RuntimeError("缺少 SERVERCHAN_KEY_2，无法推送微信。")
    m = re.match(r"sctp(\d+)t",key)
    url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m else f"https://sctapi.ftqq.com/{key}.send"
    r = requests.post(url,json={"title":title[:30],"desp":body[:30000]},timeout=30)
    r.raise_for_status()
    result = r.json()
    if result.get("code") != 0:
        raise RuntimeError(f"Server酱推送失败：{result}")

def main():
    start,end,day = day_window()
    print(f"汇总北京时间日期：{day}；UTC窗口 {zulu(start)} 至 {zulu(end)}")
    v2,nruns,nlogs = get_v2_signals(start,end)
    v4,run = latest_v4_report(start,end)
    v4map = {base_symbol(x.get("symbol") or x.get("base")):x for x in v4 if base_symbol(x.get("symbol") or x.get("base"))}
    common = [(v2[s],v4map[s]) for s in sorted(v2) if s in v4map]
    lines = [
        f"【V2 + V4 当日共同币种｜北京时间 {day}】","",
        f"V2 当日成功运行：{nruns} 次；读取日志：{nlogs} 份",
        f"V2 有效信号币种：{len(v2)} 个",
        f"V4 当日最新扫描：{run.get('created_at','')} UTC",
        f"V4 扫描币种：{len(v4map)} 个",
        f"两边共同出现：{len(common)} 个","","========== 共同币种 =========="
    ]
    if common:
        for a,b in common:
            lines.extend([
                f"币种：{a['symbol']}",
                f"V2：{'；'.join(a['details'])}",
                f"V4：{b.get('direction','未知')}｜评分 {b.get('score','未知')}",
                f"V4趋势：1小时 {b.get('trend_1h','未知')} / 4小时 {b.get('trend_4h','未知')}",
                f"V4量比：{b.get('volume_ratio','未知')}｜RSI：{b.get('rsi_1h','未知')}",
                f"V4依据：{b.get('reasons','无')}",
                f"V4风险：{b.get('risk_notes','无')}","----------------"
            ])
    else:
        lines.append("今天没有找到同时出现在 V2 有效信号和 V4 当日最新扫描报告中的币种。")
    lines.extend([
        "","说明：共同出现不代表两个系统方向一致。V2红/绿柱是4小时MACD形态；V4方向和评分是综合筛选结果。",
        "仅作信号交叉核对，不构成交易建议，不会自动下单。",
        f"V2运行记录：https://github.com/{V2_REPO}/actions",
        f"V4运行记录：{run.get('html_url','')}"
    ])
    message = "\n".join(lines)
    print(message)
    push(f"V2+V4共同币种 {len(common)}个",message)
    print("Daily common-coins report pushed successfully.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)
