# Авторизация и модельный API: результаты исследования

## Проверенный upstream

Репозиторий: https://github.com/eigent-ai/eigent

Commit: `8f90b99125e790fb4ccebb472dac7e6a6d1972a9` (HEAD при скачивании исходников). Ниже описан именно этот snapshot, а не обещание совместимости с будущими релизами.

## 1. Современный вход — внешний сайт и app token

- `src/pages/loginUtils.ts`: `https://www.eigent.ai/signin?callbackUrl=...`, desktop callback — `eigent://auth/callback`; web callback — `/login` на origin клиента.
- `src/pages/Login.tsx`: получение `token` из callback, сохранение как app token; валидация пользователя через `GET /api/v1/user`, `Authorization: Bearer <token>`.
- В том же Login.tsx есть legacy Stack Auth code exchange и `/api/v1/login-by_stack`. Это не основание считать Stack token и app token взаимозаменяемыми.
- Код MCP OAuth (`src/lib/oauth.ts`) относится к подключениям инструментов, а не к Eigent account login. Здесь он не используется.

Источники:

- https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/src/pages/loginUtils.ts
- https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/src/pages/Login.tsx

## 2. Действующий сайт ограничивает callback

Проверена публичная страница https://www.eigent.ai/signin и подключённый ею JS bundle:

https://www.eigent.ai/_next/static/chunks/e8a1c2ac4b63c810.js

В функции `isAllowedAppRedirect` разрешены:

- `eigent://auth/callback` (с query/fragment);
- протокол `http:`, точный путь `/auth/callback`, hostname `localhost`, `127.0.0.1` или `[::1]`.

В отдельной функции `isAllowedRemoteControlRedirect` есть фиксированные remote-control origins и localhost, но это **другая** схема с `rc_code`, не прямой app token на произвольный домен.

`buildAppRedirectUrl` добавляет `token` и `code` к существующим query-параметрам callback. Это позволяет сохранить случайный `state` localhost-помощника. `ensureAppToken` обменивает Stack access token через same-origin `/api/proxy/login-by_stack`, а не отправляет Stack ключи в произвольный proxy.

Отсюда решение: локальный stdlib-помощник с localhost listener → официальный сайт → проверка `state` → private JSON / HTTPS POST на свой gateway. В приложении нет обхода allowlist, скрейпинга cookies или hardcoded Stack client keys. Если сайт изменит эти правила, помощник потребует обновления.

Публичный JS bundle не является документированным API и его имя может измениться. Настоящий вход с пользовательским аккаунтом при разработке не проводился.

## 3. App token ≠ ключ модельного прокси

`src/store/chatStore.ts`, cloud branch:

1. `GET /api/v1/cloud-models?kind=chat` возвращает каталог: `id`, `model_type`, `model_platform`, `capabilities`.
2. `GET /api/v1/user/key` возвращает `value` (model API key) и `api_url`.
3. В модель передаются `api_key=res.value`, `api_url=res.api_url`, `model_type` и `model_platform` из каталога.

Gateway API_KEY — третий, независимый секрет: он защищает именно наш сервер. Клиентский API_KEY не посылается в Eigent. App token не подставляется вместо `value` модельного прокси.

Источники:

- https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/src/store/chatStore.ts
- https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/src/store/cloudModelStore.ts

## 4. Транспорт зависит от модели

`cloudModelRequestExtraParams` читает `capabilities.request_compatibility.preferred_transport` (`chat_completions` или `responses`). `backend/app/model/model_platform.py` отдельно указывает: cloud Responses для Azure должен использовать OpenAI-compatible `/responses`, а не Azure `/openai/responses`.

Для native Azure Chat выбран путь AzureOpenAI `/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21`. `api_url` берётся из ключевого ответа, не захардкожен. Его path-prefix сохраняется, как у upstream клиента.

Native Gemini и Bedrock потребовали бы отдельного преобразования запросов и стриминга. Они намеренно исключены из каталога gateway, вместо ложного обещания, что любой `model_platform` совместим с OpenAI.

Источники:

- https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/backend/app/model/model_platform.py
- https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/src/store/cloudModelStore.ts

## 5. Prompt Guard — реальное ограничение

`backend/app/agent/agent_model.py` содержит комментарий о Prompt Guard облачного прокси и передаче trusted agent instructions для Responses continuation/tool turns. Поэтому отдельный генеративный gateway может получить отказ на произвольный чат, даже если вход и форматы запросов корректны.

Адаптер не подставляет Eigent system prompts, не выдаёт запрос за другой агент и не обходит проверку. Ответы 403/429 сохраняют HTTP-класс ошибки и краткое объяснение. Полные upstream ошибки скрыты, чтобы случайно не раскрыть секреты.

Источник:

https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/backend/app/agent/agent_model.py

## 6. Refresh и self-hosted server

Публичный `server/app/domains/user/api/login_controller.py` реализует `/user/login`, `/user/refresh`, rotating refresh token. При наличии refresh_token gateway пробует соответствующий `/api/v1/user/refresh`. В callback обычного облачного входа refresh token не обнаружен; долговечность cloud app token и применимость этого refresh endpoint к cloud не подтверждены.

`/user/auto-login` предназначен для локального server, не для получения облачного доступа. Gateway его не вызывает.

Источник:

https://github.com/eigent-ai/eigent/blob/8f90b99125e790fb4ccebb472dac7e6a6d1972a9/server/app/domains/user/api/login_controller.py

## Проверки без аккаунта

- `GET https://dev.eigent.ai/api/v1/user` без credentials: HTTP 200, JSON `code=11`, `Token required`.
- `GET https://dev.eigent.ai/api/v1/cloud-models` без credentials: тот же ответ.
- Страница `/signin` доступна публично.
- Эти ответы подтверждают, что нельзя судить об успехе Eigent API только по HTTP 200. Gateway проверяет также `code`.
- С аккаунтом не выполнялись login, `/user/key`, генерация, refresh или проверка quota/policy.
