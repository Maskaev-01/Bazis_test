# Асинхронный сервис платежей

Тестовый сервис на FastAPI, PostgreSQL, RabbitMQ и FastStream. Платёжный шлюз эмулируется: задержка 2–5 секунд, вероятность успешного платежа 90%. Реальные деньги не списываются.

## Запуск

Нужны Docker Desktop с запущенным Docker Engine и Docker Compose. В PowerShell:

```powershell
Copy-Item .env.example .env
# Измените API_KEY в .env до запуска
 docker compose up --build -d
 docker compose logs -f api consumer
```

Миграции Alembic выполняются отдельным контейнером перед запуском API и consumer. API доступен на `http://localhost:8000`, RabbitMQ Management — `http://localhost:15672` (логин и пароль `payments`). Для остановки: `docker compose down`. Для удаления также и данных: `docker compose down -v`.

## API

Для каждого запроса нужен заголовок `X-API-Key` со значением из `.env`. Создание также требует непустой `Idempotency-Key` длиной до 255 символов:

```powershell
$headers = @{ 'X-API-Key' = 'change-me'; 'Idempotency-Key' = 'order-123' }
$body = @{ amount = '123.45'; currency = 'RUB'; description = 'Заказ'; metadata = @{ order_id = 123 }; webhook_url = 'http://host.docker.internal:9000/webhook' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/payments -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri http://localhost:8000/api/v1/payments/PAYMENT_ID -Headers @{ 'X-API-Key' = 'change-me' }
```

Сумма положительная, не более 16 цифр до и двух после запятой; валюта `RUB`, `USD` или `EUR`. POST возвращает 202 с `payment_id`, текущим `status` и `created_at`. Повтор с тем же ключом и тем же телом возвращает тот же платёж; другое тело — 409. GET возвращает полные данные либо 404. Неизвестный или отсутствующий API-ключ — 401. `host.docker.internal` в примере указывает на HTTP-приёмник webhook, который нужно запустить отдельно на хосте.

Webhook — HTTP POST с JSON `payment_id`, `status`, `amount` (строка), `currency`, `processed_at` и заголовком `Idempotency-Key`, равным ID платежа. Успехом считается ответ 2xx. Получатель должен поддерживать дедупликацию по этому ключу: при повторной доставке возможен повторный webhook.

## Доставка и восстановление

Платёж и запись outbox создаются одной транзакцией. Consumer публикует неподтверждённые записи в durable exchange `payments` и очередь `payments.new` с publisher confirms; после подтверждения фиксирует время публикации. Блокировка `FOR UPDATE SKIP LOCKED` позволяет нескольким издателям безопасно делить записи. Если процесс завершится после публикации, но до фиксации outbox, событие будет опубликовано повторно: доставка имеет семантику at-least-once. Обработчик блокирует строку платежа и сохраняет итоговый статус до webhook, поэтому повторное событие не запускает эмуляцию заново.

При ошибке обработки или webhook выполняется всего три попытки с задержками 1 и 2 секунды. После этого сообщение отклоняется без повторной постановки и RabbitMQ направляет его через `payments.dlx` в durable очередь `payments.dead`. При аварийном завершении consumer до подтверждения RabbitMQ доставит сообщение заново; счётчик попыток при этом начинается сначала. В DLQ остаётся исходный JSON с ID платежа, его можно проверить и вручную вернуть в `payments.new` после устранения ошибки через RabbitMQ Management. При недоступности брокера outbox остаётся в PostgreSQL и публикация повторяется.

Это демонстрационный сервис: перед публичным развёртыванием необходимо ограничить доступ к API и допустимые адреса webhook (защита от SSRF), заменить демонстрационные пароли и добавить TLS, мониторинг и политику хранения outbox/DLQ.

## Проверка

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest pytest-asyncio
.venv/Scripts/python -m pytest -q
```

Интеграционную проверку выполняйте с запущенным Docker Engine и доступным приёмником webhook; для проверки DLQ укажите недоступный URL и наблюдайте очередь `payments.dead` в RabbitMQ Management.
