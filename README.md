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
- статусы воронки от `new` и `ready` до `sent`, `answered`, `interested` и результата контакта;
- история обнаружения, обновлений, новых email и смен статуса;
- фильтры по статусу, наличию email, категории деятельности, дате, названию и ИНН;
- адаптивный React-интерфейс;
- внутренняя авторизация с постоянной подписанной сессией в `HttpOnly` cookie;
- отдельный дашборд с воронкой, активностью поиска и последними компаниями;
- сохраняемый шаблон письма с переменными компании и текущей даты;
- персональный предпросмотр и редактирование письма выбранной компании;
- безопасная одиночная отправка через Gmail API с записью в историю;
- экспорт текущей выборки в `.xlsx`;
- демонстрационный поиск, если ключ Checko ещё не настроен.

Массовая отправка писем, поиск сайтов и CRM-функции не включены. Приложение отправляет
только одно явно выбранное и проверенное пользователем письмо. Отправитель
`artel.office8@gmail.com` подключается через безопасную OAuth 2.0-интеграцию Gmail API.

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

Перед первым запуском задайте в локальном `.env` отдельные данные входа в FuelLead:

```dotenv
FUELLEAD_AUTH_EMAIL=operator@example.com
FUELLEAD_AUTH_PASSWORD=replace-with-a-strong-password
FUELLEAD_AUTH_SESSION_SECRET=replace-with-a-long-random-secret
FUELLEAD_AUTH_COOKIE_DAYS=3650
FUELLEAD_AUTH_COOKIE_SECURE=false
```

Пароль проверяется только backend и не включается во frontend-сборку. Для размещения
исключительно по HTTPS установите `FUELLEAD_AUTH_COOKIE_SECURE=true`. Вход сохраняется
на устройстве на 3650 дней и переживает перезапуски backend: повторная авторизация нужна
только после явного выхода, очистки cookies либо смены пароля/секрета сессии.

## Подключение Checko

Укажите ключ в локальном `.env`:

```dotenv
CHECKO_API_KEY=your-local-key
CHECKO_API_KEY_FALLBACKS=your-second-key,your-third-key
```

Ключи не передаются во frontend и не должны попадать в Git. При исчерпании суточного
лимита или отклонении текущего ключа backend повторяет тот же запрос со следующим ключом.
Если недоступны все настроенные ключи, поиск корректно останавливается с сохранением
уже полученных результатов. Интеграция использует официальные методы:

- `GET https://api.checko.ru/v2/search` с `by=okved`, `obj=org`, `active=true`, `codes=all`
  и отдельными запросами `region=77` (Москва), `region=50` (Московская область);
- `GET https://api.checko.ru/v2/company` по ИНН.

Перед сохранением backend повторно проверяет `Регион.Код` полной карточки. Компании
других регионов и карточки без кода региона в PostgreSQL не записываются.

Документация: [поиск](https://checko.ru/integration/api/search), [карточка организации](https://checko.ru/integration/api/company).

Размер первого поиска задаётся переменной:

```dotenv
DISCOVERY_LIMIT_PER_CODE=10
```

По каждому ОКВЭД лимит применяется отдельно к Москве и Московской области. При 13 кодах
значение `10` даёт до 260 кандидатов до межкодовой дедупликации. Перед увеличением лимита
оцените стоимость запросов и баланс Checko.

Для проверки и удаления уже сохранённых компаний других регионов используйте безопасную
команду. Без `--apply` она только показывает план удаления:

```bash
docker compose exec backend python -m app.commands.prune_non_target_regions
docker compose exec backend python -m app.commands.prune_non_target_regions --apply
```

## Gmail без пароля приложения

В качестве отправителя зафиксирован `artel.office8@gmail.com`. Обычный пароль Google
в приложение добавлять нельзя. Если раздел «Пароли приложений» недоступен, используется
Gmail API с OAuth 2.0:

1. Создайте проект в Google Cloud и включите Gmail API.
2. Настройте OAuth consent screen и создайте OAuth Client ID.
3. Один раз авторизуйте именно `artel.office8@gmail.com` со scope
   `https://www.googleapis.com/auth/gmail.send`, чтобы получить refresh token.
4. Добавьте локально в `.env` значения `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` и
   `GMAIL_REFRESH_TOKEN`. Не коммитьте и не присылайте их в чат.
5. Проверьте `GET /api/health`: поле `gmail_oauth_configured` должно стать `true`.

После изменения `.env` пересоздайте backend, чтобы контейнер получил новые значения:

```bash
docker compose up -d --force-recreate backend
```

Для одиночной проверки отправьте письмо самому отправителю:

```bash
docker compose exec backend python -m app.commands.send_gmail_test
```

При необходимости можно явно передать тестовый адрес через
`--recipient test@example.com`. Это отправляет ровно одно письмо и не выбирает компании
из базы. `GMAIL_ACCESS_TOKEN` сохранять не нужно: он короткоживущий, а backend получает
свежий access token через refresh token перед отправкой.

Backend содержит сервис отправки через Gmail API (`backend/app/services/gmail.py`).
В разделе «Шаблон письма» можно сохранить общий текст, выбрать компанию с email,
проверить подстановки, индивидуально изменить готовое письмо и отправить только его.
Кнопки массовой рассылки в приложении нет.

Доступные переменные шаблона:

- `{{company_name}}` — название компании;
- `{{date}}` — сегодняшняя дата в часовом поясе приложения;
- `{{inn}}` — ИНН;
- `{{primary_okved}}` — основной ОКВЭД;
- `{{email}}` — выбранный email получателя.

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

- `POST /api/auth/login` — вход и создание защищённой сессии;
- `GET /api/auth/session` — проверка текущей сессии;
- `POST /api/auth/logout` — выход и отзыв текущей сессии;
- `GET /api/companies` — список, статистика и фильтры;
- `GET /api/companies/{id}` — карточка, дополнительные ОКВЭД и история;
- `PATCH /api/companies/{id}/status` — смена статуса;
- `GET /api/dashboard` — показатели, воронка, активность и последние компании;
- `GET /api/email-template` — текущий основной шаблон и список переменных;
- `PUT /api/email-template` — сохранение основного шаблона;
- `POST /api/email-template/preview` — персонализация шаблона для выбранной компании;
- `POST /api/companies/{id}/send-email` — одиночная отправка проверенного письма;
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

Таблицы базы: `companies`, `company_emails`, `company_okveds`, `activity_history`,
`search_runs`, `email_templates`.
