# FuelLead MVP

Внутреннее приложение для поиска потенциальных клиентов топливных карт по ОКВЭД, получения карточек компаний из Checko, хранения данных в PostgreSQL и выгрузки в Excel.

## Что уже реализовано

- поиск организаций через Checko API по основным и дополнительным ОКВЭД (`codes=all`);
- проверка действующего статуса компании;
- получение названия, ИНН, ОГРН, основного и дополнительных ОКВЭД;
- получение нуля, одного или нескольких email из `Контакты → Емэйл`;
- PostgreSQL как основная база, подключение только через переменные окружения;
- дедупликация компаний по ИНН и email внутри компании;
- даты первого обнаружения, последней проверки и последнего обновления;
- статусы `new`, `checked`, `ready` с подготовленной моделью будущих статусов;
- история обнаружения, обновлений, новых email и смен статуса;
- фильтры по статусу, наличию email, категории деятельности, дате, названию и ИНН;
- адаптивный React-интерфейс;
- экспорт текущей выборки в `.xlsx`;
- демонстрационный поиск, если ключ Checko ещё не настроен.

Email-рассылка, поиск сайтов и CRM-функции намеренно не включены.

## Быстрый запуск через Docker

Требуются Docker Desktop и запущенный Docker Engine.

```bash
cd FuelLead
cp .env.example .env
docker compose up --build
```

После запуска откройте:

- интерфейс: `http://localhost:8080`;
- документация API: `http://localhost:8000/docs`;
- проверка backend: `http://localhost:8000/api/health`.

Если `CHECKO_API_KEY` пуст, кнопка «Найти компании» добавит безопасный демонстрационный набор. Это позволяет проверить весь интерфейс, PostgreSQL, статусы, историю, фильтры и экспорт без платных запросов.

## Подключение Checko

Укажите ключ в локальном `.env`:

```dotenv
CHECKO_API_KEY=your-local-key
```

Ключ не передаётся во frontend и не должен попадать в Git. Интеграция использует официальные методы:

- `GET https://api.checko.ru/v2/search` с `by=okved`, `obj=org`, `active=true`, `codes=all`;
- `GET https://api.checko.ru/v2/company` по ИНН.

Документация: [поиск](https://checko.ru/integration/api/search), [карточка организации](https://checko.ru/integration/api/company).

Размер первого поиска задаётся переменной:

```dotenv
DISCOVERY_LIMIT_PER_CODE=10
```

При 13 кодах значение `10` даёт до 130 кандидатов до межкодовой дедупликации. Для теста примерно на 500 кандидатов можно использовать `40`, предварительно оценив стоимость запросов и баланс Checko.

## Настройка PostgreSQL

Перед постоянным использованием замените демонстрационный пароль одновременно в `POSTGRES_PASSWORD` и `DATABASE_URL`:

```dotenv
POSTGRES_DB=fuellead
POSTGRES_USER=fuellead
POSTGRES_PASSWORD=replace-with-a-strong-password
DATABASE_URL=postgresql+psycopg://fuellead:replace-with-a-strong-password@db:5432/fuellead
```

Данные PostgreSQL сохраняются в Docker volume `fuellead_postgres_data` и не исчезают после перезапуска контейнеров.

## Локальная разработка без Docker

Backend по умолчанию использует локальный SQLite только как удобный режим разработки и автоматических тестов. Рабочая конфигурация Docker использует PostgreSQL.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/uvicorn app.main:app --reload
```

Во втором терминале:

```bash
cd frontend
npm install
npm run dev
```

Откройте `http://localhost:5173`.

## Проверки

```bash
cd backend
.venv/bin/python -m pytest

cd ../frontend
npm run build
```

## Основные API

- `GET /api/companies` — список, статистика и фильтры;
- `GET /api/companies/{id}` — карточка, дополнительные ОКВЭД и история;
- `PATCH /api/companies/{id}/status` — смена статуса;
- `POST /api/search-runs` — запуск поиска;
- `GET /api/search-runs/{id}` — состояние поиска;
- `GET /api/export.xlsx` — Excel-выгрузка.

## Структура

```text
FuelLead/
├── backend/       FastAPI, SQLAlchemy, Checko, PostgreSQL, Excel
├── frontend/      React, TypeScript, Vite
├── docker-compose.yml
├── .env.example
└── README.md
```

Таблицы базы: `companies`, `company_emails`, `company_okveds`, `activity_history`, `search_runs`.
