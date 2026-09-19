"""
校網公告自動化智慧篩選與推播系統
=================================
流程（快取版）：
1. 讀取 last_checked.json（上次已看過的公告 ID 清單）
2. 透過學校網站的 RSS 訂閱抓取公告列表
3. 找出「新」公告（不在 last_checked.json 裡的）
4. 對每則新公告呼叫 Gemini API，判斷是否符合 USER_CRITERIA
5. 符合的公告不會馬上發送，而是先存進 pending_matches.json（暫存清單）
6. 只有當這次執行的「台灣時間小時」落在 DIGEST_HOURS 指定的時間點，
   而且這個時段還沒發送過，才會把暫存清單裡累積的所有公告，
   一次組成一則訊息推播到 LINE 群組，發送完畢後清空暫存清單
7. 更新 last_checked.json（交給 GitHub Actions 去 commit）

這個設計讓「多久檢查一次」跟「多久通知一次」分開：
可以把排程設定得很密集（例如每小時跑一次），確保不會因為
RSS 只顯示最新 10 則、公告發布速度快，而漏抓公告；
但通知本身只會在你指定的時間點（例如早上 6 點、下午 4 點）發送，
不會因為檢查得勤而變成一直跳訊息轟炸群組。

同時用 state["last_sent_slot"] 記住「這個時段已經發過了」，
防止 GitHub Actions 排程延遲、把好幾次積欠的執行擠在同一小時內
連續觸發，導致同一時段被誤判成好幾次獨立發送時機、重複浪費
LINE 的免費訊息額度——這正是先前一天暴增到十幾則推播的根本原因。

本版針對「臺北市立成功高級中學」設定，直接讀取該校的 RSS 訂閱網址
（例如 https://www.cksh.tp.edu.tw/category/news/feed/ ），
比爬 HTML 頁面穩定，也不用猜 CSS 樣式。
若換成別的學校網站，只要該站也有 RSS（大部分學校網站的頁尾都會有
「RSS」連結），把 TARGET_URL 換成該校 RSS 網址即可直接使用；
若該校沒有 RSS，才需要改回 HTML 爬蟲的寫法。
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests
import feedparser
import google.generativeai as genai

STATE_FILE = "last_checked.json"
PENDING_FILE = "pending_matches.json"
GEMINI_MODEL = "gemini-3.6-flash"
REQUEST_TIMEOUT = 15
TAIWAN_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 狀態讀寫（已看過的公告 ID）
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_ids": [], "updated_at": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 暫存清單讀寫（已判斷符合條件、但還沒發送的公告）
# ---------------------------------------------------------------------------
def load_pending() -> list[dict]:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_pending(pending: list[dict]) -> None:
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 抓取公告列表（透過 RSS）
# ---------------------------------------------------------------------------
def fetch_announcements(url: str) -> list[dict]:
    """
    讀取學校的 RSS 訂閱網址，回傳 [{id, title, link, date}, ...]

    RSS 是網站自己提供的、結構固定的公告清單，比爬 HTML 頁面穩定，
    不需要自己猜 CSS 樣式。每個 <item> 對應一則公告：
        - guid 或 link 當作唯一 id（用來判斷是不是「新」公告）
        - title 是標題
        - published 是發佈時間
    """
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SchoolNoticeBot/1.0)"},
    )
    resp.raise_for_status()

    feed = feedparser.parse(resp.content)

    announcements = []
    for entry in feed.entries:
        link = getattr(entry, "link", "").strip()
        title = getattr(entry, "title", "").strip()
        uid = getattr(entry, "id", None) or link or title
        date_str = getattr(entry, "published", "") or getattr(entry, "updated", "")

        announcements.append(
            {"id": uid, "title": title, "link": link, "date": date_str}
        )

    return announcements


def filter_new(announcements: list[dict], state: dict) -> list[dict]:
    known_ids = set(state.get("last_ids", []))
    return [a for a in announcements if a["id"] not in known_ids]


# ---------------------------------------------------------------------------
# Gemini 判斷
# ---------------------------------------------------------------------------
def check_with_gemini(announcement: dict, criteria: str, api_key: str) -> dict:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一個校園公告篩選助手。請根據使用者條件，判斷這則公告是否相關。

使用者條件：{criteria}

公告標題：{announcement['title']}
公告日期：{announcement['date']}

只回傳 JSON，不要任何其他文字或說明，格式如下：
{{"is_match": true 或 false, "reason": "一句話說明理由"}}"""

    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    text = (response.text or "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"is_match": False, "reason": "Gemini 回傳格式解析失敗"}


# ---------------------------------------------------------------------------
# 縮短網址
# ---------------------------------------------------------------------------
def shorten_url(url: str) -> str:
    """
    用 TinyURL 的免費 API 把網址縮短，不需要註冊、不需要金鑰。
    如果縮短失敗（例如網路問題、服務暫時掛掉），就直接回傳原本的完整網址，
    確保就算縮網址這步出狀況，訊息還是照常能發送、連結還是能點。
    """
    try:
        resp = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10,
        )
        if resp.status_code == 200 and resp.text.strip().startswith("http"):
            return resp.text.strip()
    except Exception as e:
        print(f"[縮網址失敗，改用原網址] {e}", file=sys.stderr)
    return url


# ---------------------------------------------------------------------------
# LINE 推播
# ---------------------------------------------------------------------------
def send_line_message(token: str, group_id: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {"to": group_id, "messages": [{"type": "text", "text": text[:4900]}]}

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        print(f"[LINE 推播失敗] {resp.status_code}: {resp.text}", file=sys.stderr)
    else:
        print("[LINE 推播成功]")


# ---------------------------------------------------------------------------
# 組合彙整訊息
# ---------------------------------------------------------------------------
def build_digest_message(matched_items: list[dict]) -> str:
    """
    把所有符合條件的公告組成一則訊息，格式：

    【校網公告小幫手】2026/09/01
    1.公告名稱
    公告頁面連結
    2.公告名稱
    公告頁面連結
    ⚠️AI工具可能出錯
    重要公告請自行留意校網首頁
    """
    today = datetime.now(TAIWAN_TZ).strftime("%Y/%m/%d")
    lines = [f"【校網公告小幫手】{today}"]

    for i, item in enumerate(matched_items, start=1):
        lines.append(f"{i}.{item['title']}")
        lines.append(item["link"])

    lines.append("⚠️AI工具可能出錯")
    lines.append("重要公告請自行留意校網首頁")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 判斷這次執行是不是「該發送彙整訊息」的時間點
# ---------------------------------------------------------------------------
def should_send_digest(state: dict, digest_hours_env: str) -> bool:
    """
    DIGEST_HOURS 環境變數格式：逗號分隔的小時數字（台灣時間，24 小時制），
    例如 "6,16" 代表早上 6 點跟下午 4 點。
    只要目前台灣時間的「小時」落在這個清單裡，這次執行就符合發送資格。

    但光靠「現在是幾點」判斷還不夠：如果 GitHub Actions 排程延遲、
    把好幾次積欠的執行擠在同一小時內連續觸發，會導致同一個時段被
    誤判成好幾次獨立的發送時機、重複發送好幾次彙整訊息，白白浪費
    LINE 的免費訊息額度。
    這裡額外用 state["last_sent_slot"] 記住「這個時段（日期+小時）
    已經發送過了」，同一時段內只會真的送出第一次，之後重複觸發的
    都會被這裡擋下來。
    """
    try:
        hours = {int(h.strip()) for h in digest_hours_env.split(",") if h.strip()}
    except ValueError:
        print(f"[警告] DIGEST_HOURS 格式錯誤：{digest_hours_env!r}，本次不發送", file=sys.stderr)
        return False

    now = datetime.now(TAIWAN_TZ)
    if now.hour not in hours:
        return False

    current_slot = now.strftime("%Y-%m-%d-%H")
    if state.get("last_sent_slot") == current_slot:
        print(f"[跳過] {current_slot} 這個時段已經發送過彙整訊息，避免重複推播")
        return False

    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    target_url = os.environ["TARGET_URL"]
    gemini_key = os.environ["GEMINI_API_KEY"]
    line_token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    line_group = os.environ["LINE_GROUP_ID"]
    criteria = os.environ.get("USER_CRITERIA", "")
    digest_hours_env = os.environ.get("DIGEST_HOURS", "6,16")

    state = load_state()
    pending = load_pending()

    try:
        announcements = fetch_announcements(target_url)
    except Exception as e:
        print(f"[爬取失敗] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"抓到 {len(announcements)} 則公告")

    new_items = filter_new(announcements, state)
    print(f"其中 {len(new_items)} 則為新公告")

    new_matches = 0
    for item in new_items:
        try:
            result = check_with_gemini(item, criteria, gemini_key)
        except Exception as e:
            print(f"[Gemini 判斷失敗] {item['title']}: {e}", file=sys.stderr)
            continue

        print(f"- {item['title']} -> {result}")

        if result.get("is_match"):
            short_link = shorten_url(item["link"])
            pending.append({"title": item["title"], "link": short_link})
            new_matches += 1

    print(f"本次新增 {new_matches} 則符合條件的公告到暫存清單（目前累積 {len(pending)} 則）")

    # 先把暫存清單存檔，不論這次是不是發送時間點，都要保留累積結果
    save_pending(pending)

    if pending and should_send_digest(state, digest_hours_env):
        sent_count = len(pending)
        msg = build_digest_message(pending)
        send_line_message(line_token, line_group, msg)
        pending = []
        save_pending(pending)
        # 記住這個時段已經發送過，防止同一小時內被重複觸發時再發一次，
        # 這是避免額度被異常消耗的根本防護
        state["last_sent_slot"] = datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d-%H")
        print(f"完成。本次發送彙整訊息，共 {sent_count} 則公告已清空暫存。")
    elif pending:
        print(f"目前非發送時間點或本時段已發送過（DIGEST_HOURS={digest_hours_env}），先累積在暫存清單，暫不推播。")
    else:
        print("暫存清單目前是空的，不推播。")

    # 更新狀態：以「這次抓到的公告清單」作為下次比對基準
    # （校網公告列表頁通常只顯示最近 N 筆，用完整清單取代即可，
    #   不需要無限累加，檔案才不會一直長大）
    state["last_ids"] = [a["id"] for a in announcements]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
