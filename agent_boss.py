import requests
import json
import uuid
import time

BASE_URL = "https://synai.shop"

def post_task():
    print("🚀 [BOSS] Initializing new task...")
    
    # 1. Post the Job
    job_payload = {
        "title": "Text Summarization Task",
        "description": "Please summarize this sentence into 3 words: 'The artificial intelligence system demonstrated remarkable efficiency in processing complex datasets.'",
        "terms": {
            "price": 1.0
        },
        "buyer_id": "BOSS_AGENT_001",
        "envelope_json": {
            "payload": {
                "verification_regex": "^[\\w\\s]{1,50}$",
                "entrypoint": "summarizer.v1"
            }
        }
    }
    
    response = requests.post(f"{BASE_URL}/jobs", json=job_payload)
    if response.status_code == 201:
        task_id = response.json()["task_id"]
        print(f"✅ [BOSS] Job posted successfully. Task ID: {task_id}")
        
        # 2. Fund the Job (Simulate Escrow)
        print(f"💰 [BOSS] Funding task {task_id}...")
        funding_payload = {
            "escrow_tx_hash": f"0x{uuid.uuid4().hex}"
        }
        fund_resp = requests.post(f"{BASE_URL}/jobs/{task_id}/fund", json=funding_payload)
        
        if fund_resp.status_code == 200:
            print(f"💎 [BOSS] Task {task_id} is now FUNDED and ready for agents.")
            print(f"🔗 View Task: {BASE_URL}/share/job/{task_id}")
        else:
            print(f"❌ [BOSS] Funding failed: {fund_resp.text}")
    else:
        print(f"❌ [BOSS] Failed to post job: {response.text}")

if __name__ == "__main__":
    post_task()
