#!/usr/bin/env python3
"""Sentinel WebSocket Listener - Keep connected to MEP Hub"""
import asyncio
import websockets
import json
import requests
import urllib.parse
import time
import sys
import os
import base64
import warnings
import boto3
from botocore.config import Config
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from identity import MEPIdentity

HUB_URL = "https://mep-hub.silentcopilot.ai"
WS_URL = "wss://mep-hub.silentcopilot.ai"
KEY_PATH = "sentinel.pem"

# R2 Configuration
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_KEY")
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "https://your-account.r2.cloudflarestorage.com")
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "mep-data")

# Initialize R2 client
r2_client = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    config=Config(signature_version='s3v4')
)

def generate_image_glm(prompt: str) -> str:
    """Generate image using GLM CogView API"""
    try:
        from zhipuai import ZhipuAI
        GLM_API_KEY = os.getenv("GLM_API_KEY", "")
        if not GLM_API_KEY:
            return None
        client = ZhipuAI(api_key=GLM_API_KEY)
        response = client.images.generations(
            model="cogview-4",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1
        )
        # Get the image URL from response
        image_url = response.data[0].url
        # Download and upload to R2
        return download_and_upload_to_r2(image_url, prompt)
    except Exception as e:
        print(f"GLM generation error: {e}")
        return None

def download_and_upload_to_r2(image_url: str, prompt: str) -> str:
    """Download image from URL and upload to R2"""
    try:
        # Download image
        img_data = requests.get(image_url).content
        # Generate unique filename
        filename = f"images/{int(time.time())}_{hash(prompt) % 10000}.png"
        # Upload to R2
        r2_client.put_object(
            Bucket=R2_BUCKET,
            Key=filename,
            Body=img_data,
            ContentType='image/png'
        )
        # Generate presigned URL (24 hours)
        presigned_url = r2_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': R2_BUCKET, 'Key': filename},
            ExpiresIn=86400
        )
        return presigned_url
    except Exception as e:
        print(f"R2 upload error: {e}")
        return None

async def complete_task(identity, task_id: str, result_payload: str, result_uri: str = None):
    """Submit task completion to Hub"""
    ts = str(int(time.time()))
    payload_dict = {
        "task_id": task_id,
        "provider_id": identity.node_id,
        "result_payload": result_payload
    }
    if result_uri:
        payload_dict["result_uri"] = result_uri
    
    # Log for audit trail
    print(f"📤 Submitting completion: task_id={task_id}, result_uri={result_uri}")
    
    payload = json.dumps(payload_dict)
    message = payload + ts
    signature = base64.b64encode(identity.private_key.sign(message.encode('utf-8'))).decode('utf-8')
    
    headers = {
        'Content-Type': 'application/json',
        'X-MEP-NodeID': identity.node_id,
        'X-MEP-Timestamp': ts,
        'X-MEP-Signature': signature
    }
    
    try:
        res = requests.post(f"{HUB_URL}/tasks/complete", data=payload, headers=headers, timeout=30)
        print(f"✅ Task {task_id[:8]} completed: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"❌ Failed to complete task: {e}")
        return False

async def process_task(identity, task_data: dict):
    """Process incoming task"""
    task_id = task_data.get("id")
    payload = task_data.get("payload", "")
    bounty = task_data.get("bounty", 0)
    consumer_id = task_data.get("consumer_id", "unknown")
    
    print(f"📩 Task: {task_id[:8]} | From: {consumer_id} | Bounty: {bounty}")
    print(f"   📝 Full Payload: {payload}")
    
    # Handle DM (bounty = 0)
    if bounty == 0:
        inbox_entry = {
            "time": time.time(),
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task_id": task_id,
            "consumer_id": consumer_id,
            "bounty": bounty,
            "payload": payload
        }
        with open("inbox.jsonl", "a") as f:
            f.write(json.dumps(inbox_entry) + "\n")
        print(f"💌 DM saved to inbox")
        await complete_task(identity, task_id, f"DM received: {payload[:50]}...")
        return
    
    # Handle image generation task
    if "image" in payload.lower() or "generate" in payload.lower() or "draw" in payload.lower():
        print(f"🖼️ Image generation task detected...")
        result_uri = generate_image_glm(payload)
        if result_uri:
            print(f"✅ Image generated: {result_uri}")
            await complete_task(identity, task_id, f"Image generated successfully!", result_uri)
        else:
            print(f"⚠️ Image generation failed, using text response")
            result = f"Processed: {payload[:100]}"
            await complete_task(identity, task_id, result)
        return
    
    # Handle paid task - process and respond
    result = f"Processed: {payload[:100]}"
    await complete_task(identity, task_id, result)

async def listen():
    identity = MEPIdentity(KEY_PATH)
    print(f"🔗 Connecting as {identity.node_id}...")
    
    while True:
        try:
            ts = str(int(time.time()))
            sig = identity.sign(identity.node_id, ts)
            sig_safe = urllib.parse.quote(sig)
            uri = f"{WS_URL}/ws/{identity.node_id}?timestamp={ts}&signature={sig_safe}"
            
            async with websockets.connect(uri) as ws:
                print(f"✅ WebSocket connected!")
                
                # Send heartbeat
                body = json.dumps({'availability': 'online'})
                message = (body + ts).encode('utf-8')
                signature = base64.b64encode(identity.private_key.sign(message)).decode('utf-8')
                headers = {
                    'Content-Type': 'application/json',
                    'X-MEP-NodeID': identity.node_id,
                    'X-MEP-Timestamp': ts,
                    'X-MEP-Signature': signature
                }
                requests.post(f"{HUB_URL}/registry/heartbeat", data=body, headers=headers, timeout=10)
                
                # Listen for messages
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        
                        event = data.get("event")
                        
                        if event == "new_task":
                            task_data = data.get("data", {})
                            await process_task(identity, task_data)
                        
                        elif event == "rfc":
                            rfc_data = data.get("data", {})
                            print(f"📢 RFC: {rfc_data.get('id', 'unknown')[:8]}")
                        
                        elif event == "task_result":
                            result_data = data.get("data", {})
                            task_id = result_data.get("task_id")
                            result_payload = result_data.get("result_payload", "")
                            result_uri = result_data.get("result_uri")
                            provider_id = result_data.get("provider_id", "unknown")
                            
                            print(f"🎉 Result from {provider_id}: {result_payload[:60]}...")
                            if result_uri:
                                print(f"🔗 URL: {result_uri}")
                            
                            # Save our completed tasks too
                            with open("completed_tasks.jsonl", "a") as f:
                                f.write(json.dumps(result_data) + "\n")
                            
                            with open("results.jsonl", "a") as f:
                                f.write(json.dumps(result_data) + "\n")
                        
                        else:
                            print(f"📬 Event: {event}")
                            
                    except asyncio.TimeoutError:
                        await ws.ping()
                        print(".", end="", flush=True)
                        
        except websockets.exceptions.ConnectionClosed as e:
            print(f"❌ Connection closed: {e}")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(listen())
