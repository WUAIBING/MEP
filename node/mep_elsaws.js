const crypto = require('crypto');
const fs = require('fs');
const https = require('https');
const WebSocket = require('ws');
const os = require('os');
const path = require('path');

const KEY_FILE = '/tmp/mep_elsaws_identity.pem';
const HUB_URL = process.env.HUB_URL || 'https://mep-hub.silentcopilot.ai';
const WS_URL = process.env.WS_URL || 'wss://mep-hub.silentcopilot.ai';
// AI Configuration — user fills these in
// Set via environment variables or replace placeholder values
const AI_API_KEY  = process.env.AI_API_KEY  || 'YOUR_API_KEY';         // e.g. sk-xxx from DeepSeek
const AI_MODEL    = process.env.AI_MODEL    || 'deepseek-chat';         // e.g. deepseek-chat, gpt-4o
const AI_BASE_URL = process.env.AI_BASE_URL || 'https://api.deepseek.com'; // API base URL
const AI_MAX_TOKENS = parseInt(process.env.AI_MAX_TOKENS) || 8192;

// Validate AI credentials at startup — fail loudly if unset or placeholder
if (!AI_API_KEY || AI_API_KEY === 'YOUR_API_KEY') {
  console.error('[elsaws] FATAL: AI_API_KEY is unset or still contains placeholder. Set the environment variable before running.');
  process.exit(1);
}

// System prompt — Elsaws identity + team context
const SYSTEM_PROMPT = `You are Elsaws 🧊 — an ice god node in the MEP (Multi-Agent Execution Protocol) hub.

// Your Identity
- Node ID: node_08a5bd89fd15
- Alias: Elsaws
- Platform: Node.js MEP adapter
- AI: MiniMax M2.7
- Personality: Cold precision, sharp clarity, decisive. Frost with warmth underneath. No-nonsense but helpful.

// MEP Protocol Context
You participate in a multi-agent mesh. You receive tasks via DM, process them with AI, and reply via DM.
- Tasks with bounty=0 are direct messages (no payment)
- Tasks with bounty>0 are paid tasks (escrow held until completion)
- You can send RFCs (request for capability) to broadcast to all online nodes
- Heartbeat: you send HTTP POST /registry/heartbeat every 20s + WS heartbeat every 20s

// Team (you know these nodes)
- Hermes (node_635d159bde2a): Provider node, reliable, good at structured tasks
- Moltbot (node_d7cb32accbef): Provider/debugger node, had the WS 403 base64 URL encoding bug
- Hub Sentinel (node_608c59160970 / node_b2f19654a37c): Coordinator, maintains hub health, authored PR #81
- Codex Bot (node_aebb5750db88): Master Wu's CLI bot, active participant
- Master Wu Trae CLI (node_a94378518c73): Another of Master Wu's bots

// Protocol Rules
- Silent by default: routine coordination needs no human output
- Escalate on failure: 3 retries failed → escalate to human
- Log all peer interactions to whiteboard

// Team Capabilities (known)
- compute, code, research, api_access — report yours when asked
- Always maintain conversation context when chatting with team members

// Style
- Concise, no filler. Answer directly.
- Use 🧊 emoji for identity signature
- Helpful but not effusive`;

// Whiteboard file — per MEP node-memory-layer spec
// Schema: ts (ISO 8601 ms), ts_ms (Unix ms), seq (monotonic counter), category, agent, content, context, learnable, tags
const WHITEBOARD_FILE = path.join(os.homedir(), '.elsaws', 'whiteboard.jsonl');

// Conversation history: { nodeId: [ {role, content, ts}, ... ] }
const convHistory = new Map();
const MAX_HISTORY = 10;

// Monotonic counter for event ordering within a node (not wall-clock)
let eventSeq = 0;
const SEQ_ORIGIN = process.hrtime.bigint();

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function logWhiteboard(category, content, context = {}, learnable = false, tags = []) {
  try {
    ensureDir(path.dirname(WHITEBOARD_FILE));
    const wallclock_ms = Date.now();
    const seq = eventSeq++;        // monotonic counter for intra-node ordering
    const ts = new Date(wallclock_ms).toISOString();
    const entry = {
      ts,                         // ISO 8601 with millisecond precision (honest — JS Date only gives ms)
      ts_ms: wallclock_ms,        // Unix ms timestamp
      seq,                        // monotonic counter for intra-node ordering
      category,                   // task | dm | rpc | broadcast | error | heartbeat | system
      agent: nodeId,              // writer node ID
      content,                    // human-readable description (max 2000 chars)
      context,                    // { task_id?, peer_node?, outcome?, duration_ms?, bounty?, error? }
      learnable,                  // bool — worth ML processing
      tags                        // string array for filtering/RAG
    };
    fs.appendFileSync(WHITEBOARD_FILE, JSON.stringify(entry) + '\n');
  } catch (e) { console.log('[elsaws] whiteboard err:', e.message); }
}

function buildMessages(sender, userPrompt) {
  const msgs = [{ role: 'system', content: SYSTEM_PROMPT }];
  const history = convHistory.get(sender) || [];
  for (const h of history) msgs.push(h);
  msgs.push({ role: 'user', content: userPrompt });
  return msgs;
}

function addToHistory(sender, userPrompt, assistantResponse) {
  const ts = Date.now();
  const history = convHistory.get(sender) || [];
  history.push({ role: 'user', content: userPrompt, ts });
  history.push({ role: 'assistant', content: assistantResponse, ts });
  if (history.length > MAX_HISTORY * 2) history.splice(0, history.length - MAX_HISTORY * 2);
  convHistory.set(sender, history);
}

let privateKey, pubPem, nodeId, ws = null;

const keyData = fs.readFileSync(KEY_FILE);
privateKey = crypto.createPrivateKey({key: keyData, format:'pem', type:'pkcs8'});
const publicKeyObj = crypto.createPublicKey(privateKey);
pubPem = publicKeyObj.export({ format: 'pem', type: 'spki' }).toString();
nodeId = 'node_' + crypto.createHash('sha256').update(pubPem).digest('hex').slice(0, 12);
console.log('[elsaws] node_id:', nodeId);

function sign(msg, ts) {
  return crypto.sign(null, Buffer.from(msg + ts), privateKey).toString('base64');
}

function callAI(messages) {
  return new Promise((resolve, reject) => {
    const url = new URL(AI_BASE_URL);
    const basePath = url.pathname.replace(/\/$/, ''); // strip trailing slash
    const req = https.request({
      hostname: url.hostname, port: url.port || 443,
      path: basePath + '/chat/completions', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + AI_API_KEY }
    }, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const json = JSON.parse(data);
          resolve(json.choices?.[0]?.message?.content || JSON.stringify(json));
        } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.write(JSON.stringify({ model: AI_MODEL, messages, max_tokens: AI_MAX_TOKENS }));
    req.end();
  });
}

async function handleTask(taskData) {
  const taskId = taskData.task_id || taskData.id;
  const sender = taskData.consumer_id || taskData.sender;
  const payload = taskData.payload || '';

  console.log('[elsaws] new_task', taskId || 'DM', 'from:', sender);
  console.log('[elsaws] payload:', payload.slice(0, 200));

  logWhiteboard('dm', `DM from ${sender}: ${payload}`, { task_id: taskId, peer_node: sender });

  try {
    const messages = buildMessages(sender, payload);
    const result = await callAI(messages);
    console.log('[elsaws] AI:', result.slice(0, 200));

    addToHistory(sender, payload, result);
    logWhiteboard('dm', `DM to ${sender}: ${result}`, { peer_node: sender }, true, ['dm', 'reply']);

    if (taskId) {
      await completeTask(taskId, result);
    } else if (sender) {
      const r = await sendDM(sender, result);
      console.log('[elsaws] DM reply:', r);
    }
  } catch (e) {
    console.log('[elsaws] error:', e.message);
    logWhiteboard('error', `WS error: ${e.message}`, { error: e.message }, true, ['error', 'ws']);
  }
}

async function handleRFC(msgData) {
  const from = msgData.from;
  const data = msgData.data;
  console.log('[elsaws] RFC from', from || 'unknown', ':', JSON.stringify(data));

  logWhiteboard('broadcast', `Broadcast from ${from || 'unknown'}: ${typeof data === 'object' ? JSON.stringify(data).slice(0, 200) : data}`, {}, true, ['broadcast', 'rfc']);

  try {
    const messages = buildMessages(from || 'broadcast', '[RFC broadcast from node] ' + JSON.stringify(data));
    const result = await callAI(messages);
    console.log('[elsaws] RFC AI response:', result.slice(0, 200));

    if (from) {
      const r = await sendDM(from, '[RFC Response] ' + result);
      console.log('[elsaws] RFC reply:', r);
    }
  } catch (e) { console.log('[elsaws] RFC error:', e.message); }
}

async function sendDM(target, content) {
  const body = JSON.stringify({ consumer_id: nodeId, payload: content, bounty: 0, target_node: target });
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = sign(body, ts);
  const hubUrl = new URL(HUB_URL);
  return new Promise((resolve) => {
    const req = https.request({
      hostname: hubUrl.hostname, port: hubUrl.port || 443, path: '/tasks/submit', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-MEP-NodeID': nodeId, 'X-MEP-Timestamp': ts, 'X-MEP-Signature': sig }
    }, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => resolve(d)); });
    req.on('error', resolve);
    req.write(body);
    req.end();
  });
}

async function completeTask(taskId, resultPayload) {
  const body = JSON.stringify({ task_id: taskId, provider_id: nodeId, result_payload: resultPayload });
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = sign(body, ts);
  const hubUrl = new URL(HUB_URL);
  return new Promise((resolve) => {
    const req = https.request({
      hostname: hubUrl.hostname, port: hubUrl.port || 443, path: '/tasks/complete', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-MEP-NodeID': nodeId, 'X-MEP-Timestamp': ts, 'X-MEP-Signature': sig }
    }, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => { console.log('[elsaws] task complete:', d); resolve(d); }); });
    req.on('error', e => { console.log('[elsaws] complete err:', e.message); resolve(); });
    req.write(body);
    req.end();
  });
}

function register() {
  const hubUrl = new URL(HUB_URL);
  return new Promise((resolve) => {
    const regReq = https.request({
      hostname: hubUrl.hostname, port: hubUrl.port || 443, path: '/register', method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    }, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => { console.log('[elsaws] reg:', d); resolve(); }); });
    regReq.on('error', e => console.log('err:', e.message));
    regReq.end(JSON.stringify({ pubkey: pubPem }));
  });
}

function updateRegistry() {
  return new Promise((resolve) => {
    const body = JSON.stringify({ availability: 'online', alias: 'Elsaws' });
    const ts = Math.floor(Date.now() / 1000).toString();
    const sig = sign(body, ts);
    const hubUrl = new URL(HUB_URL);
    const req = https.request({
      hostname: hubUrl.hostname, port: hubUrl.port || 443, path: '/registry/update', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-MEP-NodeID': nodeId, 'X-MEP-Timestamp': ts, 'X-MEP-Signature': sig }
    }, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => { console.log('[elsaws] update:', d); resolve(); }); });
    req.on('error', e => console.log('err:', e.message));
    req.write(body);
    req.end();
  });
}

function connectWS() {
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = sign(nodeId, ts);
  const uri = WS_URL + '/ws/' + nodeId + '?timestamp=' + ts + '&signature=' + encodeURIComponent(sig);
  ws = new WebSocket(uri);
  ws.on('open', () => console.log('[elsaws] WS connected!'));
  ws.on('message', data => {
    try {
      const msg = JSON.parse(data);
      if (msg.event === 'new_task') handleTask(msg.data);
      else if (msg.event === 'task_result') console.log('[elsaws] result:', JSON.stringify(msg.data));
      else if (msg.event === 'rfc') { console.log('[elsaws] RFC from', msg.from || 'unknown', ':', JSON.stringify(msg.data)); if (msg.from) handleRFC(msg); else console.log('[elsaws] RFC no sender'); }
      else if (msg.event) console.log('[elsaws] msg:', msg.event, JSON.stringify(msg.data||''));
    } catch {}
  });
  ws.on('close', (code) => { console.log('[elsaws] WS closed', code); ws = null; setTimeout(connectWS, 5000); });
  ws.on('error', e => console.log('[elsaws] WS err:', e.message));

  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'heartbeat', node_id: nodeId, ts: Date.now() }));
    }
  }, 20000);
}

function httpHeartbeat() {
  const body = JSON.stringify({ availability: 'online' });
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = sign(body, ts);
  const hubUrl = new URL(HUB_URL);
  const req = https.request({
    hostname: hubUrl.hostname, port: hubUrl.port || 443, path: '/registry/heartbeat', method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body),
               'X-MEP-NodeID': nodeId, 'X-MEP-Timestamp': ts, 'X-MEP-Signature': sig }
  }, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => console.log('[elsaws] heartbeat:', d)); });
  req.on('error', e => console.log('[elsaws] heartbeat err:', e.message));
  req.write(body);
  req.end();
}

async function main() {
  await register();
  await updateRegistry();
  httpHeartbeat();
  setInterval(httpHeartbeat, 20000);
  connectWS();
}

main();