# DECISIONS

## Съществуващи архитектурни решения

### 2026-11-13 - Server-side Auth Broker сесии
- **Решение:** `WebAuthManager` пази Broker polling secret-а и GitHub credential-а в паметта на API процеса.
- **Причина:** Браузърът не трябва да получава GitHub token; cookie-то съдържа само непрозрачен session ID.
- **Последица:** Сесиите не се споделят между workers и се губят при рестарт.

### 2026-11-13 - HttpOnly session cookie
- **Решение:** Браузърът получава `dreamerol_session` с `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=600` и `Secure` при HTTPS.
- **Причина:** Намалява риска от кражба на сесията чрез JavaScript и ограничава CSRF векторите.
- **Последица:** Session TTL е 10 минути; при нужда от по-дълги сесии TTL трябва да се промени съзнателно.

### 2026-11-13 - Валидация преди Broker ACK
- **Решение:** Credential-ът от Broker се валидира чрез `GitHubClient.get_authenticated_user()` преди да бъде запазен; Broker ACK е best-effort след успешна валидация.
- **Причина:** Невалиден GitHub credential не трябва да се приема или потвърждава.
- **Последица:** При неуспешен ACK credential-ът може да остане валиден в сесията, но Broker транзакцията не е потвърдена.

### 2026-11-13 - Bearer token с приоритет пред cookie
- **Решение:** `/api/sync` приема `Authorization: Bearer <token>` или session cookie; при наличие на Bearer той има приоритет.
- **Причина:** Запазва съвместимостта с ръчния PAT fallback и позволява явно избиране на credential.
- **Последица:** Клиент с невалиден Bearer не fallback-ва автоматично към cookie.

### 2026-11-13 - Ръчен PAT fallback
- **Решение:** Dashboard-ът запазва форма за ръчен GitHub PAT, когато Broker не е достъпен.
- **Причина:** Локалната разработка и среди без GitHub App не трябва да остават без вход.
- **Последица:** PAT се пази само в паметта на браузъра и се изпраща към локалния API.

## Нови решения в текущия етап

### 2026-11-14 - Broker зарежда `.env` при фабрично стартиране
- **Решение:** `create_app()` вика `load_dotenv()` само когато не е подаден явен `BrokerConfig`.
- **Причина:** `uvicorn auth_broker.app:create_app --factory` да работи директно с локален `.env`, без ръчно експортиране на променливите.
- **Последица:** При явен конфиг `.env` не се чете; тестовете и вграждането с собствен конфиг остават изолирани.

### 2026-11-14 - Security baseline за web auth API
- **Решение:** Всички `/api/` отговори са `Cache-Control: no-store`; state-changing endpoints приемат само `POST`; сесийната cookie е `HttpOnly`, `SameSite=Lax`, `Secure` при HTTPS; невалиден Bearer token не fallback-ва към cookie.
- **Причина:** Намалява CSRF, cache leakage и случайно смесване на credential източници.
- **Последица:** Няма отделен CSRF token във v1; защитата разчита на `SameSite=Lax` и POST-only. При публично exposure може да се добави token. Зад HTTPS proxy е нужен `--proxy-headers`, за да се маркира cookie-то като `Secure`.

### 2026-11-14 - Broker зад локален cloudflared
- **Решение:** `BROKER_TRUSTED_PROXIES=127.0.0.1,::1` за локален cloudflared tunnel; всички `/auth/` отговори получават `no-store`, `Pragma: no-cache`, `Referrer-Policy: no-referrer` и `X-Content-Type-Options: nosniff`.
- **Причина:** Правилен client IP за rate limiting и предотвратява caching на auth отговори.
- **Последица:** `X-Forwarded-For` се доверява само от localhost; при друг proxy адресът трябва да се обнови.
