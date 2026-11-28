# Базовый аудит логирования (Issue #5)

Дата проведения: 2026-11-28  
Статус: Завершено (фиксация исходного состояния «как есть» до доработок логирования)

## 1. Параметры тестового стенда

- **Commit SHA:** `e1c425e0e0d0014153de9c8bc419c2426bc049ec`
- **Модель LLM:** `deepseek-v4.1-flash` (`provider=custom`, `base_url=https://mai.ru`)
- **Конфигурация окружения (без секретов):**
  - Развёртывание: Docker Compose (`backend`, `frontend`, `postgres`)
  - `LLM_API_KEY`: `<REDACTED>` (задан)
  - `TELEGRAM_BOT_TOKEN`: `<REDACTED>` (задан, polling активен)
  - `AI_API_CA_BUNDLE`: `/app/certs/mai-api-ca-chain.pem`
  - Базы данных PostgreSQL: `mai_backend`, `mai_agent`, `mai_fis_gia` (инициализированы, загружен синтетический реестр ЕГЭ `synthetic_ege_registry.json`)
  - Уровень логирования по умолчанию: `INFO` (для `uvicorn`, `app.*`, `httpx`, `hermes`)

---

## 2. Карта источников журналов

| Компонент | Контейнер / Путь | Механизм вывода | Что попадает в журнал |
| :--- | :--- | :--- | :--- |
| **Backend (FastAPI / Uvicorn)** | `backend-1` (`stdout` / `stderr`) | `docker compose logs backend` | HTTP-запросы (`method`, `path`, `status_code`, IP клиента), healthcheck `GET /ready`, события `alembic` |
| **Telegram Service** | `backend-1` (`stdout` / `stderr`) | Логгер `app.telegram_service` и `httpx` | Входящие апдейты (`update_id`, `chat_id`, полный текст сообщения), этапы обработки, полные URL-запросы `httpx` к `api.telegram.org` |
| **ACP (Hermes Client / Agent)** | `backend-1` (`stdout` / `stderr`) и `/root/.hermes/logs/agent.log` | Логгер `app.hermes_client` и внутренний логгер Hermes | Открытие/загрузка сессий (`session_id`), контекст хода (`platform=acp`, `model`, `history`, `msg`), токены и латентность вызовов LLM |
| **ACP / Agent Errors** | `backend-1` (`/root/.hermes/logs/errors.log`) | Внутренний файловый логгер Hermes | Предупреждения `tools.registry` о недоступных опциональных инструментах, ошибки выполнения инструментов и сбои API |
| **MCP (FastMCP)** | `backend-1` (`stdout` / `stderr`) и `/root/.hermes/logs/agent.log` | Логгер `agent.tool_executor` (внутри Hermes) | Имя вызванного MCP-инструмента (`mcp__mai_agent__sql_query`, `mcp__...`), время выполнения, текст ошибки PostgreSQL и SQL-запрос при сбое |
| **Состояние сессий Hermes** | `backend-1` (`/root/.hermes/state.db`) | SQLite | Внутреннее состояние сессий и история сообщений агента |
| **Frontend** | `frontend-1` (`stdout` / `stderr`) | `docker compose logs frontend` | Системные события запуска и access/error логи `nginx/1.31.5` |
| **PostgreSQL** | `postgres-1` (`stdout` / `stderr`) | `docker compose logs postgres` | События инициализации кластера БД и системные сообщения СУБД |

---

## 3. Методика воспроизведения прогона

Для повторения базового замера на чистом стенде:

1. Запустить стек и убедиться, что синтетический реестр ЕГЭ загружен:
   ```bash
   docker compose up --build -d
   docker compose exec backend python -m app.synthetic_ege_registry /app/fixtures/synthetic_ege_registry.json
   ```
2. Выполнить парные запросы с синтетическими метками `AUDIT_*`:
   - **Backend (штатно / ошибка):** `POST /chat/sessions` и `POST /chat/sessions/{id}/messages` с валидным JSON (`AUDIT_BE_OK_01`) и невалидным телом (`AUDIT_BE_ERR_01`).
   - **Telegram (штатно / ошибка):** отправка текстового сообщения (`AUDIT_TG_OK_01`) и неизвестной команды (`/broken_AUDIT_TG_ERR_01`) в чат бота.
   - **ACP (штатно / ошибка):** обработка диалогового хода агентом (`AUDIT_ACP_OK_01`) и проверка старта при некорректном пути `AI_API_CA_BUNDLE` (`AUDIT_ACP_ERR_01`).
   - **MCP (штатно / ошибка):** запрос поиска в реестре `mai_fis_gia` (`AUDIT_MCP_OK_01`) и заведомо невалидный SQL-запрос через `sql_query` (`SELECT * FROM nonexistent_table_AUDIT_MCP_ERR_01`).
3. Снять журналы контейнеров и внутренние логи Hermes:
   ```bash
   docker compose logs backend > ../audit_backend.log
   docker compose logs frontend > ../audit_frontend.log
   docker compose logs postgres > ../audit_postgres.log
   docker compose exec backend cat /root/.hermes/logs/agent.log > ../hermes_agent.log
   docker compose exec backend cat /root/.hermes/logs/errors.log > ../hermes_errors.log
   ```

---

## 4. Протокол исходного прогона (синтетические метки)

| ID сценария (метка) | Компонент | Тип сценария | Действие | Зафиксировано в логе | Категории полей в записи журнала |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `AUDIT_BE_OK_01` / `GET /ready` | **Backend** | Штатный | Создание сессии и отправка сообщения через REST API (`POST /chat/sessions/.../messages`) | Да (`stdout` `backend-1`) | `client_ip`, `http_method`, `endpoint_path`, `session_id`, `http_status` (`200 OK`) |
| `AUDIT_BE_ERR_01` | **Backend** | Ошибка | Отправка невалидного JSON-тела на эндпоинт сообщений (`422 Unprocessable Entity` / `503`) | Да (`stdout` `backend-1`) | `client_ip`, `http_method`, `endpoint_path`, `session_id`, `http_status` (тело невалидного запроса в лог Uvicorn не пишется) |
| `AUDIT_TG_OK_01` | **Telegram** | Штатный | Отправка текстового сообщения с меткой в чат Telegram-бота | Да (`stdout` `backend-1`) | `timestamp`, `log_level`, `logger_name`, `update_id`, `message_type` (`TEXT`), `chat_id`, `raw_message_text`, `with_image_flag`, `session_id` |
| `AUDIT_TG_ERR_01` | **Telegram** | Ошибка | Отправка неизвестной команды `/broken_AUDIT_TG_ERR_01` (или сбой доставки в Hermes) | Да (`stdout` `backend-1`) | `timestamp`, `log_level`, `logger_name`, `update_id`, `chat_id`, `raw_message_text`; при сбое ACP добавляются `WARNING`, `duration_sec`, `error_summary` |
| `AUDIT_ACP_OK_01` | **ACP** | Штатный | Передача промпта от Backend в `hermes-acp` и получение ответа от `deepseek-v4.1-flash` | Да (`agent.log` + `stdout`) | `timestamp`, `log_level`, `session_id`, `logger_name`, `model`, `provider`, `platform` (`acp`), `history_len`, `raw_user_msg`, `input_tokens`, `output_tokens`, `latency_sec`, `finish_reason`, `response_len` |
| `AUDIT_ACP_ERR_01` | **ACP** | Ошибка | Попытка запуска `hermes-acp` при некорректном пути к `AI_API_CA_BUNDLE` / обрыве связи с LLM | Да (`stdout` + `errors.log`) | `timestamp`, `log_level` (`WARNING`), `logger_name`, `duration_sec`, `error_summary` (`Hermes ACP startup failed` / `API call failed`) |
| `AUDIT_MCP_OK_01` | **MCP** | Штатный | Запрос на поиск кандидата в синтетическом реестре ЕГЭ (`mai_fis_gia`) | Да (`agent.log` + `stdout`) | `timestamp`, `log_level`, `session_id`, `tool_name`, `execution_duration_sec`, `model_turn_metrics` (аргументы и результат успешного вызова MCP детально не логируются на уровне `INFO`) |
| `AUDIT_MCP_ERR_01` | **MCP** | Ошибка | Вызов `sql_query` с запросом к несуществующей таблице (`nonexistent_table_AUDIT_MCP_ERR_01`) | Да (`agent.log` + `errors.log` + `stdout`) | `timestamp`, `log_level` (`WARNING`), `session_id`, `logger_name` (`agent.tool_executor`), `tool_name` (`mcp__mai_agent__sql_query`), `duration_sec`, `db_error_type` (`UndefinedTable`), `raw_sql_query` |

---

## 5. Ключевые наблюдения по безопасности и полноте логов

1. **Утечка секрета в логах `httpx`:** Библиотека `httpx` на уровне `INFO` записывает в `stdout` полный URL каждого запроса к Telegram Bot API (`https://telegram.org<TOKEN>/getUpdates` и `sendMessage`), компрометируя `TELEGRAM_BOT_TOKEN`.
2. **Логирование открытых пользовательских данных и SQL:** Модули `app.telegram_service` и `agent.turn_context` пишут полный текст сообщений пользователей (`text=...`, `msg=...`) и идентификаторы `chat_id` в открытом виде; при ошибках `sql_query` в лог попадает полный текст SQL-запроса.
3. **Разрозненность источников:** Логи транспортного слоя пишутся в `stdout`/`stderr` контейнера, тогда как детальная телеметрия агента и вызовов MCP-инструментов дублируется во внутренние файлы `/root/.hermes/logs/agent.log` и `errors.log` внутри контейнера `backend-1` (без монтирования на хост).
4. **Шум в логах:** Каждые 5 секунд healthcheck `GET /ready` генерирует по 3 строки логов (`alembic` + `uvicorn`), а каждый ход агента записывает ~20 строк `WARNING` от `tools.registry` о неиспользуемых интеграциях (Spotify, Discord, Feishu и др.).
