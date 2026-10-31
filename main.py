import os
import requests

def main():
    key = os.environ.get("SERVERCHAN_KEY")
    # Server酱的推送接口
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = {
        "title": "GitHub测试消息",
        "desp": "恭喜！GitHub Actions 已经成功跑通了！这次换成了 Server酱。"
    }
    response = requests.post(url, data=data)
    print("返回结果:", response.text)

if __name__ == "__main__":
    main()
