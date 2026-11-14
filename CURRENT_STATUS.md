# CURRENT_STATUS

## Последна завършена задача
- 2026-11-14 03:05: одит на `.env` и GitHub App конфигурацията; потвърдени са placeholder стойности и изискванията за HTTPS callback.
- Преди това: одит на Keyring retry и regression тест (2026-11-14 03:02), локален smoke test на Broker и dashboard (2026-11-13 17:27) и завършена Auth Broker интеграция (2026-11-13 17:14).

## Текущо състояние
- Клон: `feature/github-app-auth`; HEAD: `78ae420`.
- Auth Broker интеграцията е реализирана и локално проверена.
- `python -m pytest -q`: `340 passed, 1 warning` (2026-11-14 03:02).
- `node --check web/app.js`: успешен.
- Keyring retry: при грешка CLI-ят не праща ACK, предлага безопасен повторен опит и при успех ACK-ва; покрито с тестове.
- Smoke test: dashboard, auth start, pending status, logout и 401 след logout работят.
- Всички промени по интеграцията и документацията са неcommit-нати.

## Незавършени промени
- Няма незавършен код по интеграцията.
- Не е извършен реален GitHub login, защото `.env` съдържа placeholder стойности за GitHub App credentials и callback URL.
- Не е направен commit.

## Известни грешки
- Няма потвърдени runtime грешки.
- Има един `StarletteDeprecationWarning` за `httpx`/`starlette.testclient`; тестовете минават.
- Сесиите са в паметта на един процес и не се споделят между workers.

## Точна следваща стъпка
1. Замени placeholder стойностите в `.env` с реални `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` и публичен HTTPS `BROKER_CALLBACK_URL`, съвпадащ с GitHub App настройките.
2. Рестартирай Broker на 8000 и dashboard на 8001 и изпълни реален GitHub login, preview и sync тест.
3. Ако няма валидни credentials, прегледай diff-а и commit-ни интеграцията само след изрично одобрение.
