# CURRENT_STATUS

## Последна завършена задача
- 2026-11-14 03:16: Broker зарежда `.env` автоматично при фабрично стартиране без явен конфиг; добавени са тестове.
- Преди това: одит на `.env` и GitHub App конфигурацията (2026-11-14 03:05), одит на Keyring retry и regression тест (2026-11-14 03:02), локален smoke test на Broker и dashboard (2026-11-13 17:27) и завършена Auth Broker интеграция (2026-11-13 17:14).

## Текущо състояние
- Клон: `feature/github-app-auth`; HEAD: `4afc0cf`.
- Auth Broker интеграцията е реализирана и локално проверена.
- `python -m pytest -q`: `342 passed, 1 warning` (2026-11-14 03:16).
- `node --check web/app.js`: успешен.
- Keyring retry: при грешка CLI-ят не праща ACK, предлага безопасен повторен опит и при успех ACK-ва; покрито с тестове.
- Broker зарежда `.env` при `create_app()` без явен конфиг; при явен конфиг env файлът не се чете.
- Smoke test: dashboard, auth start, pending status, logout и 401 след logout работят.
- Интеграцията е commit-ната като `92f5581`; логовете са commit-нати като `4afc0cf`.

## Незавършени промени
- Няма незавършен код по интеграцията.
- Не е извършен реален GitHub login, защото `.env` съдържа placeholder стойности за GitHub App credentials и callback URL.
- `.env` auto-load промяната и тестовете ѝ са неcommit-нати.

## Известни грешки
- Няма потвърдени runtime грешки.
- Има един `StarletteDeprecationWarning` за `httpx`/`starlette.testclient`; тестовете минават.
- Сесиите са в паметта на един процес и не се споделят между workers.

## Точна следваща стъпка
1. Прегледай и commit-ни `.env` auto-load промяната и тестовете след изрично одобрение.
2. Замени placeholder стойностите в `.env` с реални `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` и публичен HTTPS `BROKER_CALLBACK_URL`, съвпадащ с GitHub App настройките.
3. Рестартирай Broker на 8000 и dashboard на 8001 и изпълни реален GitHub login, preview и sync тест.
