from fastapi import FastAPI, Response, Cookie, HTTPException, Depends, status, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import uuid
import os
import shutil  # 📌 [เพิ่มใหม่] นำเข้าโมดูลจัดการไฟล์อย่างมีประสิทธิภาพ

from .chatbot import get_chat_response, reload_knowledge, get_vectorstore_debug_data, get_system_prompt, update_system_prompt
from .telegram_utils import send_lead_to_telegram

app = FastAPI(docs_url=None, redoc_url=None) # 🔒 ปิดหน้าเอกสาร API เพื่อความปลอดภัย

# 🔐 ตั้งค่า Username / Password สำหรับแอดมิน (สามารถย้ายไปใส่ .env ได้ครับ)
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "condobot2026")
ADMIN_SESSION_TOKEN = "secure_admin_logged_in_token_xyz" # คีย์ตรวจสอบสิทธิ์

class RedisMemoryBackend:
    def __init__(self): 
        self.db = {}
    def get_history(self, session_id): 
        return self.db.get(session_id, [])
    def save_history(self, session_id, history): 
        self.db[session_id] = history[-20:]

memory_backend = RedisMemoryBackend()

class ChatRequest(BaseModel):
    message: str

class PromptRequest(BaseModel):
    prompt: str

class LoginRequest(BaseModel):
    username: str
    password: str

# 🔒 ฟังก์ชันตรวจสอบสิทธิ์ (Security Guard)
async def verify_admin_auth(admin_session: str = Cookie(None)):
    if not admin_session or admin_session != ADMIN_SESSION_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="กรุณาเข้าสู่ระบบก่อนใช้งาน"
        )
    return admin_session

# ==========================================
# API หน้าบ้านและการแชท
# ==========================================
@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest, response: Response, session_id: str = Cookie(None)):
    if not session_id:
        session_id = str(uuid.uuid4())
        response.set_cookie(key="session_id", value=session_id, max_age=3600*24*7)

    user_message = request.message
    bot_reply = get_chat_response(user_message, session_id, memory_backend)
    return {"reply": bot_reply}

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("web/index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>⚠️ System Error</h1><p>ไม่พบไฟล์ index.html</p>"

# ==========================================
# ระบบล็อกอินหลังบ้าน
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def serve_login_page():
    try:
        with open("web/login.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>⚠️ System Error</h1><p>ไม่พบไฟล์ login.html</p>"

@app.post("/api/admin/login")
async def api_admin_login(request: LoginRequest, response: Response):
    if request.username == ADMIN_USER and request.password == ADMIN_PASS:
        # ฝังคุกกี้สิทธิ์ Admin ให้เบราว์เซอร์
        response.set_cookie(key="admin_session", value=ADMIN_SESSION_TOKEN, path="/", httponly=True)
        return {"status": "success", "message": "เข้าสู่ระบบสำเร็จ"}
    raise HTTPException(status_code=400, detail="Username หรือ Password ผิดพลาด")

# 🔒 ป้องกันหน้าเว็บ /admin (ถ้าไม่ได้ล็อกอิน จะดีดไปหน้า /login อัตโนมัติ)
@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_dashboard(admin_session: str = Cookie(None)):
    if not admin_session or admin_session != ADMIN_SESSION_TOKEN:
        return RedirectResponse(url="/login")
    try:
        with open("web/admin.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>⚠️ System Error</h1><p>ไม่พบไฟล์ admin.html</p>"

# ==========================================
# API สำหรับระบบหลังบ้าน (ติดระบบป้องกันทุกลิงก์ 🔒)
# ==========================================
@app.get("/api/admin/logs")
async def get_all_chat_logs(auth=Depends(verify_admin_auth)):
    formatted_logs = {}
    for session_id, messages in memory_backend.db.items():
        formatted_logs[session_id] = [
            {"role": "user" if msg.type == "human" else "bot", "text": msg.content} 
            for msg in messages
        ]
    return {"total_sessions": len(formatted_logs), "data": formatted_logs}

@app.post("/api/admin/reload-docs")
async def api_reload_documents(auth=Depends(verify_admin_auth)):
    try:
        doc_count = reload_knowledge()
        return {"status": "success", "message": f"น้องแอนเรียนรู้ข้อมูลใหม่สำเร็จแล้วค่า! (จำนวน {doc_count} ชุดข้อมูล)"}
    except Exception as e:
        return {"status": "error", "message": f"เกิดข้อผิดพลาด: {str(e)}"}

@app.get("/api/admin/debug-rag")
async def api_debug_rag(auth=Depends(verify_admin_auth)):
    try:
        return get_vectorstore_debug_data()
    except Exception as e:
        return {"status": "error", "message": f"เกิดข้อผิดพลาด: {str(e)}"}

@app.get("/api/admin/prompt")
async def api_get_prompt(auth=Depends(verify_admin_auth)):
    try:
        return {"status": "success", "prompt": get_system_prompt()}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/admin/prompt")
async def api_update_prompt(request: PromptRequest, auth=Depends(verify_admin_auth)):
    try:
        update_system_prompt(request.prompt)
        return {"status": "success", "message": "อัปเดต System Prompt สำเร็จ! ระบบได้เปลี่ยนบุคลิกเรียบร้อยแล้ว"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 🔒 เปิดหน้าเว็บสำหรับอัปโหลดไฟล์ (ต้องล็อกอินก่อนเท่านั้น)
@app.get("/admin/upload", response_class=HTMLResponse)
async def serve_admin_upload(admin_session: str = Cookie(None)):
    if not admin_session or admin_session != ADMIN_SESSION_TOKEN:
        return RedirectResponse(url="/login")
    try:
        with open("web/upload.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>⚠️ System Error</h1><p>ไม่พบไฟล์ upload.html ในโฟลเดอร์ web/</p>"

# 🔒 API สำหรับรับไฟล์จากหน้าเว็บแล้วบันทึกลงโฟลเดอร์ info
@app.post("/api/admin/upload")
async def api_admin_upload(file: UploadFile = File(...), auth=Depends(verify_admin_auth)):
    try:
        info_path = "./info"
        if not os.path.exists(info_path):
            os.makedirs(info_path)

        # ป้องกันช่องโหว่ Path Traversal โดยเอาเฉพาะชื่อไฟล์ฐานจริง
        filename = os.path.basename(file.filename)
        file_path = os.path.join(info_path, filename)

        # 📌 [แก้ปัญหา RAM] สตรีมข้อมูลเขียนลงไฟล์ทีละส่วนด้วย shutil
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return {"status": "success", "message": f"อัปโหลดไฟล์ {filename} ไปที่คลังข้อมูลสำเร็จแล้วค่า! 🎉"}
    except Exception as e:
        return {"status": "error", "message": f"เกิดข้อผิดพลาดระหว่างบันทึกไฟล์: {str(e)}"}
