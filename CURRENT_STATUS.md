# CURRENT_STATUS

## Последна завършена задача
- 2026-11-14 03:21: security преглед на web auth flow; добавен `no-store` middleware и тестове за CSRF/session fixation/Bearer поведение.
- Преди това: Broker `.env` auto-load (2026-11-14 03:16), одит на `.env` и GitHub App конфигурацията (2026-11-14 03:05), одит на Keyring retry (2026-11-14 03:02) и завършена Auth Broker интеграция (2026-11-13 17:14).

## Текущо състояние
- Клон: `feature/github-app-auth`; HEAD: `96dc98b`.
- Auth Broker интеграцията е реализирана и локално проверена.
- `python -m pytest -q`: `349 passed, 1 warning` (2026-11-14 03:21).
- `node --check web/app.js`: успешен.
- Keyring retry: при грешка CLI-ят не праща ACK, предлага безопасен повторен опит и при успех ACK-ва; покрито с тестове.
- Broker зарежда `.env` при `create_app()` без явен конфиг; при явен конфиг env файлът не се чете.
- Security: `/api/` отговорите са `no-store`; state-changing endpoints са само POST; има тестове за различни session ID-та, Bearer precedence, невалиден Bearer без fallback и Secure cookie при HTTPS.
- Интеграцията е commit-ната като `92f5581`; логовете като `4afc0cf`; `.env` auto-load като `96dc98b`.

## Незавършени промени
- Няма незавършен код по интеграцията.
- Не е извършен реален GitHub login, защото `.env` съдържа placeholder стойности за GitHub App credentials и callback URL.
- Security промяната (`api/app.py`, `tests/test_web_auth.py`) и документацията към нея са неcommit-нати.

## Известни грешки
- Няма потвърдени runtime грешки.
- Има един `StarletteDeprecationWarning` за `httpx`/`starlette.testclient`; тестовете минават.
- Сесиите са в паметта на един процес и не се споделят между workers.

## Точна следваща стъпка
1. Прегледай и commit-ни security промяната и тестовете след изрично одобрение.
2. Замени placeholder стойностите в `.env` с реални `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` и публичен HTTPS `BROKER_CALLBACK_URL`, съвпадащ с GitHub App настройките.
3. Рестартирай Broker на 8000 и dashboard на 8001 и изпълни реален GitHub login, preview и sync тест.
