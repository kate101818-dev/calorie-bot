import os, json, io, re
from datetime import date
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, MessagingApiBlob, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, ImageMessageContent
import google.generativeai as genai
from PIL import Image

app = FastAPI()

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

USER_DATA = {}
DEFAULT_DAILY_TARGET = 1800

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    today = str(date.today())

    try:
        # 1. 下載 LINE 圖片
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            image_content = blob_api.get_message_content(event.message.id)
            image = Image.open(io.BytesIO(image_content))

        # 2. 呼叫 Gemini 進行熱量估算 (多重模型嘗試避免 404)
        prompt = """
        你是一位專業營養師。請分析照片中的食物並估算熱量，僅輸出標準 JSON，格式如下：
        {
          "meal_name": "餐點名稱",
          "items": [{"name": "食物名稱", "calories": 200}],
          "total_calories": 200
        }
        """
        
        response = None
        for model_name in ["gemini-1.5-flash-latest", "models/gemini-1.5-flash", "gemini-1.5-flash", "gemini-1.5-pro-latest"]:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content([prompt, image])
                if response and response.text:
                    break
            except Exception:
                continue

        if not response or not response.text:
            raise Exception("AI 無法辨識此圖片，請換張照片再試一次。")

        # 提取合法 JSON
        raw_text = response.text
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        clean_json = match.group(0) if match else raw_text.strip()
        data = json.loads(clean_json)

        meal_cals = data.get("total_calories", 0)

        # 3. 計算累計與剩餘熱量
        if user_id not in USER_DATA:
            USER_DATA[user_id] = {"target": DEFAULT_DAILY_TARGET, "records": {}}

        today_cals = USER_DATA[user_id]["records"].get(today, 0) + meal_cals
        USER_DATA[user_id]["records"][today] = today_cals
        remaining = USER_DATA[user_id]["target"] - today_cals

        items = "\n".join([f"- {i['name']}: {i['calories']} kcal" for i in data.get("items", [])])
        reply = (
            f"🍽️ 【{data.get('meal_name', '餐點辨識')}】\n"
            f"{items}\n"
            f"-------------------\n"
            f"🔥 本餐預估：{meal_cals} kcal\n"
            f"📊 今日已攝取：{today_cals} / {USER_DATA[user_id]['target']} kcal\n"
            f"🎯 剩餘額度：{remaining} kcal"
        )
    except Exception as e:
        reply = f"❌ 分析失敗：{str(e)}"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)])
        )
