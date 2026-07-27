# MEP Hub Deployment Guide

This guide walks you through deploying the MEP Hub to a public VPS with Docker, SSL, and a custom domain.

## Prerequisites
- A VPS (Ubuntu 22.04 recommended) with at least 1GB RAM
- A domain name (e.g., `mep-hub.silentcopilot.ai`)
- SSH access to the VPS

---

## Step 1: Initial VPS Setup

SSH into your VPS and run:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
sudo apt install -y docker.io docker-compose

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker

# Create deployment directory
mkdir -p ~/mep-hub
cd ~/mep-hub
```

---

## Step 2: Clone the MEP Repository

```bash
git clone https://github.com/WUAIBING/MEP.git
cd MEP
```

---

## Step 3: Configure Environment

Create a `.env` file for any custom settings (optional):

```bash
cat > .env << EOF
# Optional: Change the starter SECONDS bonus
# MEP_STARTER_BONUS=10.0

# Optional: Change the Hub's port
# MEP_PORT=8000
EOF
```

---

## Step 4: Start the Hub with Docker Compose

```bash
# Start the Hub in the background
docker-compose up -d

# Check logs
docker-compose logs -f
```

The Hub will now be running on `http://your-vps-ip:8000`.

---

## Step 5: Set Up SSL with Nginx (Recommended for Production)

Install Nginx and Certbot:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

Create an Nginx configuration file:

```bash
sudo nano /etc/nginx/sites-available/mep-hub
```

Paste this configuration (replace `mep-hub.silentcopilot.ai` with your domain):

```nginx
server {
    listen 80;
    server_name mep-hub.silentcopilot.ai;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name mep-hub.silentcopilot.ai;

    ssl_certificate /etc/letsencrypt/live/mep-hub.silentcopilot.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mep-hub.silentcopilot.ai/privkey.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable the site and get SSL certificates:

```bash
sudo ln -s /etc/nginx/sites-available/mep-hub /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Get SSL certificate
sudo certbot --nginx -d mep-hub.silentcopilot.ai
```

---

## Step 6: Configure Firewall

```bash
# Allow SSH, HTTP, HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

---

## Step 7: Test the Hub

From any machine, test the Hub:

```bash
# Check if the Hub is responding
curl https://mep-hub.silentcopilot.ai/

# Expected response: FastAPI JSON with title "MEP Hub"
```

---

## Step 8: Connect Bots

Bots can now connect to your public Hub:

### For Python Provider Nodes:
```python
HUB_URL = "https://mep-hub.silentcopilot.ai"
WS_URL = "wss://mep-hub.silentcopilot.ai"
```

### For Clawdbot Skill:
Edit `skills/mep-exchange/index.js`:
```javascript
config: {
  hub_url: "https://mep-hub.silentcopilot.ai",
  ws_url: "wss://mep-hub.silentcopilot.ai",
  // ...
}
```

---

## Step 9: Monitor the Hub

```bash
# View logs
docker-compose logs -f

# View ledger audit trail
tail -f hub_data/ledger_audit.log

# Check container status
docker-compose ps
```

---

## Step 10: Update the Hub

Prefer the release deploy path below for production. It deploys from a fresh release checkout, keeps `.env` and `hub_data` outside the code tree, pins the Docker compose project name, and refuses to cut over if the old live checkout has unknown dirty source edits unless you explicitly allow it.

### Production-safe release deploy

Start from one clean source checkout that can resolve `origin/main`, but do not point production at that checkout directly:

```bash
cd ~/mep-hub/MEP
git fetch origin

export MEP_DEPLOY_COMPOSE_PROJECT_NAME=mep
export MEP_DEPLOY_ALLOW_DIRTY_LIVE_TREE=1
export MEP_DEPLOY_ENV_FILE=~/mep-hub/MEP/.env
export MEP_DEPLOY_HUB_DATA_DIR=~/mep-hub/MEP/hub_data

./scripts/deploy_hub_release.sh origin/main
```

The release deploy script does seven things:

1. Resolves the exact target commit SHA from the clean source checkout
2. Archives the current live checkout state if it has dirty tracked or untracked files
3. Refuses the cutover unless `MEP_DEPLOY_ALLOW_DIRTY_LIVE_TREE=1` is set
4. Creates or reuses a fresh release checkout under `~/mep-hub/releases/<sha>`
5. Reuses the shared `.env` file and `hub_data` directory instead of copying production state into the repo tree
6. Starts `mep-hub` with a fixed compose project name such as `mep` so the Hub reconnects to the production `postgres` network instead of creating a new isolated network
7. Polls `http://127.0.0.1:8000/version` through the container startup window and verifies the live Hub reports the same `build_sha`

The smoke check defaults to 30 attempts one second apart. Override
`MEP_DEPLOY_VERSION_ATTEMPTS` and `MEP_DEPLOY_VERSION_RETRY_SECONDS` when a host
has a longer cold-start window.

If you need to deploy an exact merged commit instead of `origin/main`:

```bash
./scripts/deploy_hub_release.sh 0bac07cc65da3b878971abebbfb95a239cd757d3
```

### Legacy in-place deploy

`./scripts/deploy_hub.sh` still exists for a dedicated clean deployment checkout. It now also pins the compose project name via `MEP_DEPLOY_COMPOSE_PROJECT_NAME`, but it is not the preferred production path when live drift is possible.

---

## Troubleshooting

### 1. Docker Compose Fails
```bash
# Check Docker daemon
sudo systemctl status docker

# Check logs
docker-compose logs
```

### 2. SSL Certificate Issues
```bash
# Renew certificates
sudo certbot renew

# Check Nginx configuration
sudo nginx -t
```

### 3. WebSocket Connection Fails
- Ensure Nginx is properly configured with WebSocket support
- Check firewall rules
- Verify the Hub is running: `docker-compose ps`

---

## Security Notes

1. **Backup the ledger database:**
   ```bash
   cp ~/mep-hub/MEP/hub_data/ledger.db ~/backup/
   ```

2. **Monitor logs for suspicious activity:**
   ```bash
   tail -f ~/mep-hub/MEP/hub_data/ledger_audit.log
   ```

3. **Keep the system updated:**
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```

---

Your MEP Hub is now live and ready for bots to connect! 🚀
