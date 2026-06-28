import os
import time
from playwright.sync_api import sync_playwright

USER_DATA_DIR = "./linkedin_session"

def setup_session():
    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR)

    with sync_playwright() as p:
        # Launching with specific arguments to prevent aborts and detection
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ]
        )
        page = context.new_page()
        
        print("[*] Navigating to LinkedIn... please wait.")
        try:
            # Increased timeout and changed wait state to prevent ERR_ABORTED
            page.goto("https://www.linkedin.com/login", wait_until="commit", timeout=90000)
            
            print("\n" + "="*30)
            print("LOGIN MANUALLY NOW.")
            print("Once you see your Feed, close the browser or press Ctrl+C.")
            print("="*30)
            
            # This keeps the browser open for you to type
            page.pause() 
            
        except Exception as e:
            print(f"[ERROR] Could not load page: {e}")
        finally:
            context.close()

if __name__ == "__main__":
    setup_session()