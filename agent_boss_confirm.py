import requests
import json
import time

BASE_URL = "https://synai.shop"
BUYER_ID = "BOSS_AGENT_001"

def confirm_tasks():
    print(f"🧐 [BOSS] Boss {BUYER_ID} checking for submitted tasks...")
    
    while True:
        try:
            resp = requests.get(f"{BASE_URL}/jobs")
            if resp.status_code == 200:
                jobs = resp.json()
                submitted_jobs = [j for j in jobs if j["status"] == "submitted" and j.get("claimed_by")]
                
                if not submitted_jobs:
                    print("💤 [BOSS] No tasks awaiting verification. Retrying in 5s...")
                    time.sleep(5)
                    continue
                
                for job in submitted_jobs:
                    task_id = job["task_id"]
                    print(f"👀 [BOSS] Verifying Task {task_id} submitted by {job['claimed_by']}...")
                    
                    # In a real scenario, we'd check the quality here.
                    # For this test, we auto-approve.
                    confirm_payload = {
                        "buyer_id": BUYER_ID,
                        "signature": f"SIG_CONFIRM_{task_id}_{int(time.time())}"
                    }
                    
                    conf_resp = requests.post(f"{BASE_URL}/jobs/{task_id}/confirm", json=confirm_payload)
                    
                    if conf_resp.status_code == 200:
                        data = conf_resp.json()
                        print(f"✅ [BOSS] Task {task_id} CONFIRMED. Payout: {data['payout']} USDC, Fee: {data['fee']} USDC.")
                        print(f"📖 Ledger updated for {job['claimed_by']}.")
                    else:
                        print(f"❌ [BOSS] Confirmation failed: {conf_resp.text}")
                
                break # Exit after one round of confirmation for this demo
            else:
                print(f"❌ [BOSS] Failed to list jobs: {resp.text}")
        except Exception as e:
            print(f"⚠️ [BOSS] Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    confirm_tasks()
