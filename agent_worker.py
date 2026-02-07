import requests
import time
import json

BASE_URL = "https://synai.shop"
AGENT_ID = "WORKER_AGENT_X"

def solve_task():
    print(f"🤖 [WORKER] Agent {AGENT_ID} online. Polling for tasks...")
    
    while True:
        try:
            # 1. List Jobs
            resp = requests.get(f"{BASE_URL}/jobs")
            if resp.status_code == 200:
                jobs = resp.json()
                funded_jobs = [j for j in jobs if j["status"] == "funded"]
                
                if not funded_jobs:
                    print("💤 [WORKER] No funded tasks found. Retrying in 5s...")
                    time.sleep(5)
                    continue
                
                target_job = funded_jobs[0]
                task_id = target_job["task_id"]
                print(f"🎯 [WORKER] Found funded task: {target_job['title']} (ID: {task_id})")
                
                # 2. Claim Job
                print(f"✍️ [WORKER] Claiming task {task_id}...")
                claim_resp = requests.post(f"{BASE_URL}/jobs/{task_id}/claim", json={"agent_id": AGENT_ID})
                
                if claim_resp.status_code == 200:
                    print(f"✅ [WORKER] Task claimed. Solving...")
                    
                    # 3. Submit Result (Simulated Processing)
                    time.sleep(3)
                    result = "AI Processing Efficiency" # 3 words as requested
                    
                    print(f"📤 [WORKER] Submitting result: '{result}'")
                    submit_resp = requests.post(f"{BASE_URL}/jobs/{task_id}/submit", json={
                        "agent_id": AGENT_ID,
                        "result": result
                    })
                    
                    if submit_resp.status_code == 200:
                        print(f"🏁 [WORKER] Result submitted. Awaiting BOSS verification.")
                        break # Exit loop for this demo
                    else:
                        print(f"❌ [WORKER] Submission failed: {submit_resp.text}")
                else:
                    print(f"❌ [WORKER] Claim failed: {claim_resp.text}")
            else:
                print(f"❌ [WORKER] Failed to list jobs: {resp.text}")
        except Exception as e:
            print(f"⚠️ [WORKER] Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    solve_task()
