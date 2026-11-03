import os, json, re
from datetime import date
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, 
    TextMessage, QuickReply, QuickReplyItem, MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent
import google.generativeai as genai
from supabase import create_client, Client

app = FastAPI()

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DEFAULT_DAILY_TARGET_CAL = 1500
DEFAULT_DAILY_TARGET_PROTEIN = 80

def get_or_create_profile(user_id: str):
    res = supabase.table("user_profiles").select("*").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]
    new_profile = {
        "user_id": user_id,
        "target_cal": DEFAULT_DAILY_TARGET_CAL,
        "target_protein": DEFAULT_DAILY_TARGET_PROTEIN,
        "tdee": DEFAULT_DAILY_TARGET_CAL
    }
    supabase.table("user_profiles").insert(new_profile).execute()
    return new_profile

def get_today_record(user_id: str, today_str: str):
    res = supabase.table("daily_records").select("*").eq("user_id", user_id).eq("record_date", today_str).execute()
    if res.data:
        return res.data[0]
    new_record = {
        "user_id": user_id,
        "record_date": today_str,
        "cals": 0,
        "protein": 0
    }
    supabase.table("daily_records").insert(new_record).execute()
    return new_record

def make_quick_reply():
    items = [
        QuickReplyItem(action=MessageAction(label="查詢今日進度", text="查詢")),
        QuickReplyItem(action=MessageAction(label="重新設定目標", text="設定 女 23歲 160公分 50公斤 輕度活動 減重")),
        QuickReplyItem(action=MessageAction(label="今日歸零", text="重設今天")),
        QuickReplyItem(action=MessageAction(label="使用說明", text="說明"))
    ]
    return QuickReply(items=items)

def send_reply(reply_token, text, with_quick_reply=True):
    with ApiClient(configuration) as api_client:
        qr = make_quick_reply() if with_quick_reply else None
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text, quick_reply=qr)]
            )
        )

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(FollowEvent)
def handle_follow(event):
    welcome_text = (
        "歡迎使用個人飲食與熱量小助手！\n\n"
        "【下一步該做什麼？】\n"
        "請先告訴我您的基本資料，幫您計算專屬的每日減肥熱量與蛋白質目標。\n\n"
        "請直接複製並修改以下文字回傳：\n"
        "設定 女 23歲 160公分 50公斤 輕度活動 減重\n\n"
        "（設定好後，每次吃完東西只要直接打字告訴我吃了什麼即可！）"
    )
    send_reply(event.reply_token, welcome_text)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    today_str = str(date.today())

    profile = get_or_create_profile(user_id)

    # 1. 說明指南
    if user_text in ["說明", "help", "怎麼用", "教學"]:
        guide_text = (
            "【使用指南】\n\n"
            "1. 設定個人目標：\n"
            "輸入「設定 性別 年齡 身高 體重 活動量 目標」\n"
            "例：設定 女 23歲 160公分 50公斤 輕度活動 減重\n\n"
            "2. 記錄飲食：\n"
            "直接打字告訴我吃了什麼！\n"
            "例：早餐吃了一份鮪魚蛋餅加一杯無糖豆漿\n\n"
            "3. 快速指令：\n"
            "- 輸入「查詢」看今日剩餘熱量與蛋白質\n"
            "- 輸入「重設今天」清空今日紀錄"
        )
        send_reply(event.reply_token, guide_text)
        return

    # 2. 查詢進度
    if user_text in ["查詢", "今天吃多少", "進度", "今日剩餘"]:
        rec = get_today_record(user_id, today_str)
        t_cal = profile["target_cal"]
        t_pro = profile["target_protein"]
        rem_cal = t_cal - rec["cals"]
        rem_pro = max(0, t_pro - rec["protein"])

        reply = (
            "【今日攝取進度】\n"
            f"- 熱量：{rec['cals']} / {t_cal} kcal (剩餘 {rem_cal} kcal)\n"
            f"- 蛋白質：{rec['protein']} / {t_pro} g (尚需 {rem_pro} g)\n"
        )
        if rem_cal < 0:
            reply += "\n注意：今日熱量已稍微超標囉，多補充水分！"
        else:
            reply += "\n下一步：吃下一餐時直接打字告訴我（例：午餐吃雞胸便當飯半碗）"
        send_reply(event.reply_token, reply)
        return

    # 3. 重設今天
    if user_text in ["重設今天", "歸零"]:
        supabase.table("daily_records").upsert({
            "user_id": user_id,
            "record_date": today_str,
            "cals": 0,
            "protein": 0
        }, on_conflict="user_id,record_date").execute()
        send_reply(event.reply_token, "今日熱量與蛋白質紀錄已歸零！\n下一步：現在可以開始記錄今天的早餐囉！")
        return

    # 4. 個人目標設定與 TDEE 計算
    if user_text.startswith("設定") or ("公分" in user_text and "公斤" in user_text):
        profile_prompt = f"""
        你是一位專業減脂與運動營養師。使用者輸入：
        「{user_text}」
        
        請判斷使用者的性別、年齡、身高(cm)、體重(kg)、活動量與目標。
        1. 使用 Mifflin-St Jeor 計算 BMR。
        2. 依活動度乘以係數得到 TDEE。
        3. 減重熱量建議為 TDEE 扣除 300~500 大卡（不低於 BMR）。
        4. 蛋白質建議量：每公斤體重 1.6~2.0 克。
        
        僅輸出合法 JSON：
        {{
          "bmr": 1250,
          "tdee": 1720,
          "target_calories": 1400,
          "target_protein": 85,
          "summary": "建議說明"
        }}
        """
        try:
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content(profile_prompt)
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            pdata = json.loads(match.group(0) if match else response.text.strip())

            t_cal = pdata.get("target_calories", 1500)
            t_pro = pdata.get("target_protein", 80)
            tdee = pdata.get("tdee", 1800)

            supabase.table("user_profiles").upsert({
                "user_id": user_id,
                "target_cal": t_cal,
                "target_protein": t_pro,
                "tdee": tdee
            }).execute()

            reply = (
                "【個人化目標已設定完成】\n"
                f"- 基礎代謝 (BMR)：{pdata.get('bmr')} kcal\n"
                f"- 維持體重 (TDEE)：{tdee} kcal\n"
                f"- 每日減重熱量：{t_cal} kcal\n"
                f"- 每日蛋白質目標：{t_pro} g\n"
                "-------------------\n"
                f"{pdata.get('summary', '已為您鎖定目標！')}\n\n"
                "【下一步】\n"
                "吃完東西後，直接傳文字告訴我吃了什麼！\n"
                "（例如：早餐吃了一份鮪魚蛋餅加無糖中溫豆）"
            )
        except Exception as e:
            reply = f"設定解析失敗：{str(e)}\n請參考範例格式：設定 女 23歲 160公分 50公斤 輕度活動 減重"
        
        send_reply(event.reply_token, reply)
        return

    # 5. 記錄口述餐點
    diet_prompt = f"""
    你是一位專業營養師。使用者口述了吃了什麼：
    「{user_text}」

    請辨識食物品項、份量、熱量(kcal)與蛋白質含量(g)，嚴格只回傳合法 JSON：
    {{
      "meal_name": "餐點總結或餐別",
      "items": [
        {{"name": "品項與份量", "calories": 250, "protein_g": 18}}
      ],
      "total_calories": 250,
      "total_protein": 18
    }}
    """
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(diet_prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        data = json.loads(match.group(0) if match else response.text.strip())

        meal_cals = data.get("total_calories", 0)
        meal_protein = data.get("total_protein", 0)

        rec = get_today_record(user_id, today_str)
        new_cals = rec["cals"] + meal_cals
        new_protein = rec["protein"] + meal_protein

        supabase.table("daily_records").upsert({
            "user_id": user_id,
            "record_date": today_str,
            "cals": new_cals,
            "protein": new_protein
        }, on_conflict="user_id,record_date").execute()

        t_cal = profile["target_cal"]
        t_pro = profile["target_protein"]
        remaining_cals = t_cal - new_cals
        remaining_protein = max(0, t_pro - new_protein)

        items_desc = "\n".join([
            f"- {i['name']}: {i.get('calories', 0)} kcal | 蛋 {i.get('protein_g', 0)}g"
            for i in data.get("items", [])
        ])

        reply = (
            f"【{data.get('meal_name', '餐點辨識')}】\n"
            f"{items_desc}\n"
            "-------------------\n"
            f"本餐總計：{meal_cals} kcal | 蛋白質 {meal_protein}g\n"
            f"今日熱量：{new_cals} / {t_cal} kcal (剩餘 {remaining_cals} kcal)\n"
            f"今日蛋白質：{new_protein} / {t_pro} g (尚需 {remaining_protein} g)\n"
        )
        if remaining_cals < 0:
            reply += "\n注意：今日熱量已稍微超額，建議多喝水休息！"
        else:
            reply += "\n下一步：吃完下一餐時直接打字傳送；隨時可點擊下方按鈕查詢進度。"

    except Exception as e:
        reply = f"分析失敗：{str(e)}"

    send_reply(event.reply_token, reply)
