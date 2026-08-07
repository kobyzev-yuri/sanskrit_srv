# Руководство администратора

Установка, конфигурация и эксплуатация **Sanskrit SRV** (alpha `0.1.x`).  
Работа в UI для экспертов — в [руководстве пользователя](user-guide.md).

---

## 1. Состав системы

| Процесс | Назначение |
|---------|------------|
| **API** (`uvicorn app.main:app`) | HTTP API, статический UI, JWT-auth, экспорт PDF |
| **Worker** (`python -m app.worker`) | Очередь jobs: нарезка сканов + LLM-черновики |
| **SQLite** | Пользователи, проекты, страницы, версии, usage (по умолчанию) |
| **storage/** | Исходные PDF, PNG страниц, экспорты |
| **ProxyAPI** | Шлюз к Gemini / OpenAI (один ключ) |

Без запущенного **worker** загрузка PDF создаст проект, но страницы не переведутся.

На текущем тестовом VPS (~1 ГБ RAM) — один API + один worker, без Postgres/Redis/Celery. Для release 1.0 планируется нормальный сервер и проверка нагрузки.

---

## 2. Этапы установки

### Вариант A — локально (разработка / проверка)

1. **Клон и Python**
   ```bash
   git clone git@github.com:kobyzev-yuri/sanskrit_srv.git
   cd sanskrit_srv
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install -r backend/requirements.txt
   ```

2. **Конфигурация**
   ```bash
   cp .env.example .env
   ```
   Заполните минимум: `JWT_SECRET`, `OPENAI_API_KEY` (см. [§3](#3-конфигурация)).

3. **Каталоги и БД**
   ```bash
   mkdir -p data storage
   cd backend
   export PYTHONPATH=.
   python -m app.cli init-db
   ```

4. **Первый admin**
   ```bash
   python -m app.cli user-create \
     --email admin@example.com \
     --password 'STRONG_PASSWORD' \
     --role admin \
     --name Admin
   ```

5. **Запуск двух процессов**
   ```bash
   # из backend/, PYTHONPATH=.
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   # во втором терминале:
   python -m app.worker
   ```

6. **Проверка**
   ```bash
   curl -s http://127.0.0.1:8000/health
   ```
   Откройте http://127.0.0.1:8000 и войдите созданным admin.

### Вариант B — VPS (systemd, как на тестовом хосте)

Целевой каталог: `/opt/sanskrit_srv`.

1. Синхронизируйте код на сервер (вручную или через GitHub Actions — [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)).  
   **Не** перезаписывайте на сервере `.env`, `data/`, `storage/`.

2. Один раз положите секреты:
   ```bash
   scp .env root@HOST:/opt/sanskrit_srv/.env
   chmod 600 /opt/sanskrit_srv/.env
   ```

3. На сервере:
   ```bash
   bash /opt/sanskrit_srv/deploy/remote_deploy.sh
   ```
   Скрипт: создаёт venv, ставит зависимости, ставит unit-файлы `sanskrit-srv` и `sanskrit-worker`, перезапускает службы, дергает `/health`.

4. Bootstrap admin (если ещё нет пользователей):
   ```bash
   cd /opt/sanskrit_srv/backend
   PYTHONPATH=. /opt/sanskrit_srv/.venv/bin/python -m app.cli user-create \
     --email admin@example.com --password '...' --role admin --name Admin
   ```

5. Проксируйте `:8000` через nginx/Caddy с HTTPS (по политике хоста).

### Вариант C — Docker Compose (опционально)

[`docker-compose.yml`](../docker-compose.yml) поднимает только API-образ с монтированием репозитория. Worker в compose не описан — для полного контура удобнее вариант A/B. На слабом VPS предпочтителен systemd + SQLite (см. комментарий в compose).

---

## 3. Конфигурация

Файл `.env` в корне репозитория (или `/opt/sanskrit_srv/.env`). Шаблон: [`.env.example`](../.env.example).  
Секреты **не коммитить**.

| Переменная | Обязательно | Назначение |
|------------|-------------|------------|
| `DATABASE_URL` | да | По умолчанию SQLite, напр. `sqlite:////opt/sanskrit_srv/data/sanskrit_srv.db` |
| `JWT_SECRET` | да | Длинная случайная строка для подписи токенов |
| `STORAGE_ROOT` | да | Каталог файлов проекта (сканы, PDF) |
| `CORS_ORIGINS` | нет | Список origin через запятую; `*` для alpha |
| `OPENAI_API_KEY` | да* | Ключ ProxyAPI (`*` нужен для LLM; без ключа — только extract/заглушки) |
| `OPENAI_BASE_URL` | нет | По умолчанию `https://api.proxyapi.ru/openai/v1` |
| `OPENAI_MODEL` | нет | Модель OpenAI-канала, по умолчанию `gpt-4o-mini` |
| `GEMINI_BASE_URL` | нет | По умолчанию `https://api.proxyapi.ru/google` |
| `GEMINI_MODEL` | нет | Модель Gemini-канала, по умолчанию `gemini-2.5-flash`. **Не** ставьте сюда `claude-*` |
| `ANTHROPIC_BASE_URL` | нет | По умолчанию `https://api.proxyapi.ru/anthropic` |
| `ANTHROPIC_MODEL` | нет | Если задано (напр. `claude-opus-5`) — Claude идёт **первым**; иначе только Gemini→OpenAI |
| `LARGE_BOOK_PAGES` | нет | Порог подтверждения «перевести всю книгу» (по умолчанию `100`) |
| `DEFAULT_EXTRACT_MAX_PAGES` | нет | Устарело для upload (книга целиком); можно `0` |
| `LLM_PRICE_PER_1M` | нет | JSON тарифов USD/1M токенов для оценки $ в UI |

Пример оценки стоимости:

```bash
LLM_PRICE_PER_1M={"gemini:gemini-2.5-flash":{"in":0.1,"out":0.4},"openai:gpt-4o-mini":{"in":0.15,"out":0.6}}
```

Ключ: `сеть:модель`. Цены сверяйте с тарифами ProxyAPI вручную.

Приложение ищет `.env` в: текущий каталог → корень репозитория → `/opt/sanskrit_srv/.env`.

---

## 4. CLI

Из каталога `backend/` с `PYTHONPATH=.` (или через `.venv`):

```bash
python -m app.cli init-db
python -m app.cli user-create --email u@x --password '...' --role expert --name "Имя"
python -m app.cli user-list
python -m app.cli user-reset-password --email u@x --password '...'
```

Роли: `admin` · `expert` · `scholar` · `reader`.

---

## 5. Бэкофис в UI

Роль **admin** → пункт **Бэкофис**:

| Раздел | Действия |
|--------|----------|
| Пользователи | Создание учёток, роли, активность |
| Каталог LLM | Справочник моделей (для маршрутизации/отображения) |

Загрузка PDF — на экране **Проекты** (только admin). Книги > `LARGE_BOOK_PAGES` страниц требуют подтверждения полного перевода.

---

## 6. Эксплуатация

| Задача | Как |
|--------|-----|
| Статус служб (VPS) | `systemctl status sanskrit-srv sanskrit-worker` |
| Логи | `journalctl -u sanskrit-srv -f` · `journalctl -u sanskrit-worker -f` |
| Health | `GET /health` → `{"status":"ok",...}` |
| Бэкап | Копировать `data/*.db` и дерево `storage/` при остановленном worker (или consistent snapshot) |
| Обновление кода | rsync/Actions → `remote_deploy.sh` (venv + restart); `.env` / data / storage не трогать |
| Перезапуск конвейера книги | В UI (admin): **Перевести всю книгу заново** — дорого, затирает черновики |

Лимиты alpha на слабом хосте: `MemoryMax` в unit-файлах (~400M API / ~550M worker). Большие книги (сотни страниц) гоняются **последовательно**, долго и с расходом ProxyAPI.

---

## 7. Учёт LLM

Успешные вызовы ProxyAPI пишутся в БД; в редакторе полоса **«Расход LLM»** (admin / expert / scholar).  
Неуспешные HTTP не тарифицируются в учёте; локальный extract без LLM = 0.

---

## 8. Дорожная карта релизов (ops)

| Этап | Критерий перехода |
|------|-------------------|
| **Alpha** | Текущий контур; сбор замечаний экспертов |
| **Beta** | Замечания по UI/процессу закрыты; предсказуемое поведение на тестовых книгах |
| **1.0** | Нагрузка на нормальном сервере (RAM/CPU, параллелизм, мониторинг); стабильный деплой и бэкапы |

Планируемые сдвиги к 1.0 (не в alpha): больше RAM / отдельная БД по необходимости, контролируемый параллелизм воркеров, ужесточение CORS и HTTPS-only.
