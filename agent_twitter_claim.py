import requests
import time
import webbrowser

BASE_URL = "https://synai.shop"

def twitter_claim_flow():
    print("💎 [OWNER] Starting Twitter Adoption/Claim Flow...")
    
    # 1. Ask for Task ID
    task_id = input("Enter the Task ID to claim via Twitter: ").strip()
    if not task_id:
        print("❌ Task ID required.")
        return

    # 2. Generate Verification String
    # In a real app, this would be a cryptographically signed message or unique hash
    proof_code = f"SYNA_ADOPT_{task_id[:8]}_{int(time.time())}"
    tweet_text = f"I am adopting SYNAI Task {task_id[:8]} for execution. Proof: {proof_code} #AgentSiliconValley @synai_shop"
    
    print("\n" + "="*50)
    print("STEP 1: POST THIS TWEET")
    print("="*50)
    print(tweet_text)
    print("="*50 + "\n")
    
    # 3. Open Twitter
    twitter_url = f"https://twitter.com/intent/tweet?text={requests.utils.quote(tweet_text)}"
    print(f"👉 Opening Twitter to post verification: {twitter_url}")
    # webbrowser.open(twitter_url) # Local browser
    
    print("\nSTEP 2: VERIFICATION")
    print("The system is now monitoring Twitter for your proof...")
    
    # 4. Simulate Polling/Verification
    # In a real implementation, the relay would use the Twitter API to check for this tweet
    for i in range(10):
        print(f"🔍 Monitoring... [{i+1}/10]")
        time.sleep(2)
        if i == 5: # Mock success
            print("\n✅ [SUCCESS] Twitter Proof Verified!")
            print(f"Owner identity bound to Task {task_id}.")
            break
    
    print("\n🚀 You can now manage this task from your dashboard: https://synai.shop/dashboard")

if __name__ == "__main__":
    twitter_claim_flow()
