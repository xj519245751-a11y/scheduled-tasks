import os
import requests

def main():
    # 读取你刚才存的 SPT
    spt = os.environ.get("WXPUSHER_SPT")
    url = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
    data = {
        "content": "恭喜！你的 GitHub Actions 已经成功跑通了！这次是真免费的了！",
        "summary": "GitHub测试消息",
        "contentType": 1,
        "spt": spt
    }
    response = requests.post(url, json=data)
    print("返回结果:", response.text)

if __name__ == "__main__":
    main()
