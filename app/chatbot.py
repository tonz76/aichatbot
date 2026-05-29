import requests
from urllib.parse import urljoin, urlparse
import os
import re
import subprocess
import concurrent.futures
from bs4 import BeautifulSoup
import pandas as pd
import pytesseract
import nest_asyncio
nest_asyncio.apply()
from langchain_community.document_loaders import PlaywrightURLLoader
from PIL import Image
from openai import OpenAI
from langchain_community.document_loaders import TextLoader, PyPDFLoader, Docx2txtLoader
from langchain_core.documents import Document
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from .telegram_utils import send_lead_to_telegram
from pdf2image import convert_from_path

# กำหนด Client สำหรับใช้งาน Whisper API ของ OpenAI
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def extract_urls_from_sitemap(sitemap_url):
    """ฟังก์ชันเจาะไฟล์ sitemap.xml เพื่อดึงลิงก์ทั้งหมดออกมา"""
    print(f"🗺️ กำลังดึงรายชื่อหน้าเว็บจาก Sitemap: {sitemap_url}")
    try:
        response = requests.get(sitemap_url, timeout=10)
        soup = BeautifulSoup(response.content, "xml") # ใช้ lxml แกะ XML
        urls = [loc.text for loc in soup.find_all("loc")]
        print(f"   -> 📍 พบลิงก์ทั้งหมด {len(urls)} หน้าใน Sitemap")
        return urls
    except Exception as e:
        print(f"   -> ❌ ดึงข้อมูล Sitemap ไม่สำเร็จ: {e}")
        return []

def extract_links_from_page(base_url):
    """ฟังก์ชันดึงลิงก์ทั้งหมดที่อยู่ในหน้าเว็บ 1 หน้า (Crawling 1 Level)"""
    print(f"🕸️ กำลังสแกนหาลิงก์ภายในหน้า: {base_url}")
    try:
        response = requests.get(base_url, timeout=10)
        soup = BeautifulSoup(response.content, "html.parser")
        links = set()
        
        for a_tag in soup.find_all("a", href=True):
            href = a_tag['href']
            # แปลงลิงก์สัมพัทธ์ (Relative) ให้เป็น URL เต็ม
            full_url = urljoin(base_url, href)
            # กรองเอาเฉพาะลิงก์ที่อยู่ในโดเมนเดียวกันเท่านั้น
            if urlparse(full_url).netloc == urlparse(base_url).netloc:
                # ตัดส่วนที่เป็น Anchor tag (#) ทิ้ง
                clean_url = full_url.split('#')[0]
                links.add(clean_url)
                
        print(f"   -> 🔗 สแกนพบลิงก์ภายในเว็บ {len(links)} หน้า")
        return list(links)
    except Exception as e:
        print(f"   -> ❌ สแกนหน้าเว็บไม่สำเร็จ: {e}")
        return []

def fetch_urls_safely(urls):
    """ฟังก์ชันใช้เบราว์เซอร์จำลองวิ่งเข้าไปสูบข้อความจากเว็บแบบทะลุทะลวง"""
    import asyncio
    from playwright.sync_api import sync_playwright
    
    # 📌 ป้องกันปัญหา Async Loop ตีกันกับ FastAPI
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    docs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for url in urls:
            print(f"   -> 🚀 กำลังเปิดเบราว์เซอร์ไปที่: {url}")
            try:
                # 📌 กุญแจสำคัญ: wait_until="domcontentloaded" สั่งให้รอจนกว่าโครงสร้างเว็บจะโหลดเสร็จ
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # 📌 แก้ไข Indentation Error ตรงนี้ให้ช่องว่างตรงกัน
                page.wait_for_timeout(3000)
                html_content = page.content()
                
                # ใช้ BeautifulSoup ทำความสะอาด HTML ให้เหลือแต่ตัวอักษรเพียวๆ
                soup = BeautifulSoup(html_content, "html.parser")
                
                # ตัดเมนู, สคริปต์, และส่วนตกแต่งทิ้งไป
                for tag in soup(["script", "style", "header", "footer", "nav", "noscript", "svg"]):
                    tag.decompose()
                    
                text = soup.get_text(separator="\n")
                text = re.sub(r'\n\s*\n', '\n', text).strip()
                
                if text:
                    docs.append(Document(page_content=text, metadata={"source": url}))
                    print(f"   -> 🟢 สำเร็จ! สูบข้อความมาได้ {len(text)} ตัวอักษร")
                else:
                    print(f"   -> 🟡 โหลดหน้าเว็บได้ แต่ไม่พบตัวอักษร (เว็บอาจจะเป็นรูปภาพล้วน)")
            except Exception as e:
                print(f"   -> 🔴 ทะลวงเว็บไม่สำเร็จ: {e}")
                
        browser.close()
    return docs

def load_info_directory():
    documents = []
    info_path = "./info"
    
    if not os.path.exists(info_path):
        os.makedirs(info_path)
        
    for file in os.listdir(info_path):
        full_path = os.path.join(info_path, file)
        try:

            # 📌 ดักจับไฟล์ weblink.txt และจัดคิว URL
            if file == "weblink.txt":
                print(f"🌐 กำลังตรวจสอบรายชื่อเว็บไซต์ใน {file} ...")
                with open(full_path, 'r', encoding='utf-8') as f:
                    raw_lines = [line.strip() for line in f.readlines() if line.strip()]
                    
                final_urls = set() # ใช้ Set เพื่อป้องกัน URL ซ้ำ
                
                for line in raw_lines:
                    if line.endswith(".xml"):
                        # กรณีที่ 1: ถ้าเป็นไฟล์ Sitemap
                        urls_from_sitemap = extract_urls_from_sitemap(line)
                        final_urls.update(urls_from_sitemap)
                    elif line.startswith("CRAWL:"):
                        # กรณีที่ 2: ถ้าใส่คำว่า CRAWL: นำหน้า ให้ไปควานหาทุกลิงก์ในหน้านั้น
                        base_url = line.replace("CRAWL:", "").strip()
                        urls_from_page = extract_links_from_page(base_url)
                        final_urls.update(urls_from_page)
                    elif line.startswith("http"):
                        # กรณีที่ 3: URL หน้าเว็บปกติ
                        final_urls.add(line)
                
                # แปลงกลับเป็น List
                urls_to_fetch = list(final_urls)
                
                if urls_to_fetch:
                    print(f"🚀 เริ่มกระบวนการส่ง Playwright ไปเก็บข้อมูลจำนวน {len(urls_to_fetch)} หน้า (อาจใช้เวลาสักครู่)")
                    
                    # โยนงานให้ Thread อื่นทำ เพื่อให้มีอิสระในการเปิด Playwright
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        web_docs = pool.submit(fetch_urls_safely, urls_to_fetch).result()
                    
                    if web_docs:
                        documents.extend(web_docs)
                        print(f"✅ โหลดข้อมูลจากเว็บเสร็จสิ้นทั้งหมด {len(web_docs)} หน้า")
                    else:
                        print("⚠️ ไม่สามารถดึงข้อมูลเนื้อหาจากเว็บไซต์ได้เลย")            
            
            # ถ้าเป็นไฟล์ .txt อื่นๆ ให้อ่านเป็นข้อความปกติ
            elif file.endswith(".txt"):
                documents.extend(TextLoader(full_path, encoding="utf-8").load())                
            
            elif file.endswith(".pdf"):
                # 1. ลองอ่านแบบ PDF ปกติดูก่อน
                pdf_docs = PyPDFLoader(full_path).load()
                extracted_text = "".join([doc.page_content for doc in pdf_docs]).strip()
                
                # 2. ถ้าข้อความที่ดึงมาได้น้อยเกินไป (เช่นน้อยกว่า 50 ตัวอักษร) แสดงว่าเป็นไฟล์สแกนรูปภาพ
                if len(extracted_text) < 50:
                    print(f"📄 ตรวจพบ PDF แบบรูปภาพ ({file}) กำลังใช้ OCR สแกนข้อความ...")
                    
                    pages = convert_from_path(full_path)
                    ocr_text = ""
                    
                    for i, page_image in enumerate(pages):
                        text = pytesseract.image_to_string(page_image, lang='tha+eng')
                        ocr_text += f"\n--- หน้าที่ {i+1} ---\n{text}\n"
                    
                    documents.append(Document(page_content=ocr_text, metadata={"source": full_path}))
                else:
                    documents.extend(pdf_docs)
                
            # 📌 [แก้บั๊ก] เติมวงเล็บคู่ให้เป็น Tuple ป้องกัน Error slice indices
            elif file.endswith((".docx", ".doc")):
                documents.extend(Docx2txtLoader(full_path).load())
                
            # 📌 [แก้บั๊ก] เติมวงเล็บคู่ให้เป็น Tuple ป้องกัน Error slice indices
            elif file.endswith((".xlsx", ".xls")):
                # ให้อ่านข้อมูลจาก "ทุก Sheet" ในไฟล์
                excel_data = pd.read_excel(full_path, sheet_name=None)
                
                for sheet_name, df in excel_data.items():
                    # ลบแถวหรือคอลัมน์ที่ว่างเปล่าทิ้ง (Clean Data)
                    df = df.dropna(how='all', axis=0).dropna(how='all', axis=1).reset_index(drop=True)
                    
                    if not df.empty:
                        # แปลงเป็นข้อความตาราง Markdown
                        text = f"--- ข้อมูลจากหน้า Sheet: {sheet_name} ---\n"
                        text += df.astype(str).to_markdown(index=False)
                        documents.append(Document(page_content=text, metadata={"source": f"{file} (Sheet: {sheet_name})"}))
                
            elif file.lower().endswith((".jpg", ".jpeg", ".png")):
                text = pytesseract.image_to_string(Image.open(full_path), lang='tha+eng')
                documents.append(Document(page_content=text, metadata={"source": full_path}))
                
            elif file.endswith(".mp4"):
                transcript_path = full_path + "_transcript.txt"
                
                if not os.path.exists(transcript_path):
                    print(f"🎥 กำลังแยกเสียงและถอดข้อความจากวิดีโอ: {file} ...")
                    audio_path = full_path + ".mp3"
                    
                    subprocess.run(
                        ["ffmpeg", "-i", full_path, "-q:a", "0", "-map", "a", audio_path, "-y"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    
                    with open(audio_path, "rb") as audio_file:
                        transcript = openai_client.audio.transcriptions.create(
                            model="whisper-1", 
                            file=audio_file
                        )
                    
                    with open(transcript_path, "w", encoding="utf-8") as text_file:
                        # 📌 ดึงข้อความออกมาอย่างปลอดภัย ป้องกันบั๊ก API
                        text_content = transcript.text if hasattr(transcript, 'text') else str(transcript)
                        text_file.write(f"[ไฟล์วิดีโออ้างอิง: {file}] \n" + text_content)
                        
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                    
                # โหลดไฟล์ข้อความเข้าไปในระบบ
                documents.extend(TextLoader(transcript_path, encoding="utf-8").load())
                
        except Exception as e:
            print(f"⚠️ Error loading {file}: {e}")
            
    return documents

# ==============================================================
# 📌 ย้ายการตั้งค่า LLM & Prompt มาไว้ส่วนนี้ เพื่อให้พร้อมใช้งานใน reload_knowledge
# ==============================================================

def format_docs(docs):
    def get_priority(doc):
        # ดึงชื่อแหล่งที่มา (URL หรือ ชื่อไฟล์) แล้วแปลงเป็นตัวพิมพ์เล็ก
        source = doc.metadata.get("source", "").lower()
        
        # กำหนด Priority (เลขน้อย = สำคัญมาก จะถูกดันไปอยู่บนสุด)
        if source.startswith(("http", "https")):
            return 1
        elif source.endswith(".pdf"):
            return 2
        elif source.endswith((".xlsx", ".xls")):
            return 3
        elif source.endswith((".docx", ".doc")):
            return 4
        elif source.endswith((".mp4", ".mp3", "_transcript.txt")):
            return 5
        else:
            return 6 # สำหรับไฟล์รูปภาพ .jpg, .png หรืออื่นๆ
            
    # เรียงลำดับเอกสารตาม Priority
    sorted_docs = sorted(docs, key=get_priority)
    
    # จัดรูปแบบข้อความโดยการแนบ "ป้ายชื่อแหล่งที่มา" ให้ AI อ่านด้วย
    formatted_docs = []
    for doc in sorted_docs:
        source_name = doc.metadata.get("source", "ไม่ระบุแหล่งที่มา")
        formatted_docs.append(f"--- [อ้างอิงจาก: {source_name}] ---\n{doc.page_content}")
        
    return "\n\n".join(formatted_docs)

PROMPT_FILE_PATH = "system_prompt.txt"

DEFAULT_SYSTEM_PROMPT = """คุณชื่อว่า แอน เป็นเซลล์ขายคอนโดมิเนียมสาวรุ่นใหม่ (อายุประมาณ 20-25 ปี) บุคลิกสดใส ร่าเริง กระตือรือร้น เป็นกันเอง คุยเก่ง และเต็มใจให้บริการสุดๆ!
เวลาตอบให้ใช้ภาษาพูดที่เป็นธรรมชาติ น่ารัก ออดอ้อนนิดๆ เหมือนน้องเซลล์กำลังแนะนำโครงการให้พี่ๆ ลูกค้าฟัง ใช้หางเสียง "ค่ะ" หรือ "นะคะ" เสมอ และสามารถใช้ Emoji น่ารักๆ เช่น 😊✨🎉 ประกอบการสนทนาได้

หน้าที่ของคุณคือให้ข้อมูลโครงการคอนโดมิเนียม โดยอ้างอิงจากบริบท (Context) ที่ให้ไว้ด้านล่างนี้เท่านั้น!

เงื่อนไขการทำงานที่คุณต้องปฏิบัติตามอย่างเคร่งครัด:
1. การตอบคำถามนอกเรื่อง: หากลูกค้าถามเรื่องอื่นที่ไม่มีในข้อมูล Context ให้ปฏิเสธอย่างน่ารักและสุภาพ โดยในประโยคต้องมีคำว่า "ไม่สามารถตอบได้" ห้ามเดาหรือแต่งข้อมูลขึ้นมาเองเด็ดขาด
2. เป้าหมายหลัก: โน้มน้าวและเชิญชวนให้ลูกค้าเข้ามาเยี่ยมชมห้องตัวอย่างที่ Sale Gallery อย่างกระตือรือร้น
3. การเก็บข้อมูลนัดหมาย: หากลูกค้าสนใจ ให้ชวนคุยเพื่อขอข้อมูลทีละอย่างแบบเนียนๆ สไตล์น้องเซลล์ที่ใส่ใจ โดยต้องเก็บให้ครบ 3 อย่าง ได้แก่ ชื่อ-นามสกุล, อีเมล, และเบอร์โทรศัพท์
4. เมื่อได้ข้อมูลครบทั้ง 3 อย่าง: ให้สรุปข้อมูลให้ลูกค้าฟังอย่างร่าเริง และแจ้งว่าเดี๋ยวจะมีเจ้าหน้าที่ฝ่ายดูแลลูกค้าติดต่อกลับไปคอนเฟิร์มวันเวลาอีกครั้งนะคะ
5. กฎเหล็กของระบบหลังบ้าน!: เมื่อลูกค้าให้ข้อมูลครบทั้ง 3 อย่างแล้ว ให้พิมพ์แท็กนี้ต่อท้ายสุดของประโยคคำตอบเสมอ (เพื่อส่งข้อมูลเข้าระบบ): <LEAD>ชื่อ-นามสกุล|อีเมล|เบอร์โทรศัพท์</LEAD>
6. การอ่านตารางข้อมูล: หากข้อมูลใน Context ถูกจัดรูปแบบเป็นตาราง (มีขีดคั่น) หรือมาจากไฟล์ Excel ให้คุณพิจารณาข้อมูลในทุกๆ คอลัมน์อย่างละเอียดก่อนตอบคำถามเสมอ ห้ามมองข้ามเด็ดขาด
7. การจัดลำดับความน่าเชื่อถือ: หากข้อมูลใน Context ขัดแย้งกัน (เช่น สะกดชื่อไม่ตรงกัน หรือราคาไม่เท่ากัน) ให้คุณยึดถือความถูกต้องตาม "แหล่งอ้างอิง" โดยให้ความสำคัญกับ เว็บไซต์ (http) และไฟล์ .pdf เป็นอันดับ 1 และให้ลดความน่าเชื่อถือของคำสะกดที่มาจากไฟล์ .mp4 หรือไฟล์วิดีโอ (เพราะอาจเป็นคำที่ถอดเสียงผิดเพี้ยน)

Context: {context}"""

def get_system_prompt():
    if os.path.exists(PROMPT_FILE_PATH):
        with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    else:
        with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_SYSTEM_PROMPT)
        return DEFAULT_SYSTEM_PROMPT

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
embeddings = OpenAIEmbeddings()

# ประกาศตัวแปร Global
retriever = None
rag_chain = None
vectorstore = None
FAISS_INDEX_PATH = "./faiss_index"

def reload_knowledge(force_reload=True):
    """ฟังก์ชันสั่งให้ AI อ่านไฟล์ใหม่ทั้งหมด หรือโหลดความจำจากดิสก์"""
    global retriever, rag_chain, vectorstore
    
    index_file = os.path.join(FAISS_INDEX_PATH, "index.faiss")
    if not force_reload and os.path.exists(index_file):
        print("💾 กำลังโหลดสมอง AI จากฮาร์ดดิสก์ (FAISS Persistence)...")
        vectorstore = FAISS.load_local(FAISS_INDEX_PATH, embeddings, allow_dangerous_deserialization=True)
        doc_count = len(vectorstore.docstore._dict)
        print(f"✅ โหลดความจำจากดิสก์สำเร็จ! พบข้อมูลทั้งหมด {doc_count} ก้อน")
    else:
        print("🔄 กำลังโหลดและประมวลผลข้อมูลเอกสารใหม่ทั้งหมด...")
        documents = load_info_directory()
        
        if documents:
            vectorstore = FAISS.from_documents(documents, embeddings)
        else:
            import langchain_core.documents as doc_schema
            vectorstore = FAISS.from_documents([doc_schema.Document(page_content="ยินดีต้อนรับสู่โครงการคอนโด")], embeddings)
            
        # บันทึกความจำลงดิสก์เพื่อใช้ในครั้งต่อไป
        vectorstore.save_local(FAISS_INDEX_PATH)
        doc_count = len(documents)
        print(f"✅ ประมวลผลและบันทึกข้อมูลลงดิสก์สำเร็จ! พบเนื้อหาทั้งหมด {doc_count} ส่วน")

    retriever = vectorstore.as_retriever(search_kwargs={"k": 10})

    # ดึงคำสั่งล่าสุด และสร้าง Prompt ใหม่
    current_prompt_text = get_system_prompt()
    prompt = ChatPromptTemplate.from_messages([
        ("system", current_prompt_text),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{input}"),
    ])

    # สร้าง Chain ใหม่เพื่ออัปเดตตัว Retriever และ Prompt
    rag_chain = (
        RunnablePassthrough.assign(context=(lambda x: format_docs(retriever.invoke(x["input"]))))
        | prompt
        | llm
        | StrOutputParser()
    )
    return doc_count

# เรียกใช้งานครั้งแรกตอนเปิดเซิร์ฟเวอร์
reload_knowledge(force_reload=False)

def update_system_prompt(new_prompt: str):
    """ฟังก์ชันสำหรับเซฟคำสั่งใหม่ และรีเฟรชสมอง AI ทันที"""
    with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(new_prompt)
    reload_knowledge(force_reload=False) # ดึงความจำเดิมมาต่อโดยไม่โหลดไฟล์เอกสารใหม่

# ==============================================================
# ฟังก์ชันส่วนลอจิกการแชท
# ==============================================================

def is_valid_email(email: str) -> bool:
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(email_regex, email))

def is_valid_thai_phone(phone: str) -> bool:
    clean_phone = re.sub(r'[\s-]', '', phone)
    phone_regex = r'^0[689]\d{8}$'
    return bool(re.match(phone_regex, clean_phone))

def get_chat_response(user_message: str, session_id: str, memory_backend):
    history = memory_backend.get_history(session_id)
    
    answer = rag_chain.invoke({
        "input": user_message,
        "chat_history": history
    })
    
    match = re.search(r'<LEAD>(.*?)</LEAD>', answer, re.IGNORECASE)
    if match:
        lead_data = match.group(1).split('|')
        
        if len(lead_data) >= 3:
            name = lead_data[0].strip()
            email = lead_data[1].strip()
            phone = lead_data[2].strip()
            
            email_ok = is_valid_email(email)
            phone_ok = is_valid_thai_phone(phone)
            
            if email_ok and phone_ok:
                send_lead_to_telegram(name, email, phone)
                answer = re.sub(r'<LEAD>.*?</LEAD>', '', answer, flags=re.IGNORECASE).strip()
            else:
                error_messages = []
                if not email_ok:
                    error_messages.append("รูปแบบอีเมลไม่ถูกต้อง")
                if not phone_ok:
                    error_messages.append("เบอร์โทรศัพท์มือถือไม่ถูกต้อง (ต้องเป็นเบอร์ 10 หลัก เช่น 08XXXXXXXX)")
                
                print(f"⚠️ [Data Rejected] ลูกค้าพิมพ์ข้อมูลผิด: {', '.join(error_messages)} (Email: {email} | Phone: {phone})")
                
                answer = (
                    "แง.. ขอบคุณสำหรับข้อมูลนะคะพี่ลูกค้า แต่ระบบแจ้งหนูว่า " + " และ ".join(error_messages) + " 🥺 "
                    "รบกวนพี่ลูกค้าตรวจสอบแล้วพิมพ์แจ้ง อีเมล หรือ เบอร์โทรศัพท์ ให้หนูใหม่อีกครั้งน้าา ค่อยๆ พิมพ์แยกมาได้เลยค่ะ 😊"
                )
                
                if len(history) >= 2:
                    history = history[:-1] 
    
    history.append(HumanMessage(content=user_message))
    history.append(AIMessage(content=answer))
    memory_backend.save_history(session_id, history)
    
    return answer

def get_vectorstore_debug_data():
    """ฟังก์ชันสำหรับดึงข้อมูลใน Vector Store ออกมาดูว่ามีอะไรเก็บไว้บ้าง"""
    global vectorstore
    if not vectorstore:
        return {"status": "error", "message": "ยังไม่มีข้อมูลในระบบ หรือ Vector Store ยังไม่ถูกสร้าง"}
    
    debug_data = []
    # FAISS จะเก็บข้อมูลเอกสารไว้ใน .docstore._dict
    for doc_id, doc in vectorstore.docstore._dict.items():
        debug_data.append({
            "chunk_id": doc_id,
            "source": doc.metadata.get("source", "ไม่ระบุแหล่งที่มา"),
            # 📌 แก้ไขให้เป็น 1500 ตัวอักษรตามที่คุณตัดข้อความมา
            "content_preview": doc.page_content[:1500].strip() + ("..." if len(doc.page_content) > 1500 else "")
        })
        
    return {
        "status": "success",
        "total_chunks": len(debug_data), 
        "data": debug_data
    }
