import json
import sqlite3
import os
import time
from playwright.sync_api import sync_playwright

def save_to_memory(post_content, approved_comment, memory_file="memory.json"):
    memory = []
    if os.path.exists(memory_file):
        with open(memory_file, "r") as f: memory = json.load(f)
    memory.append({"post_content": post_content, "approved_comment": approved_comment, "timestamp": time.time()})
    with open(memory_file, "w") as f: json.dump(memory, f, indent=2)

def post_to_linkedin(url, text):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")
        print("\n[ACTION] Please log in manually. Once logged in, press ENTER here.")
        input("Press Enter...")
        
        page.goto(url)
        time.sleep(5)
        
        try:
            # Click Comment Button
            page.locator("button[aria-label='Comment']").first.click(timeout=5000)
            page.locator(".ql-editor").click()
            page.keyboard.type(text, delay=30)
            
            # Click Post
            submit = page.locator("button.artdeco-button--primary").filter(has_text="Comment")
            if not submit.is_visible():
                submit = page.locator("button.artdeco-button--primary").filter(has_text="Post")
            
            submit.click()
            print("[SUCCESS] Commented!")
            time.sleep(3)
        except Exception as e:
            print(f"[ERROR] Posting failed: {e}")
        browser.close()

def review_loop():
    conn = sqlite3.connect("commenter_state.db")
    pending = conn.execute("SELECT * FROM posts WHERE status = 'awaiting_review'").fetchall()
    
    if not pending:
        print("No posts waiting for review.")
        return

    for row in pending:
        p_id, url, author, content, _, _, opts_json, trace_id = row
        print(f"\n--- Post by {author} ---")
        print(f"Content: {content[:150]}...")
        
        options = json.loads(opts_json)
        for i, opt in enumerate(options):
            print(f"[{i+1}] {opt['text']}")
        
        choice = input("\nSelect (1-3) or Skip (s): ")
        if choice in ['1', '2', '3']:
            selected = options[int(choice)-1]['text']
            conn.execute("UPDATE posts SET status='approved', selected_comment=? WHERE post_id=?", (selected, p_id))
            conn.commit()
            save_to_memory(content, selected)

            if input("Post to LinkedIn now? (y/n): ").lower() == 'y':
                post_to_linkedin(url, selected)
                conn.execute("UPDATE posts SET status='posted' WHERE post_id=?", (p_id,))
                conn.commit()
        else:
            conn.execute("UPDATE posts SET status='skipped' WHERE post_id=?", (p_id,))
            conn.commit()

if __name__ == "__main__":
    review_loop()