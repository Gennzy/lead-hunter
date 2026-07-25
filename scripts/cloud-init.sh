#!/bin/bash
# ============================================================
# Lead Hunter — cloud-init скрипт для timeweb.cloud
# ============================================================
# Инструкция:
#   1. Закажите VPS (Ubuntu 22.04, >= 1GB RAM)
#   2. В панели timeweb.cloud → ваш сервер → "Cloud-init"
#   3. Вставьте содержимое этого файла
#   4. После деплоя: https://<IP>/login → admin / admin123
# ============================================================

set -euo pipefail

# ---- НАСТРОЙКИ (измените перед деплоем) ----
REPO_URL="https://github.com/ВАШ-НИК/lead-hunter.git"
DOMAIN=""                    # Оставьте пустым для доступа по IP
POSTGRES_PASSWORD="change-me" # Замените на надёжный пароль

# Переменные окружения (заполните своими значениями)
export TELEGRAM_API_ID="0"
export TELEGRAM_API_HASH=""
export BOT_TOKEN=""
export OWNER_CHAT_ID=""
export OPENAI_API_KEY=""
export OPENAI_MODEL="gpt-4o-mini"
export MIN_LEAD_SCORE="70"

# ---- ЦВЕТА ДЛЯ ВЫВОДА ----
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*" >&2; }

# ---- 1. СИСТЕМНЫЕ ЗАВИСИМОСТИ ----
log "Установка системных пакетов..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-pip python3-venv python3-dev \
  nginx certbot python3-certbot-nginx \
  postgresql postgresql-contrib \
  git build-essential libpq-dev \
  > /dev/null 2>&1

log "Системные пакеты установлены"

# ---- 2. ПОЛЬЗОВАТЕЛЬ ----
if ! id -u leadhunter >/dev/null 2>&1; then
  useradd -m -s /bin/bash leadhunter
  log "Пользователь leadhunter создан"
else
  log "Пользователь leadhunter уже существует"
fi

# ---- 3. POSTGRESQL ----
systemctl enable postgresql
systemctl start postgresql

# Создаём пользователя и БД
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='leadhunter'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER leadhunter WITH PASSWORD '${POSTGRES_PASSWORD}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='leadhunter'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE leadhunter OWNER leadhunter;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE leadhunter TO leadhunter;"
log "PostgreSQL настроен (БД: leadhunter)"

# ---- 4. КЛОНИРОВАНИЕ РЕПОЗИТОРИЯ ----
su - leadhunter -c "
  git clone ${REPO_URL} ~/lead-hunter 2>/dev/null || true
  cd ~/lead-hunter
  git pull origin main 2>/dev/null || true
"
log "Репозиторий клонирован"

# ---- 5. ВИРТУАЛЬНОЕ ОКРУЖЕНИЕ И ЗАВИСИМОСТИ ----
su - leadhunter -c "
  cd ~/lead-hunter
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  pip install psycopg2-binary -q
"
log "Зависимости установлены"

# ---- 6. КОНФИГУРАЦИЯ .env ----
DATABASE_URL="postgresql+asyncpg://leadhunter:${POSTGRES_PASSWORD}@localhost:5432/leadhunter"

su - leadhunter -c "
  cd ~/lead-hunter
  cat > .env << 'ENVEOF'
# === Lead Hunter Environment ===

# Telegram API (https://my.telegram.org)
TELEGRAM_API_ID=${TELEGRAM_API_ID}
TELEGRAM_API_HASH=${TELEGRAM_API_HASH}

# Telegram Bot
BOT_TOKEN=${BOT_TOKEN}
OWNER_CHAT_ID=${OWNER_CHAT_ID}

# AI
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_MODEL=${OPENAI_MODEL}
OPENAI_BASE_URL=https://api.openai.com/v1

# PostgreSQL
DATABASE_URL=${DATABASE_URL}
POSTGRES_USER=leadhunter
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=leadhunter

# Web
WEB_HOST=0.0.0.0
WEB_PORT=8000

# Monitoring
MIN_LEAD_SCORE=${MIN_LEAD_SCORE}
DEDUP_DAYS=90
ENVEOF
"
log ".env создан"

# ---- 7. ИНИЦИАЛИЗАЦИЯ БД ----
su - leadhunter -c "
  cd ~/lead-hunter
  source venv/bin/activate
  python -m app.db_init
"
log "База данных инициализирована"

# ---- 8. СОЗДАНИЕ СЕССИИ TELEGRAM ----
# После первого запуска нужно будет авторизовать Telegram аккаунт
# через веб-панель: Настройки → Telegram сессия

# ---- 9. SYSTEMD СЕРВИС ----
cat > /etc/systemd/system/leadhunter.service << 'SVCEOF'
[Unit]
Description=Lead Hunter CRM
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=leadhunter
WorkingDirectory=/home/leadhunter/lead-hunter
ExecStart=/home/leadhunter/lead-hunter/venv/bin/python -m app.main
Restart=always
RestartSec=10
Environment=PATH=/home/leadhunter/lead-hunter/venv/bin:/usr/local/bin:/usr/bin
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable leadhunter
systemctl start leadhunter
log "Сервис leadhunter запущен"

# Ждём пока сервер поднимется
sleep 5

# ---- 10. NGINX ----
NGINX_CONF="/etc/nginx/sites-available/leadhunter"
if [ -n "$DOMAIN" ]; then
  SERVER_NAME="${DOMAIN} www.${DOMAIN}"
else
  SERVER_NAME="_"
fi

cat > ${NGINX_CONF} << NGINXEOF
server {
    listen 80;
    server_name ${SERVER_NAME};

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
    }

    location /static/ {
        alias /home/leadhunter/lead-hunter/app/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/leadhunter /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
log "Nginx настроен"

# ---- 11. SSL (если указан домен) ----
if [ -n "$DOMAIN" ]; then
  certbot --nginx -d "${DOMAIN}" -d "www.${DOMAIN}" --non-interactive --agree-tos --email "admin@${DOMAIN}" || true
  log "SSL сертификат получен"
else
  warn "Домен не указан — SSL не настроен. Доступ по HTTP: //<IP>"
fi

# ---- 12. FAIL2BAN (защита SSH) ----
apt-get install -y -qq fail2ban > /dev/null 2>&1
systemctl enable fail2ban
log "Fail2ban установлен"

# ---- ИТОГО ----
echo ""
echo "============================================================"
log "Lead Hunter развёрнут!"
echo ""
IP=$(hostname -I | awk '{print $1}')
if [ -n "$DOMAIN" ]; then
  echo "  Веб-панель: https://${DOMAIN}"
else
  echo "  Веб-панель: http://${IP}"
fi
echo "  Логин: admin"
echo "  Пароль: admin123"
echo ""
echo "  Логи: sudo journalctl -u leadhunter -f"
echo "  Перезапуск: sudo systemctl restart leadhunter"
echo "============================================================"
echo ""
echo "  СЛЕДУЮЩИЕ ШАГИ:"
echo "  1. Откройте веб-панель и смените пароль admin"
echo "  2. Настройте Telegram API (Настройки → Telegram)"
echo "  3. Добавьте чаты для мониторинга"
echo "============================================================"
