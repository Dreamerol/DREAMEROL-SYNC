# NEXT_STEPS

## Приоритет 1 - Реален GitHub smoke test
- Стъпка 1 е завършена: `.env` и конфигурацията са проверени; открити са placeholder credentials.
- Keyring retry е потвърден с тестове и не блокира smoke test-а.
- Замени placeholder стойностите в `.env` с реални GitHub App credentials.
- Задай публичен HTTPS `BROKER_CALLBACK_URL`, който съвпада с callback URL в GitHub App настройките; пътят трябва да е `/auth/callback`.
- За локален тест използвай HTTPS tunnel към порт 8000, защото GitHub не може да вика `127.0.0.1`.
- Стартирай Broker на порт 8000 и dashboard на порт 8001.
- Провери: login, връщане към dashboard, preview на тестов fork, sync на fork със статус `behind` и logout.
- Broker вече зарежда `.env` автоматично при фабрично стартиране без явен конфиг.

## Приоритет 2 - Commit на интеграцията - завършен
- Интеграцията е commit-ната като `92f5581` след преглед на diff-а.
- Включени са `api/auth.py`, `tests/test_web_auth.py`, документацията и тестовете.
- Git историята не е променяна и не е правен force push.

## Приоритет 3 - След интеграцията
- Broker зарежда `.env` автоматично при фабрично стартиране без явен конфиг; решението е документирано.
- Ако dashboard-ът ще се пуска с повече от един worker, добави shared session store.
- Добави реален end-to-end тест с GitHub App, ако средата позволява стабилни credentials и callback.
