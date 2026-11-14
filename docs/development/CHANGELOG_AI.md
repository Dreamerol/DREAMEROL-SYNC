# AI Development Changelog

## 2026-11-13 17:14 - Web dashboard Auth Broker интеграция

- **Извършена задача:** Добавен е server-side Auth Broker login към web dashboard-а, с HTTP-only session cookie, автоматично preview след вход и запазен ръчен PAT fallback.
- **Променени файлове:** `.env.example`, `README.md`, `api/app.py`, `api/routes.py`, `auth_broker/routes.py`, `web/app.js`, `web/index.html`, `web/style.css`; нови `api/auth.py`, `tests/test_web_auth.py`.
- **Причина:** Браузърът не трябва да получава GitHub token, а интеграцията трябва да запази съществуващия preview/sync workflow и ръчния token fallback.
- **Изпълнени тестове:** `node --check web/app.js`; `python -m pytest -q`; `git diff --check`.
- **Резултат:** `node --check` успешен; pytest `339 passed, 1 warning`; `git diff --check` само CRLF/LF предупреждения без whitespace грешки.
- **Оставащи проблеми:** Реалният GitHub login не е тестван; сесиите са в паметта на един процес и не се споделят между workers; промените не са commit-нати.

## 2026-11-13 17:27 - Локален smoke test на Broker и dashboard

- **Извършена задача:** Стартирани са Broker на порт 8000 и dashboard на порт 8001 и е проверен локалният auth flow без завършване на реален GitHub login.
- **Променени файлове:** `LOG_13-11-26.md` (само лог).
- **Причина:** Да се провери реалната връзка между двете услуги, cookie session-а и logout поведението извън unit тестовете.
- **Изпълнени тестове:** Ръчен smoke test с `Invoke-WebRequest`: начална страница, `POST /api/auth/start`, `GET /api/auth/status`, `POST /api/auth/logout`, повторен status след logout.
- **Резултат:** Началната страница се зарежда; auth start връща 200 и authorization URL; статусът е `PENDING`; logout връща `SIGNED_OUT`; след logout status връща 401; двата сървъра стартират без грешки.
- **Оставащи проблеми:** Реалният GitHub login е блокиран от placeholder стойности за GitHub App client ID/secret и callback URL `broker.example.com`.

## 2026-11-13 17:36 - AI development logging и checkpoint документация

- **Извършена задача:** Добавени са правилата за AI development logging и са създадени/обновени файловете за changelog, текущ статус, следващи стъпки и архитектурни решения.
- **Променени файлове:** `AGENTS.md`, `docs/development/CHANGELOG_AI.md`, `CURRENT_STATUS.md`, `NEXT_STEPS.md`, `DECISIONS.md`, `LOG_13-11-26.md`.
- **Причина:** Да има постоянна, точна и проверяема история на разработката, включително checkpoint при прекъсване.
- **Изпълнени тестове:** Няма изпълнени тестове; промяната е само в документация и правила.
- **Резултат:** Документацията е създадена/обновена; не се твърди успешен тест за тази стъпка.
- **Оставащи проблеми:** Реалният GitHub login остава за тест с валидни credentials и публичен HTTPS callback.

## 2026-11-13 17:39 - Финален test gate преди commit

- **Извършена задача:** Изпълнен е финалният пълен test suite преди евентуален commit на Auth Broker интеграцията.
- **Променени файлове:** `CURRENT_STATUS.md`, `docs/development/CHANGELOG_AI.md`, `LOG_13-11-26.md` (само статус и логове).
- **Причина:** Да се потвърди, че текущото състояние на кода е стабилно преди commit.
- **Изпълнени тестове:** `python -m pytest -q`.
- **Резултат:** `339 passed, 1 warning in 1.20s`.
- **Оставащи проблеми:** Има един `StarletteDeprecationWarning` за `httpx`/`starlette.testclient`; реален GitHub login не е тестван. Промените не са commit-нати.

## 2026-11-14 02:55 - Проверка на текущото състояние

- **Извършена задача:** Потвърдено е текущото състояние на клона и работното дърво преди следваща стъпка.
- **Променени файлове:** `LOG_14-11-26.md`, `docs/development/CHANGELOG_AI.md` (само логове).
- **Причина:** Да се установи дали неcommit-натата Auth Broker web интеграция е стабилна и какви са реалните следващи стъпки.
- **Изпълнени тестове:** `python -m pytest -q -p no:cacheprovider`.
- **Резултат:** `339 passed, 1 warning in 2.11s`; няма нови кодови промени.
- **Оставащи проблеми:** Промените не са commit-нати; реален GitHub login не е тестван; сесиите на dashboard-а са само в паметта на един процес.

## 2026-11-14 03:02 - Одит на Keyring retry и regression test

- **Извършена задача:** Направен е одит на Keyring retry поведението на CLI и е добавен regression тест за многократен retry без повторен Broker poll.
- **Променени файлове:** `tests/test_auth_cli.py`, `CURRENT_STATUS.md`, `NEXT_STEPS.md`, `LOG_14-11-26.md`.
- **Причина:** Да се потвърди, че при Keyring грешка CLI-ят не излиза веднага и не праща ACK, а позволява безопасен повторен опит в същата транзакция, както изисква спецификацията.
- **Изпълнени тестове:** `python -m pytest tests/test_auth_cli.py -q -k "keyring or login_stores"`; `python -m pytest -q -p no:cacheprovider`.
- **Резултат:** Целевите тестове `5 passed`; пълният suite `340 passed, 1 warning in 1.26s`.
- **Оставащи проблеми:** Реалният GitHub login остава блокиран от placeholder credentials; промените не са commit-нати. Keyring retry не е пречка за smoke test.

## 2026-11-14 03:05 - Одит на .env и GitHub App конфигурацията

- **Извършена задача:** Проверени са `.env`, `.gitignore`, Broker конфигурацията и callback route-ът преди реален GitHub smoke test.
- **Променени файлове:** `LOG_14-11-26.md`, `CURRENT_STATUS.md`, `NEXT_STEPS.md` (само документация и статус).
- **Причина:** Да се установи какво липсва за реален login и да не се стартира тест с невалидна конфигурация.
- **Изпълнени тестове:** Няма изпълнени тестове; направени са статични проверки на конфигурацията и `.gitignore`.
- **Резултат:** `.env` е игнориран от Git; GitHub App credentials и callback URL са placeholder; изисква се фиксиран HTTPS URL без wildcard/query/fragment и път `/auth/callback`.
- **Оставащи проблеми:** Нужни са реални GitHub App credentials и публичен HTTPS callback; локален `127.0.0.1` не може да се използва директно от GitHub.

## 2026-11-14 03:13 - Commit на Auth Broker web интеграцията

- **Извършена задача:** Прегледан е diff-ът и Auth Broker web интеграцията е commit-ната.
- **Променени файлове:** Всички файлове от интеграцията, документацията и тестовете; commit `92f5581`.
- **Причина:** Да се фиксира стабилен checkpoint преди реален GitHub smoke test с външни credentials.
- **Изпълнени тестове:** `python -m pytest -q -p no:cacheprovider` преди commit-а.
- **Резултат:** `340 passed, 1 warning`; commit `92f5581` с 18 файла и 1336 insertions. `.env` не е включен.
- **Оставащи проблеми:** Реалният GitHub login остава блокиран от placeholder credentials; последващите лог/статус записи са неcommit-нати.

## 2026-11-14 03:16 - Автоматично зареждане на .env в Broker

- **Извършена задача:** `create_app()` вече зарежда `.env` автоматично, когато не е подаден явен `BrokerConfig`.
- **Променени файлове:** `auth_broker/app.py`, `tests/test_auth_broker_api.py`, `README.md`, `DECISIONS.md`, `CURRENT_STATUS.md`, `NEXT_STEPS.md`, `LOG_14-11-26.md`.
- **Причина:** Стартирането с `uvicorn auth_broker.app:create_app --factory` да работи директно с локалния `.env` без ръчно експортиране на променливи.
- **Изпълнени тестове:** `python -m pytest tests/test_auth_broker_api.py -q -k dotenv`; `python -m pytest -q -p no:cacheprovider`.
- **Резултат:** Целевите тестове `2 passed`; пълният suite `342 passed, 1 warning in 1.60s`.
- **Оставащи проблеми:** Промяната не е commit-ната; реален GitHub login остава блокиран от placeholder credentials.

