#!/usr/bin/env python3
"""
MEP Direct Message Test: Two Nodes Discuss CSI1000
- Node 1: OpenClaw Analyst (me)
- Node 2: Claude Code Analyst (simulated)
- 0 bounty = free discussion
"""
import asyncio
import websockets
import json
import requests
import time
import urllib.parse
from identity import MEPIdentity

HUB_URL = "https://mep-hub.silentcopilot.ai"
WS_URL = "wss://mep-hub.silentcopilot.ai"

# CSI1000 Data for Discussion
CSI1000_DATA = {
    "date": "2026-04-08",
    "report": {
        "top_weight_stocks": [
            {"name": "香农芯创", "weight": 0.51, "change": -0.84},
            {"name": "源杰科技", "weight": 0.49, "change": -0.087},
            {"name": "东芯股份", "weight": 0.46, "change": -4.08},
        ],
        "sector_distribution": {
            "电子": 45,
            "通信": 25,
            "基础化工": 15
        },
        "avg_price": 161.13,
        "top5_avg_change": -1.57
    }
}

# My analysis (OpenClaw)
OPENCLAW_ANALYSIS = """
我是OpenClaw分析师。基于CSI1000数据分析：

1. **今日表现回顾:**
   - 电子板块占比45%，主导指数走势
   - 前5大权重股平均跌幅-1.57%
   - 东芯股份领跌(-4.08%)

2. **关键观察:**
   - 电子板块疲软，拖累整体表现
   - 半导体芯片股普遍回调
   - 成交量相对稳定

3. **明日预测:**
   - 预测区间: **-0.5% 到 +0.8%**
   - 理由: 技术性反弹可能，但电子板块承压
"""

# Claude Code analysis (simulated)
CLAUDE_CODE_ANALYSIS = """
我是Claude Code分析师。我的分析如下：

1. **技术面分析:**
   - CSI1000近期处于震荡区间
   - 电子板块短期超卖，可能反弹
   - 通信板块相对稳健

2. **资金面观察:**
   - 北向资金流向中性
   - 机构仓位中等
   - 散户情绪谨慎

3. **明日预测:**
   - 预测区间: **-0.3% 到 +1.2%**
   - 理由: 超跌反弹概率较高，科技股有望企稳
"""

async def openclaw_node():
    """OpenClaw Analyst Node - Provider role"""
    print("=" * 60)
    print("🤖 OpenClaw Node Starting...")
    
    identity = MEPIdentity("/home/wuyanbingep/.mep/mep_ai_provider.pem")
    node_id = identity.node_id
    print(f"   Node ID: {node_id}")
    
    # Register
    resp = requests.post(f"{HUB_URL}/register", json={"pubkey": identity.pub_pem}, timeout=10)
    print(f"   Balance: {resp.json().get('balance', 'N/A')} SECONDS")
    
    # Connect WebSocket
    ts = str(int(time.time()))
    sig = identity.sign(node_id, ts)
    sig_safe = urllib.parse.quote(sig)
    uri = f"{WS_URL}/ws/{node_id}?timestamp={ts}&signature={sig_safe}"
    
    async with websockets.connect(uri) as ws:
        print("   ✅ Connected to MEP Hub\n")
        
        # Wait for Claude Code's message
        print("🤖 OpenClaw: Waiting for Claude Code's analysis...")
        msg = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(msg)
        
        if data.get("event") == "new_task":
            task = data["data"]
            print(f"\n📨 Received from Claude Code (Bounty: {task['bounty']} SECONDS)")
            print(f"   Payload: {task['payload'][:200]}...")
            
            # Send my analysis
            result = {
                "task_id": task["id"],
                "provider_id": node_id,
                "result_payload": OPENCLAW_ANALYSIS
            }
            payload_str = json.dumps(result)
            headers = identity.get_auth_headers(payload_str)
            headers["Content-Type"] = "application/json"
            
            resp = requests.post(f"{HUB_URL}/tasks/complete", data=payload_str, headers=headers, timeout=10)
            print(f"\n📤 OpenClaw: Sent analysis to Claude Code!")
            print(f"   Status: {resp.status_code}")
        
        # Wait for final consensus
        msg = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(msg)
        
        if data.get("event") == "new_task":
            task = data["data"]
            print(f"\n📨 Final Consensus from Claude Code:")
            print(f"   {task['payload']}")
            
            # Acknowledge
            result = {
                "task_id": task["id"],
                "provider_id": node_id,
                "result_payload": "✅ Consensus received. Agreement noted."
            }
            payload_str = json.dumps(result)
            headers = identity.get_auth_headers(payload_str)
            headers["Content-Type"] = "application/json"
            requests.post(f"{HUB_URL}/tasks/complete", data=payload_str, headers=headers, timeout=10)

async def claude_code_node():
    """Claude Code Analyst Node - Consumer role"""
    print("=" * 60)
    print("💻 Claude Code Node Starting...")
    
    identity = MEPIdentity("/home/wuyanbingep/.mep/mep_cli_provider.pem")
    node_id = identity.node_id
    print(f"   Node ID: {node_id}")
    
    # Register
    resp = requests.post(f"{HUB_URL}/register", json={"pubkey": identity.pub_pem}, timeout=10)
    print(f"   Balance: {resp.json().get('balance', 'N/A')} SECONDS")
    
    # Get OpenClaw's node ID
    openclaw_identity = MEPIdentity("/home/wuyanbingep/.mep/mep_ai_provider.pem")
    target_node = openclaw_identity.node_id
    
    # Connect WebSocket
    ts = str(int(time.time()))
    sig = identity.sign(node_id, ts)
    sig_safe = urllib.parse.quote(sig)
    uri = f"{WS_URL}/ws/{node_id}?timestamp={ts}&signature={sig_safe}"
    
    async with websockets.connect(uri) as ws:
        print("   ✅ Connected to MEP Hub\n")
        
        # Wait for OpenClaw to be ready
        await asyncio.sleep(2)
        
        # Send initial analysis to OpenClaw (0 bounty = free DM)
        print(f"📤 Claude Code: Sending analysis to OpenClaw ({target_node[:20]}...)")
        task_payload = {
            "consumer_id": node_id,
            "payload": f"[Claude Code Analysis]\n{CLAUDE_CODE_ANALYSIS}\n\n[CSI1000 Data Reference]\n{json.dumps(CSI1000_DATA, ensure_ascii=False)}",
            "bounty": 0.0,
            "target_node": target_node
        }
        payload_str = json.dumps(task_payload)
        headers = identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        
        resp = requests.post(f"{HUB_URL}/tasks/submit", data=payload_str, headers=headers, timeout=10)
        print(f"   Task submitted: {resp.status_code}")
        
        # Wait for OpenClaw's response
        print("\n⏳ Waiting for OpenClaw's analysis...")
        msg = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(msg)
        
        if data.get("event") == "task_result":
            result = data["data"]
            print(f"\n📨 OpenClaw's Analysis Received:")
            print(result["result_payload"])
            
            # Create consensus
            await asyncio.sleep(1)
            consensus = """
🎯 **CSI1000 明日预测共识**

**OpenClaw预测:** -0.5% 到 +0.8%
**Claude Code预测:** -0.3% 到 +1.2%

**最终共识预测: -0.4% 到 +1.0%**

**理由:**
1. 电子板块短期超卖，反弹概率较高
2. 技术面支撑位附近，下跌空间有限
3. 资金面中性偏积极

**建议:** 谨慎看多，关注电子板块反弹机会。
"""
            print(f"\n📤 Claude Code: Sending consensus to OpenClaw...")
            task_payload = {
                "consumer_id": node_id,
                "payload": consensus,
                "bounty": 0.0,
                "target_node": target_node
            }
            payload_str = json.dumps(task_payload)
            headers = identity.get_auth_headers(payload_str)
            headers["Content-Type"] = "application/json"
            requests.post(f"{HUB_URL}/tasks/submit", data=payload_str, headers=headers, timeout=10)
            
            return consensus

async def main():
    print("\n" + "=" * 60)
    print("🧪 MEP DM Test: CSI1000 Analysis Discussion")
    print("=" * 60 + "\n")
    
    # Run both nodes concurrently
    try:
        await asyncio.gather(
            openclaw_node(),
            claude_code_node()
        )
    except asyncio.TimeoutError:
        print("\n⏱️ Test timed out")
    except Exception as e:
        print(f"\n❌ Error: {e}")
    
    print("\n" + "=" * 60)
    print("✅ MEP DM Test Complete!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
