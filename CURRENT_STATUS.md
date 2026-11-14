# CURRENT_STATUS

## Последна завършена задача
- 2026-11-14 03:55: Broker hardening — no-store/Pragma/Referrer-Policy/nosniff за всички `/auth/` отговори и trusted proxy за cloudflared.
- Преди това: smoke-test скриптове (2026-11-14 03:51), cloudflared и HTTPS tunnel (2026-11-14 03:42), commit на security промените (2026-11-14 03:29) и завършена Auth Broker интеграция (2026-11-13 17:14).

## Текущо състояние
- Клон: `feature/github-app-auth`; последни commit-и: `23f1226` (код и скриптове) и `fa4a135` (логове).
- Auth Broker интеграцията е реализирана и локално проверена.
- `python -m pytest -q`: `350 passed, 1 warning` (2026-11-14 03:55).
- `node --check web/app.js`: успешен.
- Keyring retry: при грешка CLI-ят не праща ACK, предлага безопасен повторен опит и при успех ACK-ва; покрито с тестове.
- Broker зарежда `.env` при `create_app()` без явен конфиг; при явен конфиг env файлът не се чете.
- Security: `/api/` отговорите са `no-store`; state-changing endpoints са само POST; има тестове за различни session ID-та, Bearer precedence, невалиден Bearer без fallback и Secure cookie при HTTPS.
- Интеграцията е commit-ната като `92f5581`; логовете като `4afc0cf`; `.env` auto-load като `96dc98b`; security hardening като `be41a0b`; logging follow-up като `4978956`.
- `cloudflared` е инсталиран; активен quick tunnel: `https://sealed-arguments-quality-stays.trycloudflare.com`.
- Broker работи на 8000 (PID 10640), dashboard на 8001 (PID 15192), cloudflared (PID 14768); tunnel callback връща 400 без state, dashboard връща 200.
- Broker връща `Cache-Control: no-store`, `Pragma: no-cache`, `Referrer-Policy: no-referrer` и `X-Content-Type-Options: nosniff` за всички `/auth/` отговори, включително 401/404.
- `BROKER_TRUSTED_PROXIES=127.0.0.1,::1` е зададен; cloudflared client IP се използва за rate limiting.
- `scripts/start-smoke.ps1` стартира трите процеса, обновява `BROKER_CALLBACK_URL`, проверява тунела и записва PID state; `scripts/stop-smoke.ps1` ги спира.

## Незавършени промени
- Няма неcommit-нати промени.
- Не е извършен реален GitHub login, защото `.env` съдържа placeholder стойности за `GITHUB_APP_CLIENT_ID` и `GITHUB_APP_CLIENT_SECRET`.

## Известни грешки
- Няма потвърдени runtime грешки.
- Има един `StarletteDeprecationWarning` за `httpx`/`starlette.testclient`; тестовете минават.
- Сесиите са в паметта на един процес и не се споделят между workers.

## Точна следваща стъпка
1. Задай реални `GITHUB_APP_CLIENT_ID` и `GITHUB_APP_CLIENT_SECRET` в `.env`.
2. Стартирай `.\scripts\start-smoke.ps1 -Force`; скриптът ще създаде нов тунел и ще обнови callback URL.
3. Задай новия callback URL в GitHub App настройките и изпълни реален GitHub login през `http://127.0.0.1:8001`, после preview и sync тест.
4. Ако cloudflared се рестартира, пусни скрипта отново; quick tunnel URL се променя.
