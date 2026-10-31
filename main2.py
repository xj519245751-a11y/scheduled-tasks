import os
import requests

# ================= 这里写你的新脚本逻辑 =================
def run_my_new_script():
    # 假装跑了一下，你可以换成你真实要跑的代码
    print("正在运行新脚本...")
    result_text = "这是第二个脚本跑出来的测试结果！"
    return result_text
# ==========================================================

# ================= 企业微信推送函数 =================
def send_wecom(title, content):
    webhook_url = os.environ.get("WECOM_WEBHOOK")
    if not webhook_url:
        print("未找到 WECOM_WEBHOOK，跳过推送")
        return
        
    # 企业微信支持 Markdown 格式，排版好看
    markdown_content = f"## {title}\n\n{content}"
    data = {
        "msgtype": "markdown",
        "markdown": {
            "content": markdown_content
        }
    }
    
    try:
        resp = requests.post(webhook_url, json=data, timeout=10)
        print("企业微信推送返回:", resp.text)
    except Exception as e:
        print(f"企业微信推送失败: {e}")
# =====================================================

if __name__ == "__main__":
    # 1. 执行你的逻辑
    final_result = run_my_new_script()
    
    # 2. 发送到企业微信
    send_wecom("🚀 第二个脚本运行完毕", final_result)
