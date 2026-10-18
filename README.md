# eigent-to-api

Лёгкий **headless-шлюз к облачным моделям Eigent** с OpenAI-совместимым HTTP API. Без Electron, React, CAMEL, Chromium, Node.js и desktop IDE в контейнере. Python + FastAPI + HTTPX, конфигурация для Railway и других Docker PaaS.

> **Статус: прототип, контрактно протестированный на mock-upstream.** Реальный вход и генерация под аккаунтом Eigent, а также деплой на Railway не выполнялись. Облако может запрещать произвольные промпты через Prompt Guard, ограничивать план/кредиты или менять недокументированные API. Шлюз **не обходит** эти ограничения и не гарантирует, что облачная подписка разрешает использование стороннего API-клиента.
>
> Это **API к моделям**, а не перенос полного Eigent Workforce: браузерные/терминальные инструменты, планировщик и исполнение мультиагентных задач не включены. `tool_calls` передаются клиенту; инструменты исполняет клиент.

## Что реализовано

- `GET /v1/models` — реальные поддерживаемые модели из каталога аккаунта, без выдуманных ID.
- `POST /v1/chat/completions` — обычный ответ и SSE streaming.
- Модели, предпочитающие Responses: преобразование Chat Completions → Responses → Chat Completions, включая текст, function calls, usage и причины завершения.
- `POST /v1/responses` — нативный pass-through для моделей с Responses transport.
- Собственный обязательный `API_KEY` — upstream токены не выдаются клиентам.
- Получение сессии из **`EIGENT_AUTH_URL` при старте**; JSON/token env и сохранённый файл как альтернативы.
- `login.py` — вход через официальный сайт **на твоём компьютере**, без установки IDE; выводит login-ссылку в консоль.
- Проверка сессии, атомарное сохранение, права `0600` на Linux, refresh при наличии `refresh_token`.
- Ограничение размера запросов и параллелизма, таймауты, отсутствие автоматических повторов генерации.
- `/healthz` для запуска PaaS без сессии, `/readyz` для наличия загруженной сессии.

## Важное про вход: почему не просто callback на Railway

В проверенной публичной странице входа Eigent разрешены app-redirect только на `eigent://auth/callback` или `http://localhost:<port>/auth/callback` / loopback IP. Произвольный `https://your-app.up.railway.app/...` не входит в allowlist.

Поэтому удалённый сервер **не запускает браузер и не притворяется localhost твоего ПК**. Рабочая архитектура такая:

1. Один раз запускаешь небольшой `login.py` на своём компьютере.
2. Он печатает ссылку на официальный сайт и слушает только `127.0.0.1` с одноразовым случайным `state`.
3. После твоего входа сайт возвращает Eigent app token в localhost callback.
4. Помощник отправляет сессию на твой gateway через защищённый `/auth/import` **или** сохраняет `auth.json` для `EIGENT_AUTH_URL`.

Свои пароли скрипт не спрашивает и не видит. Он может спросить **API_KEY твоего gateway**, скрытым вводом. Запускай помощник там же, где открыт браузер, не в Railway shell/SSH-сессии на удалённой машине.

Исследованные контракты и источники: [docs/auth-research.md](docs/auth-research.md).

## Railway: быстрый запуск

1. Помести **содержимое** этого проекта в свой GitHub-репозиторий; в Railway выбери **New Project → Deploy from GitHub repo**. `Dockerfile` и `railway.toml` лежат в корне.
2. В Variables задай обязательный `API_KEY`. Сгенерировать на своём ПК:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. В Networking нажми **Generate Domain**. `PORT` предоставляет Railway; `RAILWAY_PUBLIC_DOMAIN` обнаруживается автоматически. Если домен не подхватился, задай `PUBLIC_URL=https://your-app.up.railway.app` и перезапусти deployment.
4. Для сохранения сессии между рестартами подключи **Volume на `/data/session`**. Entrypoint подготовит права смонтированной папки и затем сбросит root до UID/GID 10001. Для PaaS с принудительным non-root UID папка должна уже быть доступна этому UID.
5. Оставь **1 replica / 1 worker**. Состояние refresh и блокировки рассчитаны на один процесс. Healthcheck уже настроен на `/healthz`, он не мешает первому входу.
6. Выполни на **своём ПК**, в папке проекта, Python 3.10+:

   ```sh
   python login.py --server https://your-app.up.railway.app
   ```

   Введи `API_KEY` gateway, открой выданную ссылку и войди в Eigent. Готово: сессия импортирована без копирования токена руками. Можно использовать `--no-browser`, чтобы только напечатать ссылку. В логах PaaS тоже печатается команда запуска помощника, но **не неработающая прямая Railway OAuth-ссылка**.

7. Проверь каталог:

   ```sh
   export API_BASE=https://your-app.up.railway.app
   # API_KEY должен быть задан в твоём локальном окружении.
   curl -fsS "$API_BASE/v1/models" -H "Authorization: Bearer $API_KEY"
   ```

Нет аккаунта/кредитов? Gateway не создаёт их автоматически. Войди в свой действующий аккаунт и проверь разрешённость использования по условиям Eigent.

## Вариант: скачать логин из env URL при старте

На своём ПК:

```sh
python login.py --output auth.json
```

Формат файла:

```json
{"token": "YOUR_EIGENT_APP_TOKEN"}
```

Также принимается `{"access_token":"...","refresh_token":"..."}` или `{"state":{"token":"..."}}` из Zustand authStore. Refresh token необязателен; браузерный callback обычно передаёт только app token.

Размести JSON в **приватном HTTPS-хранилище** с короткоживущей signed URL или Bearer-защитой и задай на PaaS:

```dotenv
EIGENT_AUTH_URL=https://private-storage.example/session.json
# Только если хранилище требует Bearer:
EIGENT_AUTH_URL_BEARER=storage-download-secret
```

Ограничение файла — 64 KiB. Редиректы не выполняются. URL загружается только при старте; `token` проверяется через Eigent `/user`, затем сохраняется локально. При ошибке источник не подменяется незаметно: `/healthz` остаётся 200, `/readyz` — 503; можно исправить env/redeploy или использовать `/auth/import`.

**Нельзя** размещать сессию в public gist, публичном GitHub-репозитории, тикете или логах. Signed URL тоже является секретом. Удали локальную копию, если она больше не нужна.

Приоритет источников при запуске:

1. `EIGENT_AUTH_URL`
2. `EIGENT_AUTH_JSON`
3. `EIGENT_TOKEN`
4. `AUTH_FILE`
5. Нет сессии → сервер ждёт импорт, модели возвращают 503.

**После успешного импорта убери устаревшие env-источники**, если хочешь загружать свежую сессию с Volume: env иначе перезапишет файл на следующем старте. Signed URL должна быть ещё действительна при каждом рестарте либо убрана после первого импорта.

## Docker / другой PaaS

```sh
cp .env.example .env
# Отредактируй API_KEY, при необходимости EIGENT_AUTH_URL.
docker compose up --build -d
```

Локальный API: `http://localhost:8080/v1`. Для загрузки через `login.py --server` нужен публичный HTTPS origin; при локальной разработке проще сохранить `auth.json` и использовать `EIGENT_AUTH_JSON`, `EIGENT_TOKEN` или файл в volume.

Другому PaaS нужны Dockerfile, входящий порт `$PORT`, healthcheck `/healthz`, постоянная writable-папка `/data/session` и одна реплика. Для SSE отключи буферизацию reverse proxy и выставь достаточный idle timeout у платформы: приложение не может отменить лимиты внешнего балансировщика.

### Без Docker

Python 3.12 рекомендуется:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export API_KEY='your-random-secret-at-least-24-characters'
export AUTH_FILE="$PWD/session/auth.json"
# Сначала python login.py --output session/auth.json в отдельном терминале.
python -m app
```

`.env` автоматически читает Docker Compose, **не** `python -m app`. Для локального Python экспортируй переменные сам. `login.py` не требует установки библиотек; на Windows для защищённого JSON используй личную папку с корректными ACL (POSIX `0600` не заменяет Windows ACL).

## Использование из OpenAI SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["API_BASE"].rstrip("/") + "/v1",
    api_key=os.environ["API_KEY"],
    max_retries=0,  # Не повторять потенциально платные генерации автоматически.
    timeout=300,
)
models = client.models.list().data
if not models:
    raise RuntimeError("Нет поддерживаемых моделей для этого аккаунта")
model = models[0].id  # Или явно выбери ID из /v1/models.

result = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Привет!"}],
)
print(result.choices[0].message.content)

stream = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Ответь кратко"}],
    stream=True,
    stream_options={"include_usage": True},
)
for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

OpenAI SDK ставится отдельно (`pip install openai`). Если прокси Eigent вернул 403 из-за Prompt Guard, корректный формат OpenAI **не** означает разрешения обойти ограничение. API вернёт ошибку, а не будет подставлять чужой агентный system prompt.

## Поддержка протоколов и ограничения

| Сценарий | Поддержка |
|---|---|
| OpenAI / OpenAI-compatible / LiteLLM / OpenRouter chat transport из каталога Eigent | JSON и SSE pass-through |
| Azure chat transport | deployment URL и `api-version=2024-10-21`, как в upstream |
| Модель с `preferred_transport=responses` | мост Chat Completions и нативный `/v1/responses` |
| Function tools, их результаты и streaming arguments | Передача клиенту; не локальное исполнение |
| Responses bridge: текст, user image_url, response_format, reasoning_effort, лимит output tokens | Реализовано |
| Нативные Gemini / Bedrock / Anthropic protocol | **Не реализовано**, эти строки не попадают в `/v1/models` |
| `n > 1`, logprobs, audio и неизвестные поля для Responses bridge | 400, не тихая потеря параметров |
| `/v1/embeddings`, Files, Assistants, realtime, batch | Нет |
| Browser/terminal tools и полноценный Eigent agent runtime | Нет |

Сырые SSE-потоки pass-through сохраняют model/id upstream; обычные JSON-ответы и Responses→Chat bridge используют выбранный публичный ID модели. Responses reasoning/annotations и произвольные built-in tools в Chat bridge не эмулируются. Не обещается полная эквивалентность всех API OpenAI.

Сервис использует внутренние контракты приложения, не официальный стабильный API Eigent. Cloud-часть `/user/key` не полностью представлена в публичном self-hosted server. Вход в локальный server — другой механизм; его `/auto-login` здесь намеренно не используется.

## Обслуживание и безопасность

- `GET /auth/status` — требует gateway Bearer `API_KEY`, возвращает только наличие сессии/refresh.
- `POST /auth/import` — тот же Bearer + JSON сессии; проверяет `/api/v1/user` до замены.
- `GET /readyz` проверяет **наличие** загруженной сессии; это не постоянная онлайн-проверка её срока/квот. `/v1/models` проверяет доступ к каталогу (кэш 60 секунд).
- Ключ модели кешируется на 60 секунд; на 401 кэш сбрасывается, генерация не переигрывается.
- Refresh выполняется только при наличии `refresh_token`, по `/api/v1/user/refresh`, со сменой сохранённого refresh token. Этот контракт найден в публичном server; работоспособность именно в cloud не подтверждена. Если refresh отсутствует/не принят — повторный `login.py`.
- Запускай через `python -m app` / Docker entrypoint: access logs выключены. Не включай debug HTTP logging и трассировку секретных заголовков/тел запросов на PaaS/APM. Запрос `/auth/import` не передаёт токен в URL.
- Volume хранит токен открытым текстом с ограниченными правами; настрой защиту/бэкапы Volume и доступ к Railway. Поставщик PaaS может иметь доступ к этим данным.
- Нет пула аккаунтов, автоматических регистраций, обхода квот, CAPTCHA или Prompt Guard. Один gateway = одна сессия и один административный ключ. Не используй как публичный многопользовательский сервис без отдельного auth/rate-limit слоя.
- `EIGENT_API_BASE` — доверенная административная настройка; не указывай посторонний сервер, ему могут уйти данные сессии. Помощник входа использует фиксированный официальный сайт.

## Тесты

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

Проверено 22 автоматических теста (Python 3.13 в среде разработки), включая реальный локальный TCP HTTP/SSE и отмену запроса. Тесты используют mock Eigent upstream: OpenAI SDK JSON/SSE, Responses bridge, function calls, Azure URL, импорт/загрузка сессии, refresh, localhost state и одноразовость, лимиты, ошибки и отсутствие утечек ключей. Это **не** e2e с платным аккаунтом. Docker build добавлен в GitHub Actions, но локально в среде сборки архивного проекта Docker не был доступен.

## Лицензия

Apache-2.0. Независимая реализация адаптера по публичным контрактам; не официальный продукт Eigent. Источники и проверенный upstream commit — в `docs/auth-research.md`.
