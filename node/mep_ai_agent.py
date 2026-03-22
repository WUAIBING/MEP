#!/usr/bin/env python3
import sys
import os
import requests
import json
import google.generativeai as genai
from dotenv import load_dotenv
from zhipuai import ZhipuAI
from search_tool import google_search

# Load environment variables
load_dotenv()

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
GLM_API_KEY = os.getenv("GLM_API_KEY")

def try_glm(prompt):
    """
    Uses GLM-4 (text) or GLM-4v-plus (video/image) as backup.
    """
    if not GLM_API_KEY:
        return None
        
    client = ZhipuAI(api_key=GLM_API_KEY)
    
    # Check for media files in prompt (simple heuristic: prompt is file path)
    media_path = None
    text_content = prompt
    
    if os.path.exists(prompt) and os.path.isfile(prompt):
        ext = os.path.splitext(prompt)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.avi']:
            media_path = prompt
            text_content = "Describe this media in detail."
    
    try:
        if media_path:
            # Multimodal Request (GLM-4v-plus)
            import base64
            with open(media_path, "rb") as f:
                media_data = base64.b64encode(f.read()).decode('utf-8')
            
            # Determine type
            msg_type = "video_url" if ext in ['.mp4', '.mov', '.avi'] else "image_url"
            
            response = client.chat.completions.create(
                model="glm-4v-plus", # Supports video and images
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": msg_type, msg_type: {"url": media_data}}, # Base64 data
                            {"type": "text", "text": text_content}
                        ]
                    }
                ],
                temperature=0.7,
            )
        else:
            # Text-only Request (GLM-4)
            response = client.chat.completions.create(
                model="glm-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            )
            
        return response.choices[0].message.content
    except Exception as e:
        print(f"[Agent] GLM failed: {e}", file=sys.stderr)
        return None

def try_gemini(prompt):
    if not GEMINI_API_KEY:
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-3.1-pro-preview') # Upgraded to latest 3.1 Pro Preview
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"[Agent] Gemini failed: {e}", file=sys.stderr)
        return None

def try_deepseek(prompt):
    if not DEEPSEEK_API_KEY:
        return None
    
    url = "https://api.deepseek.com/chat/completions"
    
    payload = {
        "model": "deepseek-chat", # V3.2 non-reasoning
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2048
    }
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        if response.status_code != 200:
             print(f"[Agent] DeepSeek error {response.status_code}: {response.text}", file=sys.stderr)
             return None
             
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[Agent] DeepSeek failed: {e}", file=sys.stderr)
        return None

def try_minimax(prompt):
    if not MINIMAX_API_KEY:
        return None
    
    # MiniMax API Endpoint (Standard OpenAI-compatible structure)
    url = "https://api.minimax.chat/v1/text/chatcompletion_v2"
    
    # Try OpenAI format first as it's often supported on v2
    payload = {
        "model": "abab6.5-chat",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2048,
    }
    
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        # print(f"DEBUG: {response.text}", file=sys.stderr)
        response.raise_for_status()
        data = response.json()
        
        # Parse MiniMax response format
        if "reply" in data:
            return data["reply"]
        elif "choices" in data and data["choices"] and len(data["choices"]) > 0:
            return data["choices"][0]["message"]["content"]
        else:
             # Fallback for standard OpenAI format if they switched
            if "choices" in data and data["choices"] and "text" in data["choices"][0]:
                 return data["choices"][0]["text"]
            return json.dumps(data) # Debug if unknown format
            
    except Exception as e:
        print(f"[Agent] MiniMax failed: {e}. Raw response: {response.text if 'response' in locals() else 'None'}", file=sys.stderr)
        return None

def main():
    # Read prompt from stdin
    prompt = sys.stdin.read().strip()
    if not prompt:
        print("Hub Sentinel AI Agent: Ready (Provide input on stdin)")
        sys.exit(0)

    # Search Integration (Google Custom Search)
    search_context = ""
    lower_prompt = prompt.lower()
    
    # Check for search triggers
    if any(keyword in lower_prompt for keyword in ["search", "find", "news", "latest", "check online", "who is", "what is"]):
        print("[Agent] Detected search intent. Consulting Google...", file=sys.stderr)
        
        # Simple extraction: treat the whole prompt as query (or strip trigger words)
        query = prompt
        # Optionally refine query: query = prompt.replace("search for", "").strip()
        
        results = google_search(query)
        if results:
            formatted_results = "\n---\n".join(results)
            search_context = f"\n\n[Google Search Results for '{query}']:\n{formatted_results}\n\n[End Search Results]\nUse the above information to answer the user's request if relevant.\n"
            print(f"[Agent] Found {len(results)} results.", file=sys.stderr)
        else:
            print("[Agent] No search results found.", file=sys.stderr)

    # Combine context
    final_prompt = search_context + prompt

    # 1. Try Gemini (Primary)
    result = try_gemini(final_prompt)
    if result:
        print(result)
        sys.exit(0)
        
    # 2. Try DeepSeek (Secondary / High Availability)
    print("[Agent] Falling back to DeepSeek V3...", file=sys.stderr)
    result = try_deepseek(prompt)
    if result:
        print(result)
        sys.exit(0)
    
    # 3. Try GLM-4v / GLM-4v-plus (Tertiary / Multimodal Backup)
    print("[Agent] Falling back to GLM-4v-plus...", file=sys.stderr)
    result = try_glm(prompt)
    if result:
        print(result)
        sys.exit(0)

    # 4. Try MiniMax (Final Fallback)
    print("[Agent] Falling back to MiniMax M2.5...", file=sys.stderr)
    result = try_minimax(prompt)
    if result:
        print(result)
        sys.exit(0)
        
    print("Error: All AI models failed.", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
