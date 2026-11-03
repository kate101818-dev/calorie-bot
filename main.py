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

# 讀取環境變數
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# 初始化 Supabase
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
    """底部快捷引導按鈕"""
    items = [
        QuickReplyItem(action=MessageAction(label="📊 查詢今日進度", text="查詢")),
        QuickReplyItem(action=MessageAction(label="🎯 重新設定目標", text="設定 女 23歲 160公分 50公斤 輕度活動 減重")),
        QuickReplyItem(action=MessageAction(label="🔄 今日歸零
