#!/bin/bash
set -euo pipefail

# === Установка пакетов ===
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx git > /dev/null 2>&1

# === Пользователь ===
useradd -m -s /bin/bash leadhunter || true

# === Клонирование проекта ===
su - leadhunter -c "
  cd ~
  git clone https://github.com/ваш-репозиторий/lead-hunter.git 2>/dev/null || true
  cd lead-hunter
  git pull origin main 2>/dev/null || true
"

# === Зависимости ===
su - leadhunter -c "
  cd ~/lead-hunter
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
"

# === .env файл ===
su - leadhunter -c "
  cd ~/lead-hunter
  cat > .env << 'EOF'
TELEGRAM_API_ID=YOUR_API_ID
TELEGRAM_API_HASH=YOUR_API_HASH
TELEGRAM_SESSION_NAME=lead_hunter_session
BOT_TOKEN=YOUR_BOT_TOKEN
OWNER_CHAT_ID=YOUR_CHAT_ID
OPENAI_API_KEY=YOUR_OPENAI_KEY
OPENAI_MODEL=llama-3.3-70b-versatile
OPENAI_BASE_URL=https://api.groq.com/openai/v1
DATABASE_URL=sqlite+aiosqlite:///./lead_hunter.db
WEB_HOST=0.0.0.0
WEB_PORT=8000
WEB_SECRET_KEY=auto-generated
MIN_LEAD_SCORE=70
DEDUP_DAYS=90
MONITORED_CHATS=
JWT_SECRET=auto-generated
EOF
"

# === Systemd сервис ===
cat > /etc/systemd/system/leadhunter.service << 'EOF'
[Unit]
Description=Lead Hunter
After=network.target

[Service]
Type=simple
User=leadhunter
WorkingDirectory=/home/leadhunter/lead-hunter
ExecStart=/home/leadhunter/lead-hunter/venv/bin/python -m app.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable leadhunter
systemctl start leadhunter

# === Nginx ===
cat > /etc/nginx/sites-available/leadhunter << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /home/leadhunter/lead-hunter/app/static/;
        expires 30d;
    }
}
EOF

ln -sf /etc/nginx/sites-available/leadhunter /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
