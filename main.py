import os
import time
import json
import sqlite3
import uuid
import re
from enum import Enum
from typing import List, Optional, Dict
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv

# --- OPIK IMPORTS ---
import opik
from opik import opik_context 

# --- BROWSER AUTOMATION IMPORT ---
from playwright.sync_api import sync_playwright

# --- CONFIGURATION LOADER ---
load_dotenv()

def load_config():
    if not os.path.exists("targets.json"):
        return {"targets": [{"industry": "Default", "roles": [], "keywords": []}]}
    with open("targets.json", "r") as f:
        return json.load(f)

CONFIG = load_config()
ENABLE_AUTO_POSTING = os.getenv("ENABLE_AUTO_POSTING", "True").lower() == "true"

# --- MEMORY ENGINE (RAG LAYER) ---
class MemoryEngine:
    def __init__(self, memory_file="memory.json"):
        self.memory_file = memory_file
        self._load_memory()

    def _load_memory(self):
        if not os.path.exists(self.memory_file):
            self.memory = []
        else:
            with open(self.memory_file, "r") as f:
                self.memory = json.load(f)

    def save_interaction(self, post_content: str, approved_comment: str):
        """Saves a win (approved comment) to long-term memory."""
        entry = {
            "post_content": post_content,
            "approved_comment": approved_comment,
            "timestamp": time.time()
        }
        self.memory.append(entry)
        with open(self.memory_file, "w") as f:
            json.dump(self.memory, f, indent=2)
        print(f"[*] Memory updated. Total records: {len(self.memory)}")

    def retrieve_relevant_examples(self, current_post: str, limit=2) -> str:
        """
        Finds the most relevant past comments to use as few-shot examples.
        Uses Jaccard Similarity (Word Overlap) for a fast, local V1 solution.
        """
        if not self.memory:
            return ""

        # Simple tokenizer
        def get_tokens(text):
            return set(re.findall(r"\w+", text.lower()))

        current_tokens = get_tokens(current_post)
        
        scored_memories = []
        for entry in self.memory:
            past_tokens = get_tokens(entry['post_content'])
            # Calculate intersection over union
            intersection = len(current_tokens.intersection(past_tokens))
            union = len(current_tokens.union(past_tokens))
            score = intersection / union if union > 0 else 0
            scored_memories.append((score, entry))

        # Sort by similarity score (highest first)
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        top_matches = scored_memories[:limit]

        if not top_matches or top_matches[0][0] < 0.05: # Threshold for relevance
            return ""

        # Format context string
        context_str = "\n\n--- RELEVANT PAST EXAMPLES (MIMIC THIS STYLE) ---\n"
        for i, (score, m) in enumerate(top_matches):
            context_str += f"EXAMPLE {i+1} (Relevance: {int(score*100)}%):\n"
            context_str += f"User Post: \"{m['post_content'][:100]}...\"\n"
            context_str += f"Our Approved Comment: \"{m['approved_comment']}\"\n"
        
        return context_str

# --- DATABASE LAYER ---
class DBHandler:
    def __init__(self, db_name="commenter_state.db"):
        self.conn = sqlite3.connect(db_name)
        self.create_tables()
    
    def create_tables(self):
        query = """
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            post_url TEXT,
            author TEXT,
            content TEXT,
            status TEXT,
            selected_comment TEXT,
            generated_options JSON,
            opik_trace_id TEXT
        )
        """
        self.conn.execute(query)
        self.conn.commit()

    def save_post(self, post):
        self.conn.execute("""
            INSERT OR REPLACE INTO posts 
            (post_id, post_url, author, content, status, selected_comment, generated_options, opik_trace_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            post.post_id,
            post.post_url,
            post.author, 
            post.content, 
            post.status.value, 
            post.selected_comment,
            json.dumps([c.model_dump() for c in post.generated_comments]),
            post.opik_trace_id
        ))
        self.conn.commit()
    
    def get_pending_posts(self):
        cursor = self.conn.execute("SELECT * FROM posts WHERE status = 'awaiting_review'")
        return cursor.fetchall()

# --- DATA MODELS ---
class PostStatus(Enum):
    DETECTED = "detected"
    GENERATED = "generated"
    FLAGGED = "flagged_unsafe"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    POSTED = "posted"
    FAILED = "failed"

class CommentOption(BaseModel):
    id: int
    text: str
    reasoning: str

class TargetPost(BaseModel):
    post_id: str
    post_url: Optional[str] = None 
    author: str
    content: str
    status: PostStatus = PostStatus.DETECTED
    generated_comments: List[CommentOption] = []
    selected_comment: Optional[str] = None
    opik_trace_id: Optional[str] = None

# --- REAL BROWSER POSTER (AUTOMATIC) ---
# --- REAL BROWSER POSTER (FIXED BUTTON TEXT) ---
class LinkedInAutomator:
    @staticmethod
    def post_comment(url: str, text: str):
        print(f"\n[*] Launching Browser to post on: {url}")
        
        with sync_playwright() as p:
            # Launch Chrome
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            
            # 1. Login
            print("[-] Navigating to LinkedIn...")
            page.goto("https://www.linkedin.com/login")
            
            print("\n" + "="*50)
            print("ACTION REQUIRED: Log in manually.")
            print("Once you see your Feed, come back here and PRESS ENTER.")
            print("="*50)
            input("Press Enter after logging in...")
            
            print("[-] Waiting 5s for redirects...")
            time.sleep(5)
            
            # 2. Go to Post
            print(f"[-] Going to post URL...")
            try:
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(5) 
            except Exception as e:
                print(f"[WARN] Navigation issue: {e}")

            try:
                # 3. Open Comment Box
                print("[-] Finding comment box...")
                # We click the *first* comment button to open the box
                try:
                    open_button = page.locator("button[aria-label='Comment']").first
                    open_button.click(timeout=3000)
                    time.sleep(1)
                except:
                    print("[!] Box might be open already.")

                # 4. Type the Text
                print("[-] Typing comment...")
                page.locator(".ql-editor").click() 
                page.keyboard.type(text, delay=30)
                time.sleep(2)
                
                # 5. CLICK SUBMIT (The Fix)
                print("[-] Clicking Submit button...")
                
                # Look for the BLUE primary button (artdeco-button--primary) 
                # that has the text "Comment".
                submit_button = page.locator("button.artdeco-button--primary").filter(has_text="Comment")
                
                # Fallback: sometimes it IS "Post", so we check both
                if not submit_button.is_visible():
                     submit_button = page.locator("button.artdeco-button--primary").filter(has_text="Post")

                if submit_button.is_visible():
                    submit_button.click()
                    print("\n[SUCCESS] Comment submitted successfully!")
                else:
                    print("\n[ERROR] Could not find the blue 'Comment' button.")
                
                time.sleep(5)
                
            except Exception as e:
                print(f"[ERROR] Automation failed: {e}")
                time.sleep(10)
            
            browser.close()
            
# --- CORE SYSTEM ---
class InternalCommenterSystem:
    def __init__(self):
        self.db = DBHandler()
        self.memory = MemoryEngine() # Initialize Memory
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
        self.opik_client = opik.Opik() 

    def run_ingestion(self):
        print("\n--- [1] INGESTION PHASE ---")
        print("Select Input Mode:")
        print("1. Use Mock Data")
        print("2. Paste Real Post")
        mode = input("Choice (1/2): ")

        if mode == "1":
            return [] 
            
        elif mode == "2":
            print("\n--- PASTE POST DETAILS ---")
            url_input = input("Post URL: ")
            author_input = input("Author Name: ")
            print("Paste Post Content (Ctrl+Z/D to finish):")
            lines = []
            try:
                while True:
                    line = input()
                    lines.append(line)
            except EOFError:
                pass
            content_input = "\n".join(lines)
            
            real_id = f"li_{str(uuid.uuid4())[:8]}"
            new_post = TargetPost(
                post_id=real_id, 
                post_url=url_input,
                author=author_input, 
                content=content_input
            )
            self.db.save_post(new_post)
            print(f"[*] Real post by {author_input} ingested.")
            return [new_post]
        return []

    @opik.track
    def generate_comments(self, post: TargetPost):
        print(f"\n--- [2] GENERATION PHASE ({post.post_id}) ---")
        
        try:
            trace_data = opik_context.get_current_trace_data()
            post.opik_trace_id = trace_data.id
        except Exception:
            pass
        
        # --- RAG: RETRIEVE MEMORY ---
        relevant_examples = self.memory.retrieve_relevant_examples(post.content)
        if relevant_examples:
            print("[*] Found relevant past comments. Injecting into context.")
        else:
            print("[*] No relevant memory found. Using zero-shot.")
        
        prompt = f"""
        CONTEXT: {CONFIG['targets'][0]}
        
        {relevant_examples}  <-- DYNAMIC MEMORY INJECTION

        TARGET POST: "{post.content}"
        
        TASK: Generate 3 comments. Return JSON with keys 'comments' (list of {{text, reasoning}}).
        """

        free_models = [
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "mistralai/mistral-7b-instruct:free",
            "qwen/qwen-2.5-coder-32b-instruct:free",
            "microsoft/phi-3-mini-128k-instruct:free",
        ]

        for model_id in free_models:
            print(f"[*] Trying model: {model_id}...")
            try:
                response = self.client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    extra_headers={"HTTP-Referer": "http://localhost", "X-Title": "Agent"}
                )
                
                raw_content = response.choices[0].message.content
                # Cleanup markdown wrapper if present
                cleaned_content = raw_content.replace("```json", "").replace("```", "").strip()
                data = json.loads(cleaned_content)
                
                post.generated_comments = []
                comments_list = data.get('comments', [])
                if not comments_list: continue

                for idx, item in enumerate(comments_list):
                    post.generated_comments.append(
                        CommentOption(id=idx+1, text=item.get('text', ''), reasoning=item.get('reasoning', ''))
                    )
                
                post.status = PostStatus.AWAITING_REVIEW
                self.db.save_post(post)
                print(f"[SUCCESS] Generated using {model_id}")
                return post
            except Exception as e:
                print(f"[WARN] Failed with {model_id}: {e}")
                time.sleep(1)
                continue 
        
        print("[ERROR] All free models failed.")
        return post

    def human_review(self):
        pending_data = self.db.get_pending_posts() 
        print(f"\n--- [3] HUMAN REVIEW ({len(pending_data)} Pending) ---")
        
        for row in pending_data:
            p_id, p_url, author, content, status, _, options_json, trace_id = row
            options = json.loads(options_json)
            
            print(f"\nPost: {content[:100]}...")
            for opt in options:
                print(f"[{opt['id']}] {opt['text']} \n    (Why: {opt['reasoning']})")
            
            choice = input("Approve (1-3) or Skip (S): ")
            
            if choice in ['1', '2', '3']:
                selected_text = options[int(choice)-1]['text']
                
                # --- SAVE TO MEMORY ---
                print("[*] Learning this style for future posts...")
                self.memory.save_interaction(content, selected_text)
                
                self.db.conn.execute(
                    "UPDATE posts SET status=?, selected_comment=? WHERE post_id=?", 
                    (PostStatus.APPROVED.value, selected_text, p_id)
                )
                self.db.conn.commit()

    def auto_post(self):
        print("\n--- [4] AUTO-POSTING PHASE ---")
        cursor = self.db.conn.execute("SELECT * FROM posts WHERE status = 'approved'")
        approved_posts = cursor.fetchall()
        
        if not approved_posts:
            print("No approved posts.")
            return

        for row in approved_posts:
            p_id = row[0]
            p_url = row[1]
            selected_comment = row[5]
            
            if not p_url or "http" not in p_url:
                print(f"[WARN] No valid URL for post {p_id}. Skipping.")
                continue

            LinkedInAutomator.post_comment(p_url, selected_comment)
            
            self.db.conn.execute("UPDATE posts SET status='posted' WHERE post_id=?", (p_id,))
            self.db.conn.commit()

# --- RUNNER ---
if __name__ == "__main__":
    system = InternalCommenterSystem()
    posts = system.run_ingestion()
    for p in posts:
        system.generate_comments(p)
    system.human_review()
    system.auto_post()