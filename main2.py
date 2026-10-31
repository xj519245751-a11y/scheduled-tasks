import os
import requests

# ================= 你的新脚本逻辑写在这里 =================
def run_my_new_script():
    # 这里先假装跑了一下，实际你可以换成你抓数据、算指标的代码
    print("正在运行新脚本...")
    
    # 假设这是脚本跑出来的结果
    result_text = "这是一个新脚本的测试结果！"
    return result_text
# ==========================================================

# ================= WxPusher 推送函数 =================
def send_wxpusher(title, content):
    spt = os.environ.get("WXPUSHER_SPT")
    if not spt:
        print("未找到 WXPUSHER_SPT，跳过推送")
        return
        
    url = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
    data = {
        "content": f"<h3>{title}</h3><p>{content}</p >",
        "summary": title,
        "contentType": 2, # 2 代表 HTML 格式
        "spt": spt
    }
    try:
        resp = requests.post(url, json=data, timeout=10)
        print("WxPusher 推送返回:", resp.text)
    except Exception as e:
        print(f"WxPusher 推送失败: {e}")
# =====================================================

if __name__ == "__main__":
    # 1. 运行你的脚本拿到结果
    final_result = run_my_new_script()
    
    # 2. 推送到微信
    send_wxpusher("🚀 新脚本运行完毕", final_result)
