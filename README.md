# Второй мозг (Second Brain Bot)

Telegram-бот-помощник: отвечает на вопросы, разбирает идеи, проверяет текст и может
«очеловечить» его. Без режимов — пользователь просто пишет задачу обычным сообщением.

## Возможности

- Одно окно общения: пишешь задачу → получаешь ответ.
- «✨ Очеловечить текст» — переписывает текст живее и естественнее (3 уровня: LIGHT / NORMAL / STRONG).
- «🆕 Новая тема» — сброс контекста.
- Реферальные бонусы: +20 пригласившему / +10 приглашённому после 3 запросов.
- Дневной лимит запросов (по умолчанию 10) + бонусные запросы.

## Быстрый старт (локально)

```bash
# 1. Скопируй .env.example → .env и заполни
cp .env.example .env

# 2. Установи зависимости
pip install -r requirements.txt

# 3. Запусти
python bot.py
```

Локально нужны переменные: `BOT_TOKEN`, `OPENROUTER_API_KEY`, `DATABASE_URL`.
Для работы webhook задай `WEBHOOK_URL` (внешний URL, на который Telegram будет слать updates).

## Деплой на Railway

1. Импортируй репозиторий в Railway (создаётся Web Service из `Dockerfile`).
2. Добавь переменные окружения (Railway → сервис `second-brain-bot` → Variables):
   - `BOT_TOKEN` — токен от @BotFather
   - `OPENROUTER_API_KEY` — ключ OpenRouter
   - `DATABASE_URL` — Railway подставляет автоматически при подключении PostgreSQL-сервиса
   - `BOT_USERNAME` — username бота без `@`
   - `WEBHOOK_URL` — `https://<твой-сервис>.up.railway.app`
3. Подключи PostgreSQL-сервис (`second-brain-db`). Таблицы создаются автоматически при старте.
4. После деплоя статус сервиса должен стать `Online`.

## Архитектура

```
bot.py          — точка входа: aiohttp (GET / , /health, POST /webhook) + Telegram app
handlers.py     — обработчики сообщений и callback-кнопок (single-chat, без FSM)
llm_client.py   — OpenRouter клиент (JSON-режим для ответов + plain для humanizer), ретраи, timeout
db.py           — PostgreSQL (asyncpg): users, daily_usage, conversation_state, referrals, analytics_events
prompts.py      — системные промпты (основной + humanizer, изолированы друг от друга)
config.py       — конфигурация из env vars (никогда не падает на импорте)
analytics.py    — обёртка над записью событий в БД
models.py       — структуры ответа LLM
```

## Ограничения

- **Лимит**: 10 запросов/день на пользователя (настраивается через `DAILY_LIMIT`).
- **Макс. длина сообщения**: 4000 символов (`MAX_MESSAGE_LENGTH`).
- **Макс. длина текста для humanizer**: 8000 символов (`HUMANIZE_MAX_LENGTH`).
- **История**: последние 6 сообщений для контекста.

## Безопасность

- Все секреты — только в env vars / Railway Variables. `.env` в `.gitignore`.
- Пользователю никогда не показываются traceback, пути, переменные окружения или ключи.
- Webhook проверяет `X-Telegram-Bot-Api-Secret-Token` (если `WEBHOOK_SECRET` задан корректно).
- Системные промпты не содержат секретов; модель физически не имеет к ним доступа.

## Монетизация (будущие планы)

- Freemium: 10 запросов/день бесплатно + бонусы за приглашённых.
- Подписка за расширенные лимиты.

---

Разработано для Telegram. Использует OpenRouter API (gpt-4o-mini).
