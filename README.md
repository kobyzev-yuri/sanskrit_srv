# Sanskrit SRV

Сервис оцифровки санскритских **сканов и изданий** (Devanāgarī): PDF → черновик HTML (LLM) → сверка экспертом → выверенный PDF.

| Статус | Версия | Смысл |
|--------|--------|--------|
| **Alpha** (сейчас) | `0.1.x` | Работает на тестовом контуре; идёт проверка пользователями |
| **Beta** | `0.2.x` | После обратной связи по alpha: стабильнее UI/процесс, без ломающих сюрпризов для экспертов |
| **Release 1.0** | `1.0.0` | После проверки под нагрузкой на нормальном сервере (не VPS ~1 ГБ) |

Не production-ready: один воркер, SQLite, ограниченная RAM на текущем хосте.

---

## Документация

| Документ | Для кого |
|----------|----------|
| [**Руководство администратора**](docs/admin-guide.md) | Установка, конфигурация, деплой, пользователи, эксплуатация |
| [**Руководство пользователя**](docs/user-guide.md) | UI, статусы страниц, сверка, задания LLM, PDF, use cases |

Кратко: **admin** загружает PDF и ведёт пользователей; **expert** / **scholar** сверяют скан ‖ текст и согласуют страницы; **reader** в alpha почти не задействован.

---

## Что умеет alpha

- Загрузка PDF-скана → нарезка страниц → vision-LLM черновик HTML (по умолчанию OpenRouter `stealth/ox-alpha`; запасные Gemini/Opus через ProxyAPI)
- Редактор: скан слева, превью/HTML справа; статусы **согласовано** / **на правке**
- Точечный пересмотр страницы заданием на естественном языке
- Экспорт PDF (текст) и PDF «скан ‖ текст»
- Учёт расхода LLM по проекту
- Роли: `admin` · `expert` · `scholar` · `reader`
- Деплой: systemd (API + worker) или локальный venv / опциональный Docker

---

## Быстрый старт (локально)

Требования: **Python 3.12+**, ключ [OpenRouter](https://openrouter.ai) (`OPENROUTER_API_KEY`) для перевода страниц. ProxyAPI — опционально, если в бэкофисе выбран Gemini/Opus.

```bash
git clone git@github.com:kobyzev-yuri/sanskrit_srv.git
cd sanskrit_srv
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

cp .env.example .env
# отредактируйте JWT_SECRET и OPENROUTER_API_KEY

mkdir -p data storage
cd backend
export PYTHONPATH=.
python -m app.cli init-db
python -m app.cli user-create --email admin@local --password 'changeme' --role admin --name Admin

# терминал 1 — API + UI
uvicorn app.main:app --host 0.0.0.0 --port 8000

# терминал 2 — конвейер extract + LLM
python -m app.worker
```

Откройте http://127.0.0.1:8000 → вход → загрузка PDF (роль admin).

Полные этапы, переменные окружения и деплой на VPS — в [руководстве администратора](docs/admin-guide.md).

---

## Структура репозитория

```text
sanskrit_srv/
├── README.md
├── .env.example          ← шаблон конфигурации
├── docker-compose.yml    ← опционально для локальной разработки
├── deploy/               ← systemd + remote_deploy.sh
├── backend/              ← FastAPI, worker, CLI
├── frontend/             ← статический UI (отдаёт API)
└── docs/                 ← руководства
```

Данные в рантайме (не в git): `data/` (SQLite), `storage/` (сканы, PDF), `.env`.

---

## Лицензия и источники

Рабочие PDF-источники и права на них — на стороне оператора проекта. Код сервиса в этом репозитории.
