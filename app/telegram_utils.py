import requests
import os

def send_lead_to_telegram(name, email, phone):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    # 📌 เติมเพื่อเช็คว่าดึงค่าจาก .env มาได้จริงไหม
    print(f"🔑 เช็คคีย์พาส -> Token: {token[:10]}... | Chat ID: {chat_id}")

    message = (
        "🔔 **มีนัดหมายใหม่จาก Chatbot!** 🔔\n\n"
        f"👤 ชื่อ-นามสกุล: {name}\n"
        f"📧 อีเมล: {email}\n"
        f"📞 เบอร์โทรศัพท์: {phone}"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    
    try:
        response = requests.post(url, json=payload)
        # 📌 เติมเพื่อดูผลลัพธ์จากเซิร์ฟเวอร์ Telegram
        print(f"📡 Telegram Response Status: {response.status_code}")
        print(f"💬 Telegram Response Body: {response.text}")
    except Exception as e:
        print(f"🚨 ยิงไป Telegram ไม่สำเร็จเนื่องจากระบบเน็ตเวิร์ก: {e}")
