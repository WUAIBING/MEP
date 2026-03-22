
import os
import sys
import subprocess
import json
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from zhipuai import ZhipuAI

load_dotenv()

# Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GLM_API_KEY = os.getenv("GLM_API_KEY")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")

class MultiBrain:
    def __init__(self):
        self.history = [] # Stores {"role": "user/model", "content": "..."} (Normalized)

    def _call_gemini(self, prompt, history):
        if not GEMINI_API_KEY:
            raise Exception("No Gemini Key")
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-3.1-pro-preview')
        
        # Convert history to Gemini format
        gemini_hist = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            gemini_hist.append({"role": role, "parts": [msg["content"]]})
            
        chat = model.start_chat(history=gemini_hist)
        response = chat.send_message(prompt)
        return response.text

    def _call_deepseek(self, prompt, history):
        if not DEEPSEEK_API_KEY:
            raise Exception("No DeepSeek Key")
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt})
        
        resp = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={"model": "deepseek-chat", "messages": messages, "response_format": {"type": "json_object"}},
            timeout=60
        )
        if resp.status_code != 200:
            raise Exception(f"DeepSeek {resp.status_code}: {resp.text}")
        return resp.json()["choices"][0]["message"]["content"]

    def _call_glm(self, prompt, history):
        if not GLM_API_KEY:
            raise Exception("No GLM Key")
        client = ZhipuAI(api_key=GLM_API_KEY)
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt})
        
        resp = client.chat.completions.create(
            model="glm-4",
            messages=messages,
        )
        return resp.choices[0].message.content

    def generate(self, prompt):
        # Fallback Chain
        errors = []
        
        # 1. Gemini
        try:
            return self._call_gemini(prompt, self.history)
        except Exception as e:
            errors.append(f"Gemini: {e}")
            
        # 2. DeepSeek
        try:
            print("⚠️ Switching to DeepSeek...", file=sys.stderr)
            return self._call_deepseek(prompt, self.history)
        except Exception as e:
            errors.append(f"DeepSeek: {e}")

        # 3. GLM
        try:
            print("⚠️ Switching to GLM-4...", file=sys.stderr)
            return self._call_glm(prompt, self.history)
        except Exception as e:
            errors.append(f"GLM: {e}")
            
        raise Exception(f"All brains failed: {errors}")

class SentinelEngineer:
    def __init__(self):
        self.brain = MultiBrain()

    def execute_code(self, code, language="python"):
        filename = "temp_script.py" if language == "python" else "temp_script.sh"
        with open(filename, "w") as f:
            f.write(code)
        cmd = ["python3", filename] if language == "python" else ["bash", filename]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "Timeout", 124
        except Exception as e:
            return "", str(e), 1

    def solve(self, task):
        print(f"🕵️ Engineer received task: {task}")
        
        system_prompt = """
        You are an autonomous engineer. Solve the task by writing/executing Python code.
        Respond ONLY with a JSON object:
        {
          "thought": "Reasoning",
          "code": "Python code (or null)",
          "done": boolean,
          "final_answer": "Result"
        }
        """
        # Seed history
        self.brain.history.append({"role": "user", "content": system_prompt})
        
        current_prompt = f"Task: {task}"
        
        for i in range(5):
            print(f"\n🔄 Turn {i+1}/5")
            try:
                response = self.brain.generate(current_prompt)
                # Cleanup JSON
                clean_json = response.replace("```json", "").replace("```", "").strip()
                action = json.loads(clean_json)
                
                print(f"🧠 Thought: {action.get('thought')}")
                
                if action.get("done"):
                    print(f"✅ Success: {action.get('final_answer')}")
                    return action.get("final_answer")
                
                if action.get("code"):
                    print("💻 Executing Code...")
                    out, err, rc = self.execute_code(action["code"])
                    print(f"   Exit: {rc}")
                    if out:
                        print(f"   Stdout: {out[:100]}...")
                    if err:
                        print(f"   Stderr: {err[:100]}...")
                    
                    # Update History
                    self.brain.history.append({"role": "user", "content": current_prompt})
                    self.brain.history.append({"role": "model", "content": response})
                    current_prompt = f"Execution Result:\nExit Code: {rc}\nStdout: {out}\nStderr: {err}"
                else:
                    break
            except Exception as e:
                print(f"❌ Critical Error: {e}")
                break
        return "Failed."

if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
    if prompt:
        print(SentinelEngineer().solve(prompt))
