import os, json, io
from datetime import date
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, MessagingApiBlob, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, ImageMessageContent
import google.generativeai as genai
from PIL import Image

app = FastAPI()

# 讀取環境變數
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

    # 1. 下載 LINE 圖片
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_content = blob_api.get_message_content(event.message.id)
        image = Image.open(io.BytesIO(image_content))

    # 2. Gemini 辨識熱量
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = """
    你是一位營養師，請辨識照片中的餐點與熱量，嚴格只回傳 JSON：
    {"meal_name":"餐點名稱","items":[{"name":"品項","calories":200}],"total_calories":200}
    """
    response = model.generate_content([prompt, image])
    data = json.loads(response.text.replace("```json", "").replace("```", "").strip())
    meal_cals = data.get("total_calories", 0)

    # 3. 累加與計算剩餘熱量
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {"target": DEFAULT_DAILY_TARGET, "records": {}}
    
    today_cals = USER_DATA[user_id]["records"].get(today, 0) + meal_cals
    USER_DATA[user_id]["records"][today] = today_cals
    remaining = USER_DATA[user_id]["target"] - today_cals

    # 4. 回傳訊息
    items = "\n".join([f"- {i['name']}: {i['calories']} kcal" for i in data.get("items", [])])
    reply = f"🍽️ 【{data.get('meal_name')}】\n{items}\n-------------------\n🔥 本餐：{meal_cals} kcal\n📊 今日已攝取：{today_cals} / {USER_DATA[user_id]['target']} kcal\n🎯 剩餘額度：{remaining} kcal"
    
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)])
        )