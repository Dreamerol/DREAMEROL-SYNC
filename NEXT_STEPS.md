# NEXT_STEPS

## Приоритет 1 - Реален GitHub smoke test
- `.\scripts\start-smoke.ps1` стартира Broker, dashboard и cloudflared, обновява callback URL и проверява тунела.
- Активен quick tunnel: `https://sealed-arguments-quality-stays.trycloudflare.com` (Broker PID 18608, dashboard PID 15192, cloudflared PID 14768).
- Замени placeholder стойностите в `.env` с реални `GITHUB_APP_CLIENT_ID` и `GITHUB_APP_CLIENT_SECRET`.
- Пусни `.\scripts\start-smoke.ps1 -Force`, за да се създаде нов тунел и да се обнови callback URL.
- Задай новия callback URL в GitHub App настройките; пътят трябва да е `/auth/callback`.
- Провери: login, връщане към dashboard, preview на тестов fork, sync на fork със статус `behind` и logout.
- Спри услугите с `.\scripts\stop-smoke.ps1`.

## Приоритет 2 - Commit на интеграцията - завършен
- Интеграцията е commit-ната като `92f5581` след преглед на diff-а.
- Включени са `api/auth.py`, `tests/test_web_auth.py`, документацията и тестовете.
- Git историята не е променяна и не е правен force push.

## Приоритет 3 - След интеграцията
- Broker зарежда `.env` автоматично при фабрично стартиране без явен конфиг; решението е документирано.
- Security baseline е добавен и commit-нат като `be41a0b`: `no-store` за `/api/`, POST-only state changes, тестове за session fixation, Bearer precedence и Secure cookie.
- `cloudflared` е инсталиран за локален HTTPS callback; quick tunnel-ът е временен и URL-ът се променя при рестарт.
- PowerShell скриптовете `start-smoke.ps1` и `stop-smoke.ps1` автоматизират стартирането, callback обновяването и спирането.
- Broker hardening: всички `/auth/` отговори са no-store/no-cache с допълнителни security headers; `BROKER_TRUSTED_PROXIES=127.0.0.1,::1` позволява правилен client IP зад cloudflared.
- Ако dashboard-ът ще се пуска с повече от един worker, добави shared session store.
- Добави реален end-to-end тест с GitHub App, ако средата позволява стабилни credentials и callback.
