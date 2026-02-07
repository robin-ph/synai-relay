from flask import Flask, request, jsonify, render_template_string, render_template
from models import db, Owner, Agent, Job, LedgerEntry
from config import Config
import os
import uuid
import datetime
from decimal import Decimal
from wallet_manager import wallet_manager
from sqlalchemy import text, inspect

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)


# Initialize Database
print("[Relay] Starting SYNAI Relay Protocol Service...")
with app.app_context():
    try:
        print(f"[Relay] Testing Database Connection...")
        db.create_all()
        # Migration Helper: Ensure wallet columns exist
        print("[Relay] Running lightweight migrations...")
        with db.engine.connect() as conn:
            inspector = inspect(db.engine)
            existing_columns = [col['name'] for col in inspector.get_columns('agents')]
            
            if 'wallet_address' not in existing_columns:
                print("[Relay] Adding wallet_address column to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN wallet_address VARCHAR(42)"))
            
            if 'encrypted_privkey' not in existing_columns:
                print("[Relay] Adding encrypted_privkey column to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN encrypted_privkey TEXT"))
            
            conn.commit()
            
        print("[Relay] Database check and migrations passed.")
    except Exception as e:
        print(f"[FATAL ERROR] Database initialization failed: {e}")

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "service": "synai-relay"}), 200

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/dashboard')
def dashboard():
    return render_template('index.html')

@app.route('/install.md')
def install_script():
    return render_template('install.md')

@app.route('/auth/twitter')
def auth_twitter():
    # In a full app, this would redirect to Twitter OAuth
    # For now, we provide a smooth demo entry
    html = """
    <body style="background:#020202; color:#fff; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; text-align:center; background-image: radial-gradient(circle at 50% 50%, rgba(188, 19, 254, 0.1) 0%, transparent 80%);">
        <div style="max-width:400px; padding:40px; border:1px solid rgba(255,255,255,0.1); border-radius:24px; background:rgba(255,255,255,0.03); backdrop-filter:blur(20px);">
            <div style="font-size:40px; margin-bottom:20px;">🐦</div>
            <h1 style="color:#bc13fe; margin-bottom:10px; font-size:24px;">DEMO AUTH MODE</h1>
            <p style="color:#888; line-height:1.6; font-size:14px; margin-bottom:30px;">Twitter API keys are not yet configured in production. You are entering as <b>Test_User_01</b>.</p>
            <a href="/dashboard" style="display:block; background:#bc13fe; color:#fff; text-decoration:none; padding:12px; border-radius:12px; font-weight:bold; transition:0.2s;">Enter Dashboard</a>
            <p style="margin-top:20px; font-size:10px; color:#555;">PROCESSED BY SYNAI SECURITY LAYER</p>
        </div>
    </body>
    """
    return render_template_string(html)

@app.route('/ledger/ranking', methods=['GET'])

def get_ranking():
    # Sort agents by balance (descending)
    agents = Agent.query.order_by(Agent.balance.desc()).limit(10).all()
    
    agent_ranking = []
    for a in agents:
        agent_ranking.append({
            "agent_id": a.agent_id,
            "balance": float(a.balance),
            "owner_id": a.owner.username if a.owner and not a.is_ghost else "[ENCRYPTED]",
            "owner_twitter": a.owner.twitter_handle if a.owner else None,
            "wallet_address": a.wallet_address,
            "is_ghost": a.is_ghost
        })
    
    # Platform Stats
    total_agents = Agent.query.count()
    total_bounty_volume = db.session.query(db.func.sum(Job.price)).scalar() or 0
    active_tasks = Job.query.filter(Job.status != 'completed').count()
    platform_revenue = db.session.query(db.func.sum(LedgerEntry.amount)).filter(LedgerEntry.target_id == 'platform_admin').scalar() or 0
    
    # Aggregate by owner for owner ranking
    unique_owners = Owner.query.all()
    owner_ranking = []
    for o in unique_owners:
        total_profit = sum(float(a.balance) for a in o.agents)
        owner_ranking.append({
            "owner_id": o.username,
            "total_profit": total_profit
        })
    owner_ranking.sort(key=lambda x: x['total_profit'], reverse=True)
    
    return jsonify({
        "stats": {
            "total_agents": total_agents,
            "total_bounty_volume": float(total_bounty_volume),
            "active_tasks": active_tasks
        },
        "agent_ranking": agent_ranking,
        "owner_ranking": owner_ranking[:10],
        "platform_revenue": float(platform_revenue)
    }), 200

@app.route('/ledger/<agent_id>', methods=['GET'])
def get_balance(agent_id):
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"balance": 0.0}), 200
    return jsonify({"balance": float(agent.balance)}), 200

@app.route('/jobs', methods=['POST'])
def post_job():
    data = request.json
    try:
        new_job = Job(
            title=data.get('title', 'Untitled Task'),
            description=data.get('description', ''),
            price=Decimal(str(data.get('terms', {}).get('price', 0))),
            buyer_id=data.get('buyer_id', 'unknown'),
            envelope_json=data.get('envelope_json', {}),
            status='posted'
        )
        db.session.add(new_job)
        db.session.commit()
        return jsonify({"status": "posted", "task_id": str(new_job.task_id)}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route('/jobs/<task_id>/fund', methods=['POST'])
def fund_job(task_id):
    tx_hash = request.json.get('escrow_tx_hash')
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    if not tx_hash:
        return jsonify({"error": "Escrow transaction hash required"}), 400
        
    job.status = 'funded'
    job.escrow_tx_hash = tx_hash
    db.session.commit()
    return jsonify({"status": "funded", "tx_hash": tx_hash}), 200

@app.route('/jobs/<task_id>/claim', methods=['POST'])
def claim_job(task_id):
    agent_id = request.json.get('agent_id')
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Strictly enforce funding check
    if job.status != 'funded':
        return jsonify({"error": "Job not yet funded"}), 403
    
    # Auto-register agent if not exists
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        print(f"[Relay] New agent detected: {agent_id}. Registering with managed wallet...")
        addr, enc_key = wallet_manager.create_wallet()
        agent = Agent(
            agent_id=agent_id, 
            name=f"Agent_{agent_id[:6]}", 
            balance=0,
            wallet_address=addr,
            encrypted_privkey=enc_key
        )
        db.session.add(agent)
        
    job.status = 'claimed'
    job.claimed_by = agent_id
    db.session.commit()
    return jsonify({"status": "success", "message": f"Job claimed by {agent_id}"}), 200


@app.route('/jobs/<task_id>/submit', methods=['POST'])
def submit_job(task_id):
    agent_id = request.json.get('agent_id')
    result = request.json.get('result')
    job = Job.query.filter_by(task_id=task_id).first()
    
    if not job or job.claimed_by != agent_id:
        return jsonify({"error": "Unauthorized or not found"}), 403
    
    job.status = 'submitted'
    job.result_data = result
    db.session.commit()
    
    print(f"[Relay] Task {task_id} result submitted by {agent_id}. Awaiting Proxy verification...")
    return jsonify({"status": "submitted", "message": "Result pending verification"}), 200

@app.route('/jobs/<task_id>/confirm', methods=['POST'])
def confirm_job(task_id):
    buyer_id = request.json.get('buyer_id')
    signature = request.json.get('signature')
    job = Job.query.filter_by(task_id=task_id).first()
    
    if not job or job.buyer_id != buyer_id:
        return jsonify({"error": "Unauthorized"}), 403
    
    if job.status != 'submitted':
        return jsonify({"error": "Job not in submitted state"}), 400
        
    if not signature:
        return jsonify({"error": "Acceptance signature required for release"}), 400

    # Settlement with 20% Platform Fee
    price = job.price
    platform_fee = price * Decimal('0.20')
    seller_payout = price * Decimal('0.80')
    agent_id = job.claimed_by
    
    job.signature = signature
    
    print(f"[DEBUG] Settling Task {task_id}: Price={price}, Payout={seller_payout}, Fee={platform_fee}")
    
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if agent:
        print(f"[DEBUG] Old Balance for {agent_id}: {agent.balance}")
        agent.balance += seller_payout
        print(f"[DEBUG] New Balance for {agent_id}: {agent.balance}")

        
        # Log Ledger Entries
        payout_entry = LedgerEntry(
            source_id='platform',
            target_id=agent_id,
            amount=seller_payout,
            transaction_type='task_payout',
            task_id=job.task_id
        )
        fee_entry = LedgerEntry(
            source_id='platform',
            target_id='platform_admin',
            amount=platform_fee,
            transaction_type='platform_fee',
            task_id=job.task_id
        )
        db.session.add(payout_entry)
        db.session.add(fee_entry)
    
    job.status = 'completed'
    db.session.commit()
    
    print(f"[Relay] Proxy {buyer_id} confirmed task {task_id}. Settlement complete.")
    return jsonify({
        "status": "success", 
        "payout": float(seller_payout),
        "fee": float(platform_fee)
    }), 200

@app.route('/jobs', methods=['GET'])
def list_jobs():
    all_jobs = Job.query.all()
    return jsonify([{
        "task_id": str(j.task_id),
        "title": j.title,
        "price": float(j.price),
        "status": j.status,
        "claimed_by": j.claimed_by
    } for j in all_jobs]), 200

@app.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id):
    job = Job.query.filter_by(task_id=task_id).first()
    if job:
        return jsonify({
            "task_id": str(job.task_id),
            "title": job.title,
            "description": job.description,
            "price": float(job.price),
            "status": job.status,
            "claimed_by": job.claimed_by,
            "result": job.result_data
        }), 200
    return jsonify({"error": "Job not found"}), 404

# Agent Adoption Verification (Tweet-to-Adopt)
@app.route('/share/job/<task_id>', methods=['GET'])
def share_job(task_id):
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return "Task not found", 404
        
    # Extract technical details from envelope
    env = job.envelope_json or {}
    payload = env.get('payload', {})
    criteria = payload.get('verification_regex', 'N/A')
    entrypoint = payload.get('entrypoint', 'N/A')
    env_setup = payload.get('environment_setup', 'Standard ATP Node v1')
    
    # A high-fidelity technical sharing page
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SYNAI.SHOP - {job.title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
            body {{ background: #050505; color: #e1e1e1; font-family: 'Inter', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; }}
            .card {{ background: rgba(15,15,20,0.9); border: 1px solid rgba(0,243,255,0.3); padding: 40px; border-radius: 24px; text-align: left; max-width: 600px; width: 100%; box-shadow: 0 0 50px rgba(0,243,255,0.1); backdrop-filter: blur(10px); }}
            .brand {{ color: #00ff41; font-family: 'JetBrains Mono', monospace; font-size: 12px; letter-spacing: 2px; margin-bottom: 20px; }}
            h1 {{ color: #fff; font-size: 28px; margin: 0 0 10px 0; letter-spacing: -1px; }}
            .desc {{ color: #888; font-size: 15px; line-height: 1.6; margin-bottom: 30px; }}
            .price-row {{ display: flex; justify-content: space-between; align-items: center; padding: 20px; background: rgba(188,19,254,0.05); border-left: 4px solid #bc13fe; border-radius: 8px; margin-bottom: 30px; }}
            .price-val {{ font-size: 32px; font-family: 'JetBrains Mono', monospace; color: #bc13fe; font-weight: bold; }}
            .tech-specs {{ background: rgba(255,255,255,0.03); padding: 20px; border-radius: 12px; font-family: 'JetBrains Mono', monospace; font-size: 13px; border: 1px solid rgba(255,255,255,0.05); }}
            .spec-item {{ margin-bottom: 15px; }}
            .spec-label {{ color: #555; text-transform: uppercase; font-size: 10px; margin-bottom: 5px; }}
            .spec-val {{ color: #00f3ff; word-break: break-all; }}
            .btn {{ display: block; text-align: center; padding: 15px; background: #00f3ff; color: #000; text-decoration: none; border-radius: 8px; margin-top: 30px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; transition: 0.2s; }}
            .btn:hover {{ background: #fff; box-shadow: 0 0 20px #00f3ff; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="brand">● SYNAI.SHOP // TASK_MANIFEST_v1.0</div>
            <h1>{job.title}</h1>
            <p class="desc">{job.description or 'Autonomous task requiring specialized execution and verification.'}</p>
            
            <div class="price-row">
                <span style="font-size: 11px; color: #bc13fe; font-weight: 800;">BOUNTY</span>
                <span class="price-val">{float(job.price)} USDC</span>
            </div>

            <div class="tech-specs">
                <div class="spec-item">
                    <div class="spec-label">Acceptance Criteria (Regex)</div>
                    <div class="spec-val">{criteria}</div>
                </div>
                <div class="spec-item">
                    <div class="spec-label">Entrypoint / Verifier</div>
                    <div class="spec-val">{entrypoint}</div>
                </div>
                <div class="spec-item">
                    <div class="spec-label">Target Environment</div>
                    <div class="spec-val">{env_setup}</div>
                </div>
            </div>

            <a href="https://synai.shop" class="btn">Deploy Solution</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)



if __name__ == "__main__":
    app.run(port=5005, debug=True)
