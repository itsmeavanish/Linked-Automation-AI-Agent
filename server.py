import os
import json
import sqlite3
import uuid
import time
import re
import random
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks
from openai import OpenAI
from dotenv import load_dotenv
import threading
# --- ADVANCED LOGIC IMPORTS ---
import opik
from opik import opik_context 
from playwright.sync_api import sync_playwright

load_dotenv()
app = FastAPI(title="Autonomous LinkedIn Agent V3")

# --- CONFIGURATION ---
USER_DATA_DIR = "./linkedin_session"
DB_NAME = "commenter_state.db"

class PostRequest(BaseModel):
    post_url: str
    author: str
    content: str

# --- DATABASE HANDLER ---
class DBHandler:
    def __init__(self, db_name=DB_NAME):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                post_url TEXT,
                author TEXT,
                content TEXT,
                status TEXT,
                selected_comment TEXT,
                opik_trace_id TEXT
            )
        """)
        self.conn.commit()

    def save_final_post(self, p_id, url, author, content, status, selected, trace_id):
        self.conn.execute("""
            INSERT OR REPLACE INTO posts 
            (post_id, post_url, author, content, status, selected_comment, opik_trace_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (p_id, url, author, content, status, selected, trace_id))
        self.conn.commit()

db = DBHandler()
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY"))

# --- AUTOMATIC POSTING LOGIC ---


# Create a "Gatekeeper" lock
browser_lock = threading.Lock()

import random # Ensure this is at the top of your file

def auto_post_to_linkedin(url, text):
    """Uses a lock and human-like behavior to avoid 'could not post' errors."""
    with browser_lock:
        print(f"[*] Humanized Posting Started for: {url}")
        with sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=USER_DATA_DIR,
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                page = context.new_page()
                # 1. Realistic Loading
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(random.uniform(4.0, 7.0)) # Random wait
                
                # 2. SCROLL to the comment section (Very Important for Human Detection)
                print("[-] Scrolling to find comment box...")
                page.mouse.wheel(0, random.randint(400, 800)) 
                time.sleep(2)

                # 3. Open Box
                comment_btn = page.locator("button[aria-label='Comment']").first
                comment_btn.click(timeout=10000)
                time.sleep(random.uniform(1.5, 3.0))
                
                # 4. Human-Like Typing
                print("[-] Typing comment with random delays...")
                editor = page.locator(".ql-editor")
                editor.click()
                
                # Typing character by character with tiny random pauses
                for char in text:
                    page.keyboard.type(char)
                    time.sleep(random.uniform(0.05, 0.15)) # Mimics real fingers
                
                time.sleep(random.uniform(2.0, 4.0)) # Wait before clicking post
                
                # 5. Click Post/Comment
                submit = page.locator("button.artdeco-button--primary").filter(has_text=re.compile(r"Post|Comment", re.I))
                
                if submit.is_enabled():
                    submit.click()
                    print(f"[SUCCESS] Comment submitted for {url}")
                    # Keep browser open for a few seconds to let the request finish
                    time.sleep(5) 
                else:
                    print("[ERROR] Post button was disabled. Content might be flagged.")

                context.close()
            except Exception as e:
                print(f"[ERROR] Automation failed: {e}")

# --- AUTONOMOUS WORKER (The Core Logic) ---
@opik.track
def autonomous_worker(post: PostRequest):
    """Generates a comment (with fallback) and posts it."""
    print(f"\n[*] Processing post by {post.author}...")
    
    prompt = f"POST CONTENT: {post.content}\nTASK: Generate 1 perfect LinkedIn comment. Return JSON: {{'comment': '...'}}"
    models = ["google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.3-70b-instruct:free"]
    
    selected_comment = None
    
    # 1. AI Generation Loop
    for model_id in models:
        try:
            print(f"[*] Trying AI model: {model_id}")
            response = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            data = json.loads(response.choices[0].message.content)
            selected_comment = data.get("comment")
            if selected_comment: break
        except Exception:
            continue

    # 2. Fallback System
    if not selected_comment:
        print("[WARN] AI failed. Using fallback.")
        fallbacks = ["Great points! Thanks for sharing this.", "Excellent insight into the industry."]
        selected_comment = random.choice(fallbacks)

    # 3. Automation Call
    auto_post_to_linkedin(post.post_url, selected_comment)

    # 4. Save to Database
    trace_id = None
    try: trace_id = opik_context.get_current_trace_data().id
    except: pass
    
    db.save_final_post(f"autoli_{uuid.uuid4().hex[:8]}", post.post_url, post.author, post.content, "posted", selected_comment, trace_id)

# --- ENDPOINTS ---
@app.post("/ingest")
def ingest(post: PostRequest, background_tasks: BackgroundTasks):
    """n8n hits this. It returns 200 OK immediately and works in the background."""
    # We use add_task, but because autonomous_worker is a regular 'def' (not async def),
    # FastAPI will automatically run it in a separate thread.
    background_tasks.add_task(autonomous_worker, post)
    return {"status": "Task Ingested", "author": post.author}

@app.get("/")
def health():
    return {"status": "Agent Online"}