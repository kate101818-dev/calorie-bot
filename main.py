import os, json, re
from datetime import date
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import google.generativeai as genai

app = FastAPI()

# 讀取環境變數
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# 儲存使用者設定與每日紀錄
# 結構: { user_id: { "target": 1500, "tdee": 1800, "profile": "...", "records": { "2026-11-03": 850 } } }
USER_DATA = {}
DEFAULT_DAILY_TARGET = 1500

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    today_str = str(date.today())

    # 初始化使用者資料庫空間
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            "target": DEFAULT_DAILY_TARGET,
            "tdee": DEFAULT_DAILY_TARGET,
            "records": {}
        }

    # 1. 指令：手動查詢今天攝取
    if user_text in ["查詢", "今天吃多少", "進度", "今日剩餘"]:
        current_today = USER_DATA[user_id]["records"].get(today_str, 0)
        target = USER_DATA[user_id]["target"]
        remaining = target - current_today
        reply = (
            f"📊 【今日熱量進度】\n"
            f"🔥 今日已攝取：{current_today} / {target} kcal\n"
            f"🎯 剩餘可用熱量：{remaining} kcal\n"
        )
        if remaining < 0:
            reply += "⚠️ 今日已超過目標熱量，建議適量補充水分！"
        send_reply(event.reply_token, reply)
        return

    # 2. 指令：重設今天
    if user_text in ["重設今天", "歸零"]:
        USER_DATA[user_id]["records"][today_str] = 0
        send_reply(event.reply_token, "🔄 今日攝取熱量已歸零，可以開始記錄囉！")
        return

    # 3. 指令：設定個人資料與計算 TDEE / 減肥熱量
    # 支援格式包含關鍵字「設定」或含有「公分/公斤」
    if user_text.startswith("設定") or ("公分" in user_text and "公斤" in user_text):
        profile_prompt = f"""
        你是一位專業減脂營養師。使用者想要計算減肥熱量，輸入如下：
        「{user_text}」
        
        請判斷使用者的性別、年齡、身高(cm)、體重(kg)、活動量與目標。
        1. 使用 Mifflin-St Jeor 公式計算基礎代謝率 (BMR)：
           - 男：10 * 體重(kg) + 6.25 * 身高(cm) - 5 * 年齡 + 5
           - 女：10 * 體重(kg) + 6.25 * 身高(cm) - 5 * 年齡 - 161
        2. 依活動度乘以係數得到 TDEE (久坐 1.2、輕度 1.375、中度 1.55、重度 1.725；未註明預設輕度 1.375)。
        3. 減重熱量建議為 TDEE 扣除 300~500 大卡（最低不低於 BMR）。
        
        請務必僅以合法 JSON 格式回傳，不得加入任何額外說明文字：
        {{
          "bmr": 1250,
          "tdee": 1720,
          "target_calories": 1400,
          "summary": "簡短的一兩句建議與分析"
        }}
        """
        try:
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(profile_prompt)
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            pdata = json.loads(match.group(0) if match else response.text.strip())

            target_cals = pdata.get("target_calories", 1500)
            USER_DATA[user_id]["target"] = target_cals
            USER_DATA[user_id]["tdee"] = pdata.get("tdee", 1800)

            reply = (
                f"🎯 【個人化熱量目標已設定】\n"
                f"🔹 基礎代謝 (BMR)：{pdata.get('bmr')} kcal\n"
                f"🔹 維持體重 (TDEE)：{pdata.get('tdee')} kcal\n"
                f"🔥 每日減重目標熱量：{target_cals} kcal\n"
                f"-------------------\n"
                f"💡 {pdata.get('summary', '已為您鎖定減重每日目標！')}\n\n"
                f"👉 接下來只要直接打字告訴我吃了什麼即可（例：早餐吃了一份鮪魚蛋餅加溫奶茶）。"
            )
        except Exception as e:
            reply = f"❌ 設定解析失敗：{str(e)}\n建議格式：設定 女 23歲 160公分 50公斤 中度活動 減重"
        
        send_reply(event.reply_token, reply)
        return

    # 4. 預設：文字口述飲食熱量計算
    diet_prompt = f"""
    你是一位專業營養師。使用者口述了今天某一餐吃的東西：
    「{user_text}」

    請辨識裡面的食物品項、推估份量與熱量，嚴格只回傳合法 JSON：
    {{
      "meal_name": "餐點總結或餐別 (例如：早餐、午餐或自訂餐點)",
      "items": [
        {{"name": "品項名稱與份量推估", "calories": 250}}
      ],
      "total_calories": 250
    }}
    """
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(diet_prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        data = json.loads(match.group(0) if match else response.text.strip())

        meal_cals = data.get("total_calories", 0)

        # 累加今日總熱量
        current_today = USER_DATA[user_id]["records"].get(today_str, 0) + meal_cals
        USER_DATA[user_id]["records"][today_str] = current_today

        target = USER_DATA[user_id]["target"]
        remaining = target - current_today

        items_str = "\n".join([f"- {i['name']}: {i['calories']} kcal" for i in data.get("items", [])])
        reply = (
            f"🍽️ 【{data.get('meal_name', '餐點辨識')}】\n"
            f"{items_str}\n"
            f"-------------------\n"
            f"🔥 本餐預估：{meal_cals} kcal\n"
            f"📊 今日已攝取：{current_today} / {target} kcal\n"
            f"🎯 今日剩餘熱量：{remaining} kcal\n"
        )
        if remaining < 0:
            reply += "⚠️ 今日額度已用完，建議多喝水，晚點放鬆休息！"
        else:
            reply += "💪 離目標還有餘裕，繼續保持！"

    except Exception as e:
        reply = f"❌ 分析失敗：{str(e)}"

    send_reply(event.reply_token, reply)

def send_reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
