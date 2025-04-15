AromaInc Telegram Bot
📦 Установка и запуск бота на сервере с использованием pm2
🧠 Предварительные требования
Установлен Python 3

Установлен git

Установлен nodejs и npm

Доступ к серверу по SSH

🚀 Шаги установки

# Подключаемся к серверу

```bash
ssh root@45.93.201.75
```

# Обновляем систему

```bash
sudo apt update
sudo apt upgrade -y
```

# Устанавливаем необходимые зависимости

```bash
sudo apt install -y git python3 python3-pip python3-venv
```

# Клонируем репозиторий бота

```bash
mkdir AromaInc
cd AromaInc/
git clone https://github.com/f1nderas/bot-python.git .
```

# Устанавливаем PyTorch (CPU версия)

```bash
pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

# Устанавливаем Node.js и pm2

```bash
sudo apt install nodejs
sudo apt install npm
npm install pm2 -g
```

# Создаём и активируем виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate
```

# Устанавливаем зависимости проекта

```bash
pip install -r requirements.txt
pip install nltk
```

⚙️ Запуск бота с помощью pm2

# Убедитесь, что вы в активированном виртуальном окружении (source venv/bin/activate)

# Добавьте запуск бота в pm2 (предположим, основной файл называется bot.py)

pm2 start bot.py --interpreter venv/bin/python3 --name aroma-bot

# Сохраняем конфигурацию pm2

pm2 save

# Устанавливаем автозапуск pm2 при старте системы

pm2 startup

# Следите за логами

```bash
pm2 logs aroma-bot
```

🔄 Обновление кода

```bash
git pull
pm2 restart aroma-bot
```

🛑 Управление процессом

```bash
pm2 stop aroma-bot      # Остановить
pm2 restart aroma-bot   # Перезапустить
pm2 delete aroma-bot    # Удалить из списка
```

✅ Полезные команды

```bash
pm2 list                # Список всех процессов
pm2 logs aroma-bot      # Логи конкретного процесса
pm2 monit               # Мониторинг
```

Чтобы выйти из виртуального окружения Python, просто введи команду:

```bash
deactivate
```
