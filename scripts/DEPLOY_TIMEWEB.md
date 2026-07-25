# Деплой Lead Hunter на timeweb.cloud

## Быстрый старт (5 минут)

### Шаг 1: Подготовка репозитория

Загрузите проект на GitHub:

```bash
cd C:\Users\xx007\Desktop\lead-hunter

# Создайте .gitignore если нет
echo -e "venv/\n__pycache__/\n*.pyc\n.env\n*.db\nsessions/\nserver_pid.txt\n*.log" > .gitignore

git init
git add .
git commit -m "deploy to timeweb"
git remote add origin git@github.com:ВАШ-НИК/lead-hunter.git
git push -u origin main
```

### Шаг 2: Заказ сервера на timeweb.cloud

1. **VDS** → Заказать сервер
2. **ОС**: Ubuntu 22.04
3. **Конфигурация**: 1 vCPU / 1 GB RAM (минимум) или 2 GB RAM (рекомендуется)
4. **Регион**: Москва или Санкт-Петербург
5. При создании сервера перейдите во вкладку **Cloud-init**
6. Вставьте содержимое файла `scripts/cloud-init.sh`

### Шаг 3: Настройка скрипта

Перед вставкой в cloud-init **измените** переменные в начале файла:

```bash
REPO_URL="https://github.com/ВАШ-НИК/lead-hunter.git"
POSTGRES_PASSWORD="ваш_надёжный_пароль"

# Telegram (получить на https://my.telegram.org)
TELEGRAM_API_ID="12345678"
TELEGRAM_API_HASH="abcdef1234567890"

# Telegram Bot (получить у @BotFather)
BOT_TOKEN="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
OWNER_CHAT_ID="123456789"

# OpenAI (опционально)
OPENAI_API_KEY="sk-..."
```

### Шаг 4: Деплой

1. Нажмите **Создать сервер** в timeweb.cloud
2. Дождитесь завершения cloud-init (~5 минут)
3. Откройте IP-адрес сервера в браузере: `http://<IP_СЕРВЕРА>`
4. Логин: `admin` / Пароль: `admin123`

### Шаг 5: Настройка

1. **Смените пароль** admin (Настройки → профиль)
2. **Telegram API**: Настройки → Telegram → введите API ID и Hash
3. **Авторизация**: нажмите "Подключить Telegram" и введите код
4. **Добавьте чаты** для мониторинга

---

## Управление

```bash
# Подключиться к серверу
ssh root@<IP>

# Логи
sudo journalctl -u leadhunter -f

# Перезапуск
sudo systemctl restart leadhunter

# Остановка
sudo systemctl stop leadhunter

# Статус
sudo systemctl status leadhunter
```

## Обновление

```bash
# На сервере
su - leadhunter
cd ~/lead-hunter
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
exit

# Перезапуск
sudo systemctl restart leadhunter
```

## Домен + SSL

После настройки домена:

```bash
# Пропишите A-запись: ваш-домен.ru → IP сервера
sudo certbot --nginx -d ваш-домен.ru -d www.ваш-домен.ru
```

## Бэкапы

```bash
# Бэкап БД
su - leadhunter -c "pg_dump leadhunter > ~/backup_$(date +%Y%m%d).sql"

# Бэкап .env и sessions
cp ~/lead-hunter/.env ~/backup/
cp -r ~/lead-hunter/telegram_sessions/ ~/backup/
```

## Требования

- Python 3.11+
- PostgreSQL 14+
- Nginx
- ~500 MB RAM
- 2 GB диска
